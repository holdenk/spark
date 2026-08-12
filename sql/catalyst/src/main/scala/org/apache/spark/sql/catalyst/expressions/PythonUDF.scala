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

package org.apache.spark.sql.catalyst.expressions

import org.apache.spark.SparkException.internalError
import org.apache.spark.api.python.{PythonEvalType, PythonFunction}
import org.apache.spark.sql.catalyst.InternalRow
import org.apache.spark.sql.catalyst.analysis.UnresolvedException
import org.apache.spark.sql.catalyst.expressions.aggregate.{AggregateExpression, AggregateFunction}
import org.apache.spark.sql.catalyst.expressions.codegen.{CodegenContext, ExprCode}
import org.apache.spark.sql.catalyst.trees.TreePattern.{PYTHON_UDF, TRANSPILED_PYTHON_UDF,
  TreePattern}
import org.apache.spark.sql.catalyst.util.toPrettySQL
import org.apache.spark.sql.errors.{QueryCompilationErrors, QueryExecutionErrors}
import org.apache.spark.sql.types._

/**
 * Helper functions for [[PythonUDF]]
 */
object PythonUDF {
  private[this] val SCALAR_TYPES = Set(
    PythonEvalType.SQL_BATCHED_UDF,
    PythonEvalType.SQL_ARROW_BATCHED_UDF,
    // Element-wise UDFs are row-shaped from the plan's point of view: one array column in, one
    // array column out per row. They are extracted by `ExtractPythonUDFs` like any other scalar
    // UDF; only the Python worker treats them element-wise. One eval type per lifted flavor keeps
    // the worker's pandas- vs. Arrow-shaped batching and the iterator contract distinct.
    PythonEvalType.SQL_ARROW_ELEMENTWISE_UDF,
    PythonEvalType.SQL_SCALAR_PANDAS_ELEMENTWISE_UDF,
    PythonEvalType.SQL_SCALAR_PANDAS_ITER_ELEMENTWISE_UDF,
    PythonEvalType.SQL_SCALAR_ARROW_ELEMENTWISE_UDF,
    PythonEvalType.SQL_SCALAR_ARROW_ITER_ELEMENTWISE_UDF,
    PythonEvalType.SQL_SCALAR_PANDAS_UDF,
    PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF,
    PythonEvalType.SQL_SCALAR_ARROW_UDF,
    PythonEvalType.SQL_SCALAR_ARROW_ITER_UDF
  )

  def isScalarPythonUDF(e: Expression): Boolean = {
    e.isInstanceOf[PythonUDF] && SCALAR_TYPES.contains(e.asInstanceOf[PythonUDF].evalType)
  }

  /**
   * Whether `e` is a Python UDF that can be lifted out of a higher-order function's lambda by
   * `ExtractPythonUDFFromLambda`, which applies it to the whole array outside the lambda.
   *
   * Both the row-at-a-time eval types (plain and Arrow batched) and the vectorized scalar eval
   * types (scalar pandas / Arrow and their iterator variants) qualify: the rule lifts the UDF
   * structurally over `array<T>` arguments, and the Python worker flattens each array, invokes the
   * function on the flat element column with its own batching contract, and re-nests. See
   * [[liftedElementwiseEvalType]] for the mapping to the eval type the lifted UDF runs under.
   *
   * Otherwise-eligible shapes are excluded because the rewrite cannot preserve them:
   *   - a zero-argument call, `f()`: the lift turns each argument into an aligned array, so with no
   *     argument there is no array to carry the iterated shape, and the element-wise UDF would
   *     reach the worker with no input column and crash there instead of failing analysis;
   *   - a call with named arguments: its `NamedArgumentExpression` children would be buried inside
   *     the generated `ArrayTransform`, losing the kwargs mapping the runner derives from the
   *     direct children;
   *   - a UDF whose argument or return type involves a UDT: the lift forces an Arrow element-wise
   *     eval type, which has no UDT fallback (unlike `correctEvalType`'s Arrow -> pickle path), so
   *     it would fail at runtime instead of at analysis.
   * All keep the previous behavior (an analysis error) rather than being rewritten.
   *
   * This is shared with `CheckAnalysis` so that the shapes analysis accepts are exactly those the
   * optimizer rule can rewrite.
   */
  def isElementwiseRewritableUDF(e: Expression): Boolean = e match {
    case udf: PythonUDF =>
      isElementwiseRewritableEvalType(udf.evalType) &&
        udf.children.nonEmpty &&
        !udf.children.exists(_.isInstanceOf[NamedArgumentExpression]) &&
        !containsUDT(udf.dataType) &&
        !udf.children.exists(c => containsUDT(c.dataType))
    case _ => false
  }

  private def isElementwiseRewritableEvalType(evalType: Int): Boolean = evalType match {
    case PythonEvalType.SQL_BATCHED_UDF |
         PythonEvalType.SQL_ARROW_BATCHED_UDF |
         PythonEvalType.SQL_SCALAR_PANDAS_UDF |
         PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF |
         PythonEvalType.SQL_SCALAR_ARROW_UDF |
         PythonEvalType.SQL_SCALAR_ARROW_ITER_UDF => true
    case _ => false
  }

  /**
   * The eval type a rewritable UDF runs under once lifted out of the lambda. Each maps to the
   * element-wise flavor that preserves its worker contract: the row-at-a-time types share the one
   * pickle-based element-wise path, while each vectorized scalar type keeps its own pandas- vs.
   * Arrow-shaped batching and iterator behavior. `evalType` must satisfy
   * [[isElementwiseRewritableEvalType]].
   */
  def liftedElementwiseEvalType(evalType: Int): Int = evalType match {
    case PythonEvalType.SQL_BATCHED_UDF | PythonEvalType.SQL_ARROW_BATCHED_UDF =>
      PythonEvalType.SQL_ARROW_ELEMENTWISE_UDF
    case PythonEvalType.SQL_SCALAR_PANDAS_UDF =>
      PythonEvalType.SQL_SCALAR_PANDAS_ELEMENTWISE_UDF
    case PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF =>
      PythonEvalType.SQL_SCALAR_PANDAS_ITER_ELEMENTWISE_UDF
    case PythonEvalType.SQL_SCALAR_ARROW_UDF =>
      PythonEvalType.SQL_SCALAR_ARROW_ELEMENTWISE_UDF
    case PythonEvalType.SQL_SCALAR_ARROW_ITER_UDF =>
      PythonEvalType.SQL_SCALAR_ARROW_ITER_ELEMENTWISE_UDF
    case other =>
      throw internalError(s"Not a rewritable elementwise UDF eval type: $other")
  }

  /**
   * Whether every Python UDF in `hof`'s lambdas can be lifted out by `ExtractPythonUDFFromLambda`.
   * Used by `CheckAnalysis` to decide whether to reject the plan. These shapes cannot be rewritten:
   *   - a UDF in a *nested* lambda, `transform(arr, i -> transform(i, x -> f(x)))`: the inner array
   *     `i` is not a real column. (A UDF in a nested *argument*, `transform(arr, x ->
   *     transform(udf(x), y -> y))`, is fine - `udf(x)` lifts onto `arr`.)
   *   - a UDF in `aggregate` / `reduce`: the fold is sequential, so it sees earlier steps' outputs;
   *   - a *nondeterministic iterated argument*, `filter(shuffle(arr), x -> f(x))`: the rewrite
   *     references that argument several times (the carrier's `c0`, each lifted UDF's argument, the
   *     `map_keys`/`map_values` desugar, the pairwise `array_sort` path), and nondeterministic
   *     expressions are not subexpression-eliminated, so the copies would evaluate independently
   *     and disagree - keeping the results misaligned. (This is distinct from a nondeterministic
   *     UDF *call*, which `ExtractPythonUDFFromLambda.liftKey` keeps distinct but well-defined.)
   */
  def canRewritePythonUDFInLambda(hof: HigherOrderFunction): Boolean = {
    // Only row-at-a-time eval types are supported; a vectorized UDF is not rewritable regardless
    // of the enclosing function.
    val allUDFsRewritable = hof.functions.forall { f =>
      f.collect { case udf: PythonUDF => udf }.forall(isElementwiseRewritableUDF)
    }
    // Reading a free lambda variable means `hof` is itself nested in an enclosing lambda, so the
    // array it iterates is not a real column (the nested case above).
    val iteratesRealColumns = !hasFreeLambdaVariable(hof)
    // The rewrite duplicates the iterated argument expression, so a nondeterministic one would be
    // evaluated more than once with diverging results.
    val deterministicArguments = hof.arguments.forall(_.deterministic)
    allUDFsRewritable && iteratesRealColumns && deterministicArguments && isRewritableShape(hof)
  }

  /**
   * The structural assumption the rewrite makes: one lambda with plain-variable parameters, over at
   * least one array- or map-valued argument. `aggregate` / `reduce` fail this - they have two
   * lambdas (`merge`, `finish`) - so a UDF in a fold is rejected. Checking the shape rather than
   * listing classes means a new function of a familiar shape needs no change here.
   *
   * The function must also carry one of the result-type marker traits the rewrite dispatches on
   * ([[ResultTypeFromArgument]] or [[ResultTypeFromFunction]]). Every built-in single-lambda HOF is
   * marked today, but requiring it here keeps "analysis accepts exactly what the rule rewrites"
   * structural: a future HOF missing both traits is rejected at analysis rather than slipping
   * through and leaving the UDF inside the lambda at runtime.
   */
  private def isRewritableShape(hof: HigherOrderFunction): Boolean =
    hof.functions.length == 1 &&
      hof.functions.head.isInstanceOf[LambdaFunction] &&
      hof.functions.head.asInstanceOf[LambdaFunction].arguments
        .forall(_.isInstanceOf[NamedLambdaVariable]) &&
      (hof.isInstanceOf[ResultTypeFromArgument] || hof.isInstanceOf[ResultTypeFromFunction]) &&
      hof.arguments.exists { a =>
        a.dataType.isInstanceOf[ArrayType] || a.dataType.isInstanceOf[MapType]
      }

  /**
   * Whether `e` references a [[NamedLambdaVariable]] that it does not itself bind, i.e. one bound
   * by an enclosing lambda. Such an expression cannot be evaluated outside that lambda.
   *
   * Shared with `ExtractPythonUDFFromLambda` so the rule can re-check the nested-lambda guard on
   * its own, rather than relying only on `CheckAnalysis` having already rejected such plans.
   */
  def hasFreeLambdaVariable(e: Expression): Boolean = {
    def check(expr: Expression, bound: Set[ExprId]): Boolean = expr match {
      case LambdaFunction(function, arguments, _) =>
        check(function, bound ++ arguments.map(_.exprId))
      case v: NamedLambdaVariable => !bound.contains(v.exprId)
      case other => other.children.exists(check(_, bound))
    }
    check(e, Set.empty)
  }

  def isWindowPandasUDF(e: PythonFuncExpression): Boolean = {
    // This is currently only `PythonUDAF` (which means SQL_GROUPED_AGG_PANDAS_UDF or
    // SQL_GROUPED_AGG_ARROW_UDF), but we might
    // support new types in the future, e.g, N -> N transform.
    e.isInstanceOf[PythonUDAF]
  }

  def correctEvalType(udf: PythonUDF, pythonUDFArrowFallbackOnUDT: Boolean): Int = {
    if (udf.evalType == PythonEvalType.SQL_ARROW_BATCHED_UDF) {
      if (pythonUDFArrowFallbackOnUDT &&
        (containsUDT(udf.dataType) || udf.children.exists(expr => containsUDT(expr.dataType)))) {
        PythonEvalType.SQL_BATCHED_UDF
      } else {
        PythonEvalType.SQL_ARROW_BATCHED_UDF
      }
    } else {
      udf.evalType
    }
  }

  private def containsUDT(dataType: DataType): Boolean = dataType match {
    case _: UserDefinedType[_] => true
    case ArrayType(elementType, _) => containsUDT(elementType)
    case StructType(fields) => fields.exists(field => containsUDT(field.dataType))
    case MapType(keyType, valueType, _) => containsUDT(keyType) || containsUDT(valueType)
    case _ => false
  }
}


trait PythonFuncExpression extends NonSQLExpression with UserDefinedExpression { self: Expression =>
  def name: String
  def func: PythonFunction
  def evalType: Int
  def udfDeterministic: Boolean
  def resultId: ExprId

  override lazy val deterministic: Boolean = udfDeterministic && children.forall(_.deterministic)

  override def toString: String = s"$name(${children.mkString(", ")})#${resultId.id}$typeSuffix"

  override def nullable: Boolean = true
}


/**
 * Marks a subtree of a transpiled option as the argument spliced in for the UDF's `index`th
 * parameter.
 *
 * `UserDefinedPythonFunction`'s builder resolves the `_udf_param_N` placeholders at
 * call-construction time, which erases which copy came from which parameter -- and two parameters
 * can be bound to structurally equal arguments, so counting copies cannot recover it. Giving each
 * parameter a single evaluation needs that identity, so the builder tags the copies of any
 * parameter it splices in more than once and `ConvertToCatalyst` unwraps them all.
 *
 * A [[TaggingExpression]], so it is transparent: it evaluates as its child and a stray one is
 * harmless.
 */
case class TranspiledUDFParameter(child: Expression, index: Int) extends TaggingExpression {
  override protected def withNewChildInternal(newChild: Expression): TranspiledUDFParameter =
    copy(child = newChild)
}


object TranspiledUDFParameter {
  /**
   * Gives each tagged parameter of a transpiled option a single evaluation, like the Python UDF the
   * option replaces, whose eval operator computes one column per argument. Called by
   * `ConvertToCatalyst` once the option is resolved -- a [[CommonExpressionRef]] needs its
   * definition's type, so this cannot run at call-construction time where the tags are added.
   *
   * The parameters that need work are the ones the builder tagged because it spliced them in more
   * than once: the copies that belong together are the tags sharing an index, so each index becomes
   * one [[CommonExpressionDef]] and `RewriteWithExpression` pre-evaluates it in a Project below the
   * operator, re-inlining the cheap ones so plain column arguments leave the plan unchanged. An
   * unused parameter never made it into the option at all (so its argument is never evaluated,
   * unlike the Python path -- a deliberate difference), and a foldable one folds in at each use
   * site.
   *
   * Sharing never crosses parameters: two parameters are two columns to Python, so
   * `f(rand(1), rand(1))` owes the body two draws however identical the copies look, and an
   * argument nested inside another is evaluated again as part of the outer one.
   *
   * Hoisting also makes a shared argument eager, which changes when an ANSI error surfaces: with
   * `lambda x, y: (x + x) if y > 0 else 0.0` over `f(a / b, y)`, `a / b` used to be skipped on rows
   * where the branch was not taken and now raises for every row with `b = 0`. That is the
   * interpreted UDF's behaviour -- it computes every argument column -- so the change is towards
   * Python, not away from it, but it is a visible difference for queries that relied on the branch.
   *
   * The [[With]] wraps the whole option rather than each shared subtree, since one nested in a
   * conditional branch just gets inlined again -- a common expression can't be hoisted into an
   * always-evaluated Project from a branch that may not run.
   *
   * That last point, plus the aggregate guard above, leaves three shapes where a per-row
   * nondeterministic argument cannot be pinned to one evaluation per row with `With` alone:
   *
   *  - A parameter used exactly *once* stays inline, so when that use sits in a conditional branch
   *    the argument is only evaluated on the rows taking the branch (`lambda a, b: a if b > 0.5
   *    else 0.0` over `f(rand(1), rand(1))`: `a`'s draw advances only on rows where `b` passed).
   *    A definition would not fix it -- `RewriteWithExpression` inlines any definition holding a
   *    single ref, so forcing pre-evaluation needs a mechanism other than `With`.
   *  - When the UDF call itself sits in a conditional branch (`when(c, udf(rand()))`) the argument
   *    is likewise evaluated only on branch-taken rows, where the interpreted UDF (hoisted by
   *    `ExtractPythonUDFs`) computes it for every row; and a `With` hoisted there is inlined back
   *    into the branch, so even shared copies drift.
   *  - An aggregating option shares nothing (the guard above), so a repeated nondeterministic
   *    argument there keeps drifting.
   *
   * "Conditional branch" here also covers the short-circuited right operand of `and` / `or`, which
   * the transpiler emits and which is skipped at runtime just like a `when` branch.
   *
   * Rather than emit a silently-drifting option, `ConvertToCatalyst` detects these shapes via
   * [[hasUnsupportedNondeterministicInput]] and falls back to the interpreted Python UDF, which is
   * always correct. Closing them properly -- a single-evaluation mechanism that does not rely on
   * `With`, and a Project-below-aggregate hoist -- is future work. See also the nondeterminism TODO
   * in `RewriteWithExpression`.
   */
  def shareTaggedParameters(option: Expression): Expression = {
    val tags = tagsOf(option)
    if (tags.isEmpty) {
      return option
    }
    // Give each shareable index (see sharedIndices for the criteria and the aggregate/type
    // exclusions) one definition holding the raw argument; the copies that belong together are the
    // tags with that index.
    val byIndex = tags.groupBy(_.index)
    val shared = sharedIndices(option).toSeq.sorted.map { index =>
      index -> CommonExpressionDef(byIndex(index).head.child)
    }
    val refs = shared.map { case (index, d) => index -> new CommonExpressionRef(d) }.toMap
    // Unwrap every tag on the way out, shared or not -- the marker has no business surviving into
    // execution. The definitions hold the raw arguments: RewriteWithExpression only resolves refs
    // in the `With`'s child, so a ref inside a sibling definition would be left dangling.
    def unwrap(e: Expression): Expression = e match {
      case p: TranspiledUDFParameter => refs.getOrElse(p.index, p.child)
      case t: TranspiledPythonUDF => t
      case _ => e.mapChildren(unwrap)
    }
    if (shared.isEmpty) unwrap(option) else With(unwrap(option), shared.map(_._2))
  }

  /**
   * Whether `option` carries a per-row nondeterministic argument that its shared form (from
   * [[shareTaggedParameters]]) cannot pin to one evaluation per row, so `ConvertToCatalyst` should
   * fall back to the interpreted Python UDF instead of emitting a silently-drifting option. The
   * shapes, all documented on [[shareTaggedParameters]]:
   *
   *  - a nondeterministic argument left inline in a position that may be skipped at runtime -- a
   *    conditional branch or `and`/`or` short-circuit operand of the body, or anywhere in the
   *    option when the whole call sits in such a position (`inConditionalBranch`). Found by walking
   *    `shared` seeded with `inConditionalBranch`: every shared copy is a [[CommonExpressionRef]]
   *    by now -- a deterministic leaf -- so anything still nondeterministic under a skippable
   *    position is a genuinely inline argument, whether used once or repeated;
   *  - a repeated nondeterministic argument that sharing left inline anyway -- an aggregating
   *    option (which shares nothing) or, in principle, copies unresolved or disagreeing on type.
   *
   * A nondeterministic argument used once in an always-evaluated position, and any deterministic
   * argument, is safe and keeps transpiling. `shared` is [[shareTaggedParameters]]'s output for
   * `option`; the two agree on what was shared because both consult [[sharedIndices]].
   */
  def hasUnsupportedNondeterministicInput(
      option: Expression, shared: Expression, inConditionalBranch: Boolean): Boolean = {
    // A repeated nondeterministic parameter that sharing did NOT hoist into one definition stays
    // inline in every use, so its copies drift. That is every repeated nondeterministic parameter
    // in an aggregating option (which shares nothing) and, in principle, one whose copies are
    // unresolved or disagree on type. `exists` over the copies, not `head`: they need not be
    // structurally equal, so any nondeterministic copy makes the parameter unsafe.
    val shareable = sharedIndices(option)
    val unsharedRepeatedNondet = tagsOf(option).groupBy(_.index).exists {
      case (index, ps) =>
        ps.length > 1 && ps.exists(!_.child.deterministic) && !shareable.contains(index)
    }
    // A nondeterministic argument left inline in a position that may be skipped at runtime: a
    // conditional branch or short-circuit operand of the body, or -- when the whole call sits in
    // one (`inConditionalBranch`, so seed the walk with it) -- anywhere in the option, since a
    // `With` hoisted there is inlined back into the branch and its shared copies drift too. Shared
    // copies are deterministic refs by now, so only genuinely inline nondeterministic arguments
    // trip this; a nested TranspiledPythonUDF is skipped, its inputs handled when the rule recurses
    // into it. A node is the nondeterministic one when it is itself nondeterministic with
    // deterministic children; otherwise recurse so a nondeterministic descendant is found with the
    // branch context that reaches it.
    def nondetInBranch(e: Expression, inBranch: Boolean): Boolean = e match {
      case _: TranspiledPythonUDF => false
      case n if inBranch && !n.deterministic && n.children.forall(_.deterministic) => true
      case _ =>
        val branchChildren = conditionallyEvaluatedChildren(e)
        e.children.exists { child =>
          nondetInBranch(child, inBranch || branchChildren.exists(_.eq(child)))
        }
    }
    nondetInBranch(shared, inBranch = inConditionalBranch) || unsharedRepeatedNondet
  }

  // Stop at a nested TranspiledPythonUDF: its options carry tags for their own call's parameters,
  // which are handled when ConvertToCatalyst recurses into it, and whose indexes would otherwise
  // collide with ours.
  private def tagsOf(e: Expression): Seq[TranspiledUDFParameter] = e match {
    case p: TranspiledUDFParameter => Seq(p)
    case _: TranspiledPythonUDF => Nil
    case _ => e.children.flatMap(tagsOf)
  }

  // Indices of parameters that shareTaggedParameters hoists into one CommonExpressionDef: repeated
  // (more than one copy), all copies resolved, and agreeing on (dataType, nullable) so a single ref
  // can stand in. Two deliberate exclusions leave a parameter inline, and this is the single source
  // of truth both shareTaggedParameters and hasUnsupportedNondeterministicInput consult:
  //  - an aggregating option shares nothing: `With` forbids a common expression ref inside a
  //    same-scope AggregateExpression and RewriteWithExpression asserts on it rather than skewing a
  //    value, so a transpiled grouped-agg UDF keeps every input inline;
  //  - copies that are unresolved or disagree on type: the tags say which copies are one parameter
  //    (we do not require structural equality -- analysis reseeds each `expr("rand()")` copy
  //    independently, and demanding equality would skip the very case sharing exists for), but a
  //    ref carries one dataType/nullability, so when the copies disagree none can stand in.
  private def sharedIndices(option: Expression): Set[Int] = {
    if (option.exists(_.isInstanceOf[AggregateExpression])) {
      Set.empty
    } else {
      tagsOf(option).groupBy(_.index).collect {
        case (index, ps) if ps.length > 1 && ps.forall(_.child.resolved) &&
            ps.map(p => (p.child.dataType, p.child.nullable)).distinct.length == 1 => index
      }.toSet
    }
  }

  // Children of `e` that may be skipped at runtime, so a nondeterministic argument spliced into one
  // is not guaranteed one evaluation per row. `ConditionalExpression` (If / CaseWhen / Coalesce /
  // NaNvl) names its always-run children via `alwaysEvaluatedInputs`; every other child it holds is
  // a branch. `And` / `Or` are not `ConditionalExpression`s but short-circuit their right operand,
  // and the transpiler lowers Python `and` / `or` to them, so treat that operand as a branch too.
  // Identity (`eq`) is deliberate: `alwaysEvaluatedInputs` returns the very child instances, and it
  // beats `semanticEquals`, which would wrongly clear the flag for `If(c, x, x)`. The one hole -- a
  // single instance reused in both an always-run and a branch position -- is not reachable from the
  // built-in transpiler, which builds each option's subtrees fresh.
  private[sql] def conditionallyEvaluatedChildren(e: Expression): Seq[Expression] = e match {
    case c: ConditionalExpression =>
      c.children.filterNot(child => c.alwaysEvaluatedInputs.exists(_.eq(child)))
    case And(_, right) => Seq(right)
    case Or(_, right) => Seq(right)
    case _ => Nil
  }
}


case class TranspiledPythonUDF(
  name: String,
  pythonUDFExpr: Expression,
  transpiledOptions: List[Expression],
  // Per-option input-type categories ("numeric"/"string" per public param),
  // parallel to `transpiledOptions`. ResolveTranspiledPythonUDFOptions prunes the
  // options to those whose categories match the resolved input types (before
  // CheckAnalysis can reject a type-incompatible option) and clears this field;
  // ConvertToCatalyst then picks the first survivor or falls back to the Python
  // UDF. Empty means "no restriction" (kept as-is).
  optionInputCategories: List[List[String]] = Nil) extends Expression with Unevaluable {
  require(
    optionInputCategories.isEmpty || optionInputCategories.length == transpiledOptions.length,
    s"optionInputCategories (${optionInputCategories.length}) must be parallel to " +
    s"transpiledOptions (${transpiledOptions.length}) or empty"
  )
  override def children: Seq[Expression] = pythonUDFExpr +: transpiledOptions
  override def dataType: DataType = pythonUDFExpr.dataType
  override def nullable: Boolean = pythonUDFExpr.nullable
  override protected def withNewChildrenInternal(newChildren: IndexedSeq[Expression]):
      TranspiledPythonUDF =
    copy(pythonUDFExpr = newChildren.head, transpiledOptions = newChildren.tail.toList)
  final override val nodePatterns: Seq[TreePattern] = Seq(TRANSPILED_PYTHON_UDF)

  // True when every direct input to pythonUDFExpr is a plain PythonUDF (not a
  // TranspiledPythonUDF). Used to decide whether to preserve the UDF batch pipeline
  // rather than inserting a Catalyst node in the middle of a Python UDF chain.
  def hasOnlyPythonUDFInputs: Boolean =
    pythonUDFExpr.children.nonEmpty &&
    pythonUDFExpr.children.forall {
      _.isInstanceOf[PythonUDF]
    }
}

/**
 * A serialized version of a Python lambda function. This is a special expression, which needs a
 * dedicated physical operator to execute it, and thus can't be pushed down to data sources.
 */
case class PythonUDF(
    name: String,
    func: PythonFunction,
    dataType: DataType,
    children: Seq[Expression],
    evalType: Int,
    udfDeterministic: Boolean,
    resultId: ExprId = NamedExpression.newExprId)
  extends Expression with PythonFuncExpression with Unevaluable {

  lazy val resultAttribute: Attribute = AttributeReference(toPrettySQL(this), dataType, nullable)(
    exprId = resultId)

  override lazy val canonicalized: Expression = {
    val canonicalizedChildren = children.map(_.canonicalized)
    // `resultId` can be seen as cosmetic variation in PythonUDF, as it doesn't affect the result.
    this.copy(resultId = ExprId(-1)).withNewChildren(canonicalizedChildren)
  }

  final override val nodePatterns: Seq[TreePattern] = Seq(PYTHON_UDF)

  override protected def withNewChildrenInternal(newChildren: IndexedSeq[Expression]): PythonUDF =
    copy(children = newChildren)
}

abstract class UnevaluableAggregateFunc extends AggregateFunction {
  override def aggBufferSchema: StructType = throw internalError(
    "UnevaluableAggregateFunc.aggBufferSchema should not be called.")
  override def aggBufferAttributes: Seq[AttributeReference] = throw internalError(
    "UnevaluableAggregateFunc.aggBufferAttributes should not be called.")
  override def inputAggBufferAttributes: Seq[AttributeReference] = throw internalError(
    "UnevaluableAggregateFunc.inputAggBufferAttributes should not be called.")
  final override def eval(input: InternalRow = null): Any =
    throw QueryExecutionErrors.cannotEvaluateExpressionError(this)
  final override protected def doGenCode(ctx: CodegenContext, ev: ExprCode): ExprCode =
    throw QueryExecutionErrors.cannotGenerateCodeForExpressionError(this)
}

/**
 * A serialized version of a Python lambda function for aggregation. This is a special expression,
 * which needs a dedicated physical operator to execute it, instead of the normal Aggregate
 * operator.
 */
case class PythonUDAF(
    name: String,
    func: PythonFunction,
    dataType: DataType,
    children: Seq[Expression],
    udfDeterministic: Boolean,
    evalType: Int = PythonEvalType.SQL_GROUPED_AGG_PANDAS_UDF,
    resultId: ExprId = NamedExpression.newExprId)
  extends UnevaluableAggregateFunc with PythonFuncExpression {

  override def sql(isDistinct: Boolean): String = {
    val distinct = if (isDistinct) "DISTINCT " else ""
    s"$name($distinct${children.mkString(", ")})"
  }

  override def toAggString(isDistinct: Boolean): String = {
    val start = if (isDistinct) "(distinct " else "("
    name + children.mkString(start, ", ", ")") + s"#${resultId.id}$typeSuffix"
  }

  override lazy val canonicalized: Expression = {
    val canonicalizedChildren = children.map(_.canonicalized)
    // `resultId` can be seen as cosmetic variation in PythonUDAF, as it doesn't affect the result.
    this.copy(resultId = ExprId(-1)).withNewChildren(canonicalizedChildren)
  }

  final override val nodePatterns: Seq[TreePattern] = Seq(PYTHON_UDF)

  override protected def withNewChildrenInternal(newChildren: IndexedSeq[Expression]): PythonUDAF =
    copy(children = newChildren)
}

abstract class UnevaluableGenerator extends Generator {
  final override def eval(input: InternalRow): IterableOnce[InternalRow] =
    throw QueryExecutionErrors.cannotEvaluateExpressionError(this)

  final override protected def doGenCode(ctx: CodegenContext, ev: ExprCode): ExprCode =
    throw QueryExecutionErrors.cannotGenerateCodeForExpressionError(this)
}

/**
 * A serialized version of a Python table-valued function call. This is a special expression,
 * which needs a dedicated physical operator to execute it.
 * @param name name of the Python UDTF being called
 * @param func string contents of the Python code in the UDTF, along with other environment state
 * @param elementSchema result schema of the function call
 * @param pickledAnalyzeResult if the UDTF defined an 'analyze' method, this contains the pickled
 *                             'AnalyzeResult' instance from that method, which contains all
 *                             metadata returned including the result schema of the function call as
 *                             well as optional other information
 * @param children input arguments to the UDTF call; for scalar arguments these are the expressions
 *                 themeselves, and for TABLE arguments, these are instances of
 *                 [[FunctionTableSubqueryArgumentExpression]]
 * @param evalType identifies whether this is a scalar or aggregate or table function, using an
 *                 instance of the [[PythonEvalType]] enumeration
 * @param udfDeterministic true if this function is deterministic wherein it returns the same result
 *                         rows for every call with the same input arguments
 * @param resultId unique expression ID for this function invocation
 * @param pythonUDTFPartitionColumnIndexes holds the zero-based indexes of the projected results of
 *                                         all PARTITION BY expressions within the TABLE argument of
 *                                         the Python UDTF call, if applicable
 * @param tableArguments holds whether an input argument is a table argument
 */
case class PythonUDTF(
    name: String,
    func: PythonFunction,
    elementSchema: StructType,
    pickledAnalyzeResult: Option[Array[Byte]],
    children: Seq[Expression],
    evalType: Int,
    udfDeterministic: Boolean,
    resultId: ExprId = NamedExpression.newExprId,
    pythonUDTFPartitionColumnIndexes: Option[PythonUDTFPartitionColumnIndexes] = None,
    tableArguments: Option[Seq[Boolean]] = None)
  extends UnevaluableGenerator with PythonFuncExpression {

  override lazy val canonicalized: Expression = {
    val canonicalizedChildren = children.map(_.canonicalized)
    // `resultId` can be seen as cosmetic variation in PythonUDTF, as it doesn't affect the result.
    this.copy(resultId = ExprId(-1)).withNewChildren(canonicalizedChildren)
  }

  override protected def withNewChildrenInternal(newChildren: IndexedSeq[Expression]): PythonUDTF =
    copy(children = newChildren)
}

/**
 * Holds the indexes of the TABLE argument to a Python UDTF call, if applicable.
 * @param partitionChildIndexes The indexes of the partitioning columns in each TABLE argument.
 */
case class PythonUDTFPartitionColumnIndexes(partitionChildIndexes: Seq[Int])

/**
 * A placeholder of a polymorphic Python table-valued function.
 */
case class UnresolvedPolymorphicPythonUDTF(
    name: String,
    func: PythonFunction,
    children: Seq[Expression],
    evalType: Int,
    udfDeterministic: Boolean,
    resolveElementMetadata: (PythonFunction, Seq[Expression]) => PythonUDTFAnalyzeResult,
    resultId: ExprId = NamedExpression.newExprId,
    tableArguments: Option[Seq[Boolean]] = None)
  extends UnevaluableGenerator with PythonFuncExpression {

  override lazy val resolved = false

  override def elementSchema: StructType = throw new UnresolvedException("elementSchema")

  override protected def withNewChildrenInternal(
      newChildren: IndexedSeq[Expression]): UnresolvedPolymorphicPythonUDTF =
    copy(children = newChildren)
}

/**
 * Represents the result of invoking the polymorphic 'analyze' method on a Python user-defined table
 * function. This returns the table function's output schema in addition to other optional metadata.
 *
 * @param schema result schema of this particular function call in response to the particular
 *               arguments provided, including the types of any provided scalar arguments (and
 *               their values, in the case of literals) as well as the names and types of columns of
 *               the provided TABLE argument (if any)
 * @param withSinglePartition true if the 'analyze' method explicitly indicated that the UDTF call
 *                            should consume all rows of the input TABLE argument in a single
 *                            instance of the UDTF class, in which case Catalyst will invoke a
 *                            repartitioning to a separate stage with a single worker for this
 *                            purpose
 * @param partitionByExpressions if non-empty, this contains the list of column names that the
 *                               'analyze' method explicitly indicated that the UDTF call should
 *                               partition the input table by, wherein all rows corresponding to
 *                               each unique combination of values of the partitioning columns are
 *                               consumed by exactly one unique instance of the UDTF class
 * @param orderByExpressions if non-empty, this contains the list of ordering items that the
 *                           'analyze' method explicitly indicated that the UDTF call should consume
 *                           the input table rows by
 * @param selectedInputExpressions If non-empty, this is a list of expressions that the UDTF is
 *                                 specifying for Catalyst to evaluate against the columns in the
 *                                 input TABLE argument. In this case, Catalyst will insert a
 *                                 projection to evaluate these expressions and return the result to
 *                                 the UDTF. The UDTF then receives one input column for each
 *                                 expression in the list, in the order they are listed.
 * @param pickledAnalyzeResult this is the pickled 'AnalyzeResult' instance from the UDTF, which
 *                             contains all metadata returned by the Python UDTF 'analyze' method
 *                             including the result schema of the function call as well as optional
 *                             other information
 */
case class PythonUDTFAnalyzeResult(
    schema: StructType,
    withSinglePartition: Boolean,
    partitionByExpressions: Seq[Expression],
    orderByExpressions: Seq[SortOrder],
    selectedInputExpressions: Seq[PythonUDTFSelectedExpression],
    pickledAnalyzeResult: Array[Byte]) {
  /**
   * Applies the requested properties from this analysis result to the target TABLE argument
   * expression of a UDTF call, throwing an error if any properties of the UDTF call are
   * incompatible.
   */
  def applyToTableArgument(
      pythonUDTFName: String,
      t: FunctionTableSubqueryArgumentExpression): FunctionTableSubqueryArgumentExpression = {
    if (withSinglePartition && partitionByExpressions.nonEmpty) {
      throw QueryCompilationErrors.tableValuedFunctionRequiredMetadataInvalid(
        functionName = pythonUDTFName,
        reason = "the 'with_single_partition' field cannot be assigned to true " +
          "if the 'partition_by' list is non-empty")
    }
    if (orderByExpressions.nonEmpty && !withSinglePartition && partitionByExpressions.isEmpty) {
      throw QueryCompilationErrors.tableValuedFunctionRequiredMetadataInvalid(
        functionName = pythonUDTFName,
        reason = "the 'order_by' field cannot be non-empty unless the " +
          "'with_single_partition' field is set to true or the 'partition_by' list " +
          "is non-empty")
    }
    if ((withSinglePartition || partitionByExpressions.nonEmpty) && t.hasRepartitioning) {
      throw QueryCompilationErrors
        .tableValuedFunctionRequiredMetadataIncompatibleWithCall(
          functionName = pythonUDTFName,
          requestedMetadata =
            "specified its own required partitioning of the input table",
          invalidFunctionCallProperty =
            "specified the WITH SINGLE PARTITION or PARTITION BY clause; " +
              "please remove these clauses and retry the query again.")
    }
    var newWithSinglePartition = t.withSinglePartition
    var newPartitionByExpressions = t.partitionByExpressions
    var newOrderByExpressions = t.orderByExpressions
    var newSelectedInputExpressions = t.selectedInputExpressions
    if (withSinglePartition) {
      newWithSinglePartition = true
    }
    if (partitionByExpressions.nonEmpty) {
      newPartitionByExpressions = partitionByExpressions
    }
    if (orderByExpressions.nonEmpty) {
      newOrderByExpressions = orderByExpressions
    }
    if (selectedInputExpressions.nonEmpty) {
      newSelectedInputExpressions = selectedInputExpressions
    }
    t.copy(
      withSinglePartition = newWithSinglePartition,
      partitionByExpressions = newPartitionByExpressions,
      orderByExpressions = newOrderByExpressions,
      selectedInputExpressions = newSelectedInputExpressions)
  }
}

/**
 * Represents an expression that the UDTF is specifying for Catalyst to evaluate against the
 * columns in the input TABLE argument. The UDTF then receives one input column for each expression
 * in the list, in the order they are listed.
 *
 * @param expression the expression that the UDTF is specifying for Catalyst to evaluate against the
 *                   columns in the input TABLE argument
 * @param alias If present, this is the alias for the column or expression as visible from the
 *              UDTF's 'eval' method. This is required if the expression is not a simple column
 *              reference.
 */
case class PythonUDTFSelectedExpression(expression: Expression, alias: Option[String])

/**
 * A place holder used when printing expressions without debugging information such as the
 * result id.
 */
case class PrettyPythonUDF(
    name: String,
    dataType: DataType,
    children: Seq[Expression])
  extends UnevaluableAggregateFunc with NonSQLExpression {

  override def toString: String = s"$name(${children.mkString(", ")})"

  override def sql(isDistinct: Boolean): String = {
    val distinct = if (isDistinct) "DISTINCT " else ""
    s"$name($distinct${children.mkString(", ")})"
  }

  override def toAggString(isDistinct: Boolean): String = {
    val start = if (isDistinct) "(distinct " else "("
    name + children.mkString(start, ", ", ")")
  }

  override def nullable: Boolean = true

  override protected def withNewChildrenInternal(
    newChildren: IndexedSeq[Expression]): PrettyPythonUDF = copy(children = newChildren)
}
