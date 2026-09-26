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

import org.apache.spark.api.python.PythonEvalType
import org.apache.spark.sql.catalyst.QueryPlanningTracker
import org.apache.spark.sql.catalyst.dsl.expressions._
import org.apache.spark.sql.catalyst.expressions.{Add, Alias, Cast, Concat, Expression, Literal, PythonUDF, TranspiledPythonUDF, TranspiledUDFParameter}
import org.apache.spark.sql.catalyst.plans.logical.{LocalRelation, LogicalPlan, Project}
import org.apache.spark.sql.types.{DataType, LongType, StringType}

/**
 * Unit tests for [[ResolveTranspiledPythonUDFOptions]], which prunes a
 * TranspiledPythonUDF's per-input-type options to those whose declared categories match the
 * resolved argument types, and for [[DropUnresolvedTranspiledPythonUDFOptions]], which drops the
 * ones analysis never resolved. func=null in the leaf PythonUDF is intentional: these structural
 * tests don't execute Python.
 */
class ResolveTranspiledPythonUDFOptionsSuite extends AnalysisTest {

  private def pyUDF(children: Seq[Expression], returnType: DataType = LongType): PythonUDF =
    PythonUDF("udf", null, returnType, children,
      PythonEvalType.SQL_BATCHED_UDF, udfDeterministic = true)

  // Runs the rule on a Project that wraps the node, and returns the (possibly pruned) node.
  private def prune(node: TranspiledPythonUDF, rel: LocalRelation): TranspiledPythonUDF = {
    val rewritten = ResolveTranspiledPythonUDFOptions(Project(Seq(Alias(node, "r")()), rel))
    theNodeIn(rewritten)
  }

  // Both rules, in the order the analyzer runs them: categories first, then the drop.
  private def pruneAndDrop(node: TranspiledPythonUDF, rel: LocalRelation): TranspiledPythonUDF = {
    val plan = Project(Seq(Alias(node, "r")()), rel)
    theNodeIn(DropUnresolvedTranspiledPythonUDFOptions(ResolveTranspiledPythonUDFOptions(plan)))
  }

  private def analyzeAndPrune(
      node: TranspiledPythonUDF, rel: LocalRelation): TranspiledPythonUDF = {
    theNodeIn(getAnalyzer.executeAndCheck(
      Project(Seq(Alias(node, "r")()), rel), new QueryPlanningTracker))
  }

  private def theNodeIn(plan: LogicalPlan): TranspiledPythonUDF =
    plan.expressions.flatMap(_.collect { case t: TranspiledPythonUDF => t }).head

  test("keeps the numeric option for numeric columns and drops the string one") {
    val a = $"a".long
    val b = $"b".long
    val numericOpt = Add(a, b)
    val stringOpt = Concat(Seq(a, b))
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a, b)), List(numericOpt, stringOpt),
      List(List("numeric", "numeric"), List("string", "string")))
    val pruned = prune(node, LocalRelation(a, b))
    assert(pruned.transpiledOptions == List(numericOpt))
    assert(pruned.optionInputCategories.isEmpty)
  }

  test("keeps the string option for string columns and drops the numeric one") {
    val a = $"a".string
    val b = $"b".string
    val numericOpt = Add(a, b)
    val stringOpt = Concat(Seq(a, b))
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a, b)), List(numericOpt, stringOpt),
      List(List("numeric", "numeric"), List("string", "string")))
    val pruned = prune(node, LocalRelation(a, b))
    assert(pruned.transpiledOptions == List(stringOpt))
    assert(pruned.optionInputCategories.isEmpty)
  }

  test("empties the options when no category set matches (falls back to Python UDF)") {
    val a = $"a".string
    val b = $"b".long
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a, b)),
      List(Add(a, b), Concat(Seq(a, b))),
      List(List("numeric", "numeric"), List("string", "string")))
    val pruned = prune(node, LocalRelation(a, b))
    assert(pruned.transpiledOptions.isEmpty)
    assert(pruned.optionInputCategories.isEmpty)
  }

  test("matches binary columns against neither category (string is StringType only)") {
    val a = $"a".binary
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a)),
      List(Concat(Seq(a, a))), List(List("string")))
    val pruned = prune(node, LocalRelation(a))
    assert(pruned.transpiledOptions.isEmpty)
  }

  test("leaves options untouched when categories are empty (no restriction)") {
    val a = $"a".long
    val onlyOpt = Add(a, Literal(1L))
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a)), List(onlyOpt), Nil)
    val pruned = prune(node, LocalRelation(a))
    assert(pruned.transpiledOptions == List(onlyOpt))
  }

  test("category pruning keeps an option that is still unresolved, and types it") {
    // Every real option is born unresolved: a `_udf_param_N` reference has no type until this rule
    // reads one off the bound argument. Checking `resolved` here would drop the lot and turn
    // transpilation off, so the check belongs in DropUnresolvedTranspiledPythonUDFOptions.
    val a = $"a".long
    val option = Cast(Add(TranspiledUDFParameter(0), Literal(1L)), LongType)
    assert(!option.resolved)
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a)), List(option), List(List("numeric")))
    val pruned = prune(node, LocalRelation(a))
    assert(pruned.transpiledOptions.length == 1)
    assert(pruned.transpiledOptions.head.resolved)
  }

  test("the drop rule leaves a node alone while its categories are still set") {
    // The category rule owns that state and clears it; dropping options against a list still
    // parallel to them would trip the node's `require`.
    val a = $"a".binary
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a)), List(Cast(a, LongType)),
      List(List("binary")))
    val plan = Project(Seq(Alias(node, "r")()), LocalRelation(a))
    val untouched = theNodeIn(DropUnresolvedTranspiledPythonUDFOptions(plan))
    assert(untouched.transpiledOptions.length == 1)
    assert(untouched.optionInputCategories == List(List("binary")))
  }

  test("drops an option that fails to resolve even when its category matches") {
    val a = $"a".binary
    val neverResolves = Cast(a, LongType)
    assert(!neverResolves.resolved)
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a)), List(neverResolves),
      List(List("binary")))
    val pruned = pruneAndDrop(node, LocalRelation(a))
    assert(pruned.transpiledOptions.isEmpty)
    assert(pruned.optionInputCategories.isEmpty)
  }

  test("keeps a resolved option and drops a sibling that does not resolve") {
    val a = $"a".binary
    val good = Cast(a, StringType)
    val bad = Cast(a, LongType)
    assert(good.resolved && !bad.resolved)
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a), StringType), List(good, bad),
      List(List("binary"), List("binary")))
    val pruned = pruneAndDrop(node, LocalRelation(a))
    assert(pruned.transpiledOptions == List(good))
    assert(pruned.optionInputCategories.isEmpty)
  }

  test("full analysis drops an option that can never resolve instead of failing") {
    val a = $"a".binary
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a)), List(Cast(a, LongType)),
      List(List("binary")))
    val analyzed = analyzeAndPrune(node, LocalRelation(a))
    assert(analyzed.transpiledOptions.isEmpty)
  }

  test("full analysis keeps an option that is only resolved by a later rule") {
    val a = $"a".string
    val needsFunctionResolution = Cast(
      UnresolvedFunction(Seq("concat"), Seq(a, Literal("x")), isDistinct = false), StringType)
    assert(!needsFunctionResolution.resolved)
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a), StringType),
      List(needsFunctionResolution), List(List("string")))
    val analyzed = analyzeAndPrune(node, LocalRelation(a))
    assert(analyzed.transpiledOptions.length == 1)
    assert(analyzed.transpiledOptions.head.resolved)
  }

  test("full analysis keeps an option that is only resolved by type coercion") {
    val a = $"a".string
    val needsCoercion = Cast(Concat(Seq(a, Literal(1L))), StringType)
    assert(!needsCoercion.resolved)
    val node = TranspiledPythonUDF("udf", pyUDF(Seq(a), StringType), List(needsCoercion),
      List(List("string")))
    val analyzed = analyzeAndPrune(node, LocalRelation(a))
    assert(analyzed.transpiledOptions.length == 1)
    assert(analyzed.transpiledOptions.head.resolved)
  }
}
