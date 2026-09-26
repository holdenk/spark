/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.spark.sql.catalyst.analysis

import org.apache.spark.internal.LogKeys
import org.apache.spark.sql.catalyst.expressions.{TranspiledPythonUDF, TranspiledUDFParameter}
import org.apache.spark.sql.catalyst.plans.logical.LogicalPlan
import org.apache.spark.sql.catalyst.rules.Rule
import org.apache.spark.sql.catalyst.trees.TreePattern.TRANSPILED_PYTHON_UDF
import org.apache.spark.sql.types.{BinaryType, BooleanType, DataType, DecimalType, NumericType, StringType}

/**
 * Prunes the per-input-type options carried by a [[TranspiledPythonUDF]] down to those whose
 * declared categories match the resolved argument types.
 *
 * A Python operator such as `a + b` is overloaded for text, so the transpiler emits one option
 * per input-type variant -- a numeric `Add` and a string `concat`, say -- each tagged with the
 * input-type categories it expects. Those options are children of the node, so leaving a
 * type-incompatible one in place (a numeric `Add` over string columns) would make `CheckAnalysis`
 * reject the whole plan. We can only choose once the argument types are known, which is after
 * reference resolution -- hence a rule here rather than in the builder, which runs at
 * call-construction time before the columns are bound -- and we must run before `CheckAnalysis`.
 *
 * Matching is strict by category (a numeric option only for numeric columns, a string option only
 * for string columns). We deliberately do not lean on implicit type coercion, which would, e.g.,
 * make a numeric `Add` "valid" over a string column and silently diverge from Python's
 * `TypeError`. When no option matches, the list is emptied and `ConvertToCatalyst` falls back to
 * the original Python UDF.
 *
 * A category match does not make an option resolvable -- `cast(binary as bigint)` matches "binary"
 * and never resolves -- and this rule deliberately does not check, because the options it hands
 * back are unresolved on purpose and stay that way until the analyzer coerces them. Dropping the
 * ones that never get there is [[DropUnresolvedTranspiledPythonUDFOptions]], which runs once this
 * batch has converged.
 */
object ResolveTranspiledPythonUDFOptions extends Rule[LogicalPlan] {
  def apply(plan: LogicalPlan): LogicalPlan = {
    if (!plan.containsPattern(TRANSPILED_PYTHON_UDF)) {
      plan
    } else {
      plan.resolveOperatorsWithPruning(_.containsPattern(TRANSPILED_PYTHON_UDF)) {
        case op if op.containsPattern(TRANSPILED_PYTHON_UDF) =>
          // Bottom-up so a nested TranspiledPythonUDF (a transpiled UDF feeding another) is pruned
          // -- and thus resolved -- before its parent's input types are inspected.
          op.transformExpressionsUpWithPruning(_.containsPattern(TRANSPILED_PYTHON_UDF)) {
            // The second half of the guard stops this firing every later iteration: categories
            // cleared and options resolved means there's nothing to do. It would still converge
            // without it, but it would re-walk every option each time round.
            case t: TranspiledPythonUDF if t.arguments.forall(_.resolved) &&
                (t.optionInputCategories.nonEmpty || !t.transpiledOptions.forall(_.resolved)) =>
              pruneByCategoryAndTypeParameters(t)
          }
      }
    }
  }

  /**
   * Prunes to the options whose categories match the argument types (when the categories are still
   * set, clearing them), then types every `_udf_param_N` reference from the argument it stands for.
   *
   * Shared with [[DropUnresolvedTranspiledPythonUDFOptions]], which has to do this same work for a
   * node this rule can never reach -- see that rule's doc. One copy, so the two cannot drift.
   *
   * The options are unresolved until the typing runs, so from inside the Resolution batch the
   * analyzer comes back after and coerces their bodies like anything else.
   */
  private[analysis] def pruneByCategoryAndTypeParameters(
      t: TranspiledPythonUDF): TranspiledPythonUDF = {
    val args = t.arguments
    val pruned = if (t.optionInputCategories.isEmpty) {
      t
    } else {
      val argTypes = args.map(_.dataType)
      val kept = t.transpiledOptions.zip(t.optionInputCategories).collect {
        case (option, categories) if optionMatchesTypes(categories, argTypes) => option
      }
      t.copy(transpiledOptions = kept, optionInputCategories = Nil)
    }
    pruned.copy(transpiledOptions =
      pruned.transpiledOptions.map(TranspiledUDFParameter.resolveTypes(_, args)))
  }

  // True when each declared category matches the corresponding argument type:
  // "numeric" -> NumericType, "string" -> StringType, "bool" -> BooleanType,
  // "binary" -> BinaryType. "string" matches only StringType (not BinaryType): a
  // bytes/BinaryType column is tagged "binary" instead, so the string lowerings
  // (e.g. `repeat`) never see it. Empty categories means "no restriction", so the
  // option is kept.
  //
  // Two deliberate exclusions keep the transpiled semantics faithful to Python:
  // - DecimalType is NOT "numeric": Python receives decimal.Decimal objects,
  //   which raise TypeError when mixed with float literals and carry different
  //   precision semantics than Spark's decimal arithmetic, so decimal columns
  //   fall back to interpreted Python.
  // - "string" requires the default UTF8_BINARY collation: under a non-binary
  //   collation (e.g. UTF8_LCASE) Spark's `=`/`<`/`concat` follow collation
  //   rules while Python compares codepoints, so `'abc' == 'ABC'` would return
  //   true where Python returns False.
  private def optionMatchesTypes(categories: Seq[String], argTypes: Seq[DataType]): Boolean = {
    if (categories.isEmpty) {
      true
    } else if (categories.length != argTypes.length) {
      false
    } else {
      categories.zip(argTypes).forall {
        case ("numeric", dt) => dt.isInstanceOf[NumericType] && !dt.isInstanceOf[DecimalType]
        case ("string", st: StringType) => st.isUTF8BinaryCollation
        case ("bool", dt) => dt.isInstanceOf[BooleanType]
        case ("binary", dt) => dt.isInstanceOf[BinaryType]
        case _ => false
      }
    }
  }
}

/**
 * Drops any transpiled option that analysis left unresolved, so the call falls back to interpreted
 * Python instead of failing the query.
 *
 * An option is a child of [[TranspiledPythonUDF]], so one that is still unresolved when analysis
 * finishes reaches `CheckAnalysis`, which reports on an expression the user never wrote. For the
 * common shape that is a type-check failure: an option of `cast(binary as bigint)` is measured as
 * [[DATATYPE_MISMATCH.CAST_WITHOUT_SUGGESTION]], "cannot cast BINARY to BIGINT", naming a cast the
 * transpiler invented. It is an internal error only where a `_udf_param_N` reference is left
 * untyped and `CheckAnalysis` reads `dataType` off it -- since SPARK-58626 that means a nested
 * call, which is why this rule prunes and types such a node rather than skipping it. Either way the
 * honest answer is the one a category miss already gets: run the Python.
 *
 * Matching categories does not rule any of this out -- the transpiler picks a lowering per operator
 * rather than per exact type, so `cast(binary as bigint)` matches "binary" and never resolves.
 *
 * Separate from [[ResolveTranspiledPythonUDFOptions]], and in a batch after the Resolution batch,
 * because inside that batch `!resolved` does not mean unresolvable. Every option starts unresolved
 * by design: its `_udf_param_N` references carry no type until ResolveTranspiledPythonUDFOptions
 * reads one off each bound argument, and the node staying unresolved is what brings the analyzer
 * back to coerce the body (SPARK-58626). Dropping on `!resolved` from inside the batch would throw
 * away every option that reads a parameter and turn transpilation off without saying so. Once the
 * batch is at a fixed point, nothing is going to resolve one.
 *
 * The cost of waiting, and it is a real one: a reference above the call cannot resolve while the
 * option holds the node unresolved, so for those queries the batch converges with that reference
 * unresolved too and `CheckAnalysis` reports it instead of the fallback taking effect. The message
 * gets worse, not merely different. Measured on `SELECT r FROM (SELECT f(b) AS r FROM t)` where
 * `f`'s option cannot resolve:
 *
 *   before: [[DATATYPE_MISMATCH.CAST_WITHOUT_SUGGESTION]] cannot cast "BINARY" to "BIGINT"
 *   after:  [[UNRESOLVED_COLUMN.WITH_SUGGESTION]] `r` cannot be resolved. Did you mean [`r`]
 *
 * which offers `r` as the fix for `r` and never mentions the UDF. It reaches SQL and Connect, whose
 * analysis is single-pass; the classic DataFrame API is spared because each `select` analyzes
 * eagerly, so the reference is bound before this rule ever sees the plan. Not an internal error
 * either before or after -- an earlier version of this comment claimed the fallback traded an
 * internal error for an ordinary one, and that was wrong in both directions.
 */
object DropUnresolvedTranspiledPythonUDFOptions extends Rule[LogicalPlan] {
  def apply(plan: LogicalPlan): LogicalPlan = {
    if (!plan.containsPattern(TRANSPILED_PYTHON_UDF)) {
      plan
    } else {
      plan.resolveOperatorsWithPruning(_.containsPattern(TRANSPILED_PYTHON_UDF)) {
        case op if op.containsPattern(TRANSPILED_PYTHON_UDF) =>
          // Bottom-up, which is what makes the nested case work: dropping an inner call's dead
          // option resolves the inner node, and only then does the outer call -- whose argument IS
          // that node -- come into scope here.
          op.transformExpressionsUpWithPruning(_.containsPattern(TRANSPILED_PYTHON_UDF)) {
            // An unresolved argument means the query has a real error to report -- an unknown
            // column, say -- and falling back to Python here would bury it.
            case t: TranspiledPythonUDF if t.arguments.forall(_.resolved) &&
                (t.optionInputCategories.nonEmpty || !t.transpiledOptions.forall(_.resolved)) =>
              // Categories still set means ResolveTranspiledPythonUDFOptions never got to this
              // node: its guard needs every argument resolved, and an inner call holding an
              // unresolvable option is not. So do its work here rather than skip the node --
              // skipping leaves the parameters untyped, and CheckAnalysis reads `dataType` off one
              // and reports an internal error, which is the very thing this rule exists to avoid.
              // Coercion cannot run again from here, so an option body that still needs it stays
              // unresolved and is dropped just below: the call falls back to Python, which is the
              // safe direction.
              val typed = ResolveTranspiledPythonUDFOptions.pruneByCategoryAndTypeParameters(t)
              val (kept, dropped) = typed.transpiledOptions.partition(_.resolved)
              // Say so. The doc above argues this only happens where the transpiler emitted an
              // option it should not have, which makes a silent drop a bug that erases its own
              // evidence: the query quietly runs interpreted Python and nothing records why.
              // ConvertToCatalyst logs every one of its skip paths for the same reason.
              if (dropped.nonEmpty) {
                logWarning(log"Dropping ${MDC(LogKeys.COUNT, dropped.length)} transpiled " +
                  log"option(s) for Python UDF ${MDC(LogKeys.FUNCTION_NAME, t.name)} that " +
                  log"analysis left " +
                  log"unresolved; the call falls back to interpreted Python. This indicates the " +
                  log"transpiler emitted an option it cannot resolve. First one: " +
                  log"${MDC(LogKeys.EXPR, dropped.head.simpleString(maxFields = 100))}")
              }
              typed.copy(transpiledOptions = kept)
          }
      }
    }
  }
}
