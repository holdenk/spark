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

  private def theNodeIn(plan: LogicalPlan): TranspiledPythonUDF = nodesIn(plan).head

  // Outermost first, which is the order `collect` yields for a nested call.
  private def nodesIn(plan: LogicalPlan): Seq[TranspiledPythonUDF] =
    plan.expressions.flatMap(_.collect { case t: TranspiledPythonUDF => t })

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

  test("the drop rule prunes by category itself when the categories are still set") {
    // This used to skip such a node, to avoid dropping options while they were still parallel to a
    // category list and tripping the node's `require`. That left a nested call's parameters untyped
    // and CheckAnalysis calling it an internal error (SPARK-59090). It now does the category prune,
    // which clears the list and satisfies the `require`, and the category semantics still apply:
    // a "string" option over a binary column is not a match and goes.
    val a = $"a".binary
    val mismatched = TranspiledPythonUDF("udf", pyUDF(Seq(a)), List(Concat(Seq(a, a))),
      List(List("string")))
    val pruned = theNodeIn(DropUnresolvedTranspiledPythonUDFOptions(
      Project(Seq(Alias(mismatched, "r")()), LocalRelation(a))))
    assert(pruned.optionInputCategories.isEmpty)
    assert(pruned.transpiledOptions.isEmpty)

    // And taking over that job does not make it over-eager: a matching option whose parameter it
    // types itself resolves, so it survives.
    val b = $"b".long
    val matching = TranspiledPythonUDF("udf", pyUDF(Seq(b)),
      List(Cast(Add(TranspiledUDFParameter(0), Literal(1L)), LongType)), List(List("numeric")))
    val kept = theNodeIn(DropUnresolvedTranspiledPythonUDFOptions(
      Project(Seq(Alias(matching, "r")()), LocalRelation(b))))
    assert(kept.optionInputCategories.isEmpty)
    assert(kept.transpiledOptions.length == 1)
    assert(kept.transpiledOptions.head.resolved)
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

  test("nested call: an unresolvable inner option does not strand the outer's parameters") {
    // SPARK-59090 regression. The outer call's argument IS the inner TranspiledPythonUDF, so while
    // the inner holds an unresolvable option the outer is unresolved too and
    // ResolveTranspiledPythonUDFOptions -- which needs every argument resolved -- never fires on
    // it. Its categories stay set and its `_udf_param_N` references stay untyped. Skipping such a
    // node here left CheckAnalysis to read `dataType` off an untyped parameter and call it an
    // internal error: the exact failure this rule removes.
    val a = $"a".binary
    val inner = TranspiledPythonUDF("g", pyUDF(Seq(a)), List(Cast(a, LongType)),
      List(List("binary")))
    val outer = TranspiledPythonUDF("f", pyUDF(Seq(inner)),
      List(Cast(Add(TranspiledUDFParameter(0), Literal(1L)), LongType)), List(List("numeric")))
    val analyzed = getAnalyzer.executeAndCheck(
      Project(Seq(Alias(outer, "r")()), LocalRelation(a)), new QueryPlanningTracker)
    val nodes = nodesIn(analyzed)
    // First, or the `forall`s below pass vacuously on an empty result and the failure arrives as a
    // MatchError that says nothing about nothing having been transpiled.
    assert(nodes.length == 2, s"expected both calls to survive analysis, got: $nodes")
    assert(nodes.forall(_.resolved), "every node must be resolved once analysis finishes")
    assert(nodes.forall(_.optionInputCategories.isEmpty), "categories must be cleared")
    assert(nodes.forall(_.transpiledOptions.forall(_.resolved)),
      "no unresolved option may survive to CheckAnalysis")
    // By name rather than position: both are here, and which is which should not ride on the
    // traversal order `collect` happens to use.
    val analyzedInner = nodes.filter(_.name == "g").head
    val analyzedOuter = nodes.filter(_.name == "f").head
    // The inner's only option cannot resolve, so it falls back to Python; the outer's can, once its
    // parameter is typed from the inner's return type, so it survives.
    assert(analyzedInner.transpiledOptions.isEmpty)
    assert(analyzedOuter.transpiledOptions.length == 1)
  }

  test("nested call: an option needing function resolution is dropped where a flat call keeps it") {
    // What the nested path costs is not type coercion alone. The transpiler emits `concat`, `upper`
    // and friends as UnresolvedFunction -- see "only resolved by a later rule" below -- and
    // ResolveFunctions is in the Resolution batch too. Behind a call whose own option cannot
    // resolve, the outer never gets that pass, so a perfectly good option is dropped and the call
    // runs interpreted Python, while the identical option over a plain column survives. Master
    // errors on both, so this is not a regression, but nothing pinned the difference.
    val a = $"a".binary
    // A def, so each plan gets its own instance rather than sharing one tree.
    def option: Expression = Cast(
      UnresolvedFunction(Seq("upper"), Seq(TranspiledUDFParameter(0)), isDistinct = false),
      StringType)

    // Nested: `f(g(a))`, where g's only option is an impossible cast.
    val inner = TranspiledPythonUDF("g", pyUDF(Seq(a), StringType), List(Cast(a, LongType)),
      List(List("binary")))
    val outer = TranspiledPythonUDF("f", pyUDF(Seq(inner), StringType), List(option),
      List(List("string")))
    val nested = nodesIn(getAnalyzer.executeAndCheck(
      Project(Seq(Alias(outer, "r")()), LocalRelation(a)), new QueryPlanningTracker))
    assert(nested.length == 2, s"expected both calls to survive analysis, got: $nested")
    assert(nested.forall(_.resolved))
    assert(nested.filter(_.name == "f").head.transpiledOptions.isEmpty,
      "the outer option needed ResolveFunctions, which no longer runs, so it is dropped")

    // Flat: the same option over a plain string column keeps it.
    val s = $"s".string
    val flatCall = TranspiledPythonUDF("f", pyUDF(Seq(s), StringType), List(option),
      List(List("string")))
    val flat = theNodeIn(getAnalyzer.executeAndCheck(
      Project(Seq(Alias(flatCall, "r")()), LocalRelation(s)), new QueryPlanningTracker))
    assert(flat.transpiledOptions.length == 1)
    assert(flat.transpiledOptions.head.resolved)
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
