#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Unit tests for UDF transpilation.

These were previously interleaved with the broader UDF mixin in
``test_udf.py``. They are split out because UDF transpilation is currently
only supported in regular (non-Connect) Spark, so they should not be
inherited into the Spark Connect parity test class. The companion
property-based suite lives in ``test_udf_transpile_hypothesis.py``.
"""

import unittest

from pyspark.sql import Row
from pyspark.sql.types import (
    BinaryType,
    BooleanType,
    DoubleType,
    LongType,
    StringType,
)
from pyspark.sql.udf import UserDefinedFunction
from pyspark.testing.sqlutils import ReusedSQLTestCase
from pyspark.util import is_remote_only


# Both flags must be on for the transpiler to attempt a rewrite (at UDF
# construction time and again in the optimizer); ANSI is required because
# transpilation targets ANSI semantics.
_TRANSPILE_ON = {
    "spark.sql.experimental.optimizer.transpilePyUDFs": True,
    "spark.sql.ansi.enabled": True,
}


@unittest.skipIf(
    is_remote_only(),
    "UDF transpilation is only supported in regular (non-Connect) Spark.",
)
class UDFTranspileUnitTests(ReusedSQLTestCase):
    def test_udf_transpile_basic(self):
        # Test callable object
        class PlusFour:
            def __call__(self, col):
                return col + 4

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            # Make sure we can transpile the object
            call = PlusFour()
            pudf = UserDefinedFunction(call, LongType())
            self.assertTrue(pudf.transpiled)
            # Now make sure we can run the transpiled UDF*
            input_df = self.spark.createDataFrame([Row(a=1)])
            transformed_df = input_df.select(pudf("a"))
            [row] = transformed_df.collect()
            self.assertEqual(row[0], 5)

        with self.sql_conf({"spark.sql.experimental.optimizer.transpilePyUDFs": False}):
            call = PlusFour()
            pudf = UserDefinedFunction(call, LongType())
            self.assertEqual([], pudf.transpiled)
            # Now make sure we can run the UDF
            input_df = self.spark.createDataFrame([Row(a=1)])
            transformed_df = input_df.select(pudf("a"))
            [row] = transformed_df.collect()
            self.assertEqual(row[0], 5)

    def test_udf_transpile_with_nones(self):
        # Test callable object
        class PlusFour:
            def __call__(self, col):
                if col is not None:
                    return col + 4

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            # Make sure we can transpile the object
            call = PlusFour()
            pudf = UserDefinedFunction(call, LongType())
            self.assertTrue(pudf.transpiled)
            # Now make sure we can run the transpiled UDF*
            input_df = self.spark.createDataFrame([Row(a=1)])
            transformed_df = input_df.select(pudf("a").alias("result"))
            [row] = transformed_df.collect()
            self.assertEqual(row[0], 5)
            physical_plan = transformed_df._jdf.queryExecution().executedPlan().toString()
            self.assertNotIn("UDF", physical_plan)

        with self.sql_conf({"spark.sql.experimental.optimizer.transpilePyUDFs": False}):
            call = PlusFour()
            pudf = UserDefinedFunction(call, LongType())
            self.assertEqual([], pudf.transpiled)
            # Now make sure we can run the UDF
            input_df = self.spark.createDataFrame([Row(a=1)])
            transformed_df = input_df.select(pudf("a").alias("result"))
            [row] = transformed_df.collect()
            self.assertEqual(row[0], 5)
            physical_plan = transformed_df._jdf.queryExecution().executedPlan().toString()
            self.assertIn("UDF", physical_plan)

    def test_udf_not_transpilable(self):
        class UnsupportedEx:
            def __call__(self, col):
                if col is not None:
                    return col in "4"

        with self.sql_conf({"spark.sql.experimental.optimizer.transpilePyUDFs": True}):
            call = UnsupportedEx()
            pudf = UserDefinedFunction(call, BooleanType())
            self.assertEqual([], pudf.transpiled)

    def test_udf_transpile_requires_ansi(self):
        # Transpilation targets ANSI semantics. With ANSI off the transpiler
        # must skip rewriting (and warn the user) so we don't silently
        # diverge from the Python interpretation; with ANSI on it should
        # produce a Catalyst expression.
        import warnings

        def plus_four(x):
            if x is not None:
                return x + 4

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": False,
            }
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                pudf = UserDefinedFunction(plus_four, LongType())
            self.assertEqual([], pudf.transpiled)
            ansi_warnings = [w for w in caught if "ANSI mode" in str(w.message)]
            self.assertTrue(
                ansi_warnings,
                "expected an 'ANSI mode' warning when transpilation is "
                "requested but ANSI is disabled",
            )

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            pudf = UserDefinedFunction(plus_four, LongType())
            self.assertTrue(
                pudf.transpiled,
                "expected transpilation to produce a Catalyst expression "
                "when both transpilePyUDFs and ANSI mode are enabled",
            )

    def test_udf_transpile_falls_back_for_unsupported_patterns(self):
        # The transpiler intentionally only handles a small subset of
        # Python AST today. Everything outside that subset must
        # gracefully fall back to interpreted Python (with an empty
        # `transpiled` list and a UserWarning) rather than break the
        # UDF -- the "don't break people's Spark code" promise. This test
        # walks the most common unsupported shapes, registers each as a
        # UDF with transpilation on, and asserts (a) construction does
        # not raise, (b) `transpiled == []`, (c) the UDF still produces
        # the correct interpreted result.

        def divide_by_two(x):  # `/` -- ast.Div, not handled.
            if x is not None:
                return x / 2

        def floor_divide_by_two(x):  # `//` -- ast.FloorDiv, not handled.
            if x is not None:
                return x // 2

        def bit_and_one(x):  # `&` -- ast.BitAnd, not handled.
            if x is not None:
                return x & 1

        def bit_or_one(x):  # `|` -- ast.BitOr, not handled.
            if x is not None:
                return x | 1

        def left_shift(x):  # `<<` -- ast.LShift, not handled.
            if x is not None:
                return x << 1

        def multi_statement(x):  # > 1 top-level statement, not handled.
            y = 1
            return x + y if x is not None else 0

        def func_closure_capture(x):
            offset = 7
            if x is not None:
                return x + offset

        cases = [
            ("divide_by_two", divide_by_two, DoubleType(), Row(a=4.0), 2.0),
            ("floor_divide_by_two", floor_divide_by_two, LongType(), Row(a=5), 2),
            ("bit_and_one", bit_and_one, LongType(), Row(a=5), 1),
            ("bit_or_one", bit_or_one, LongType(), Row(a=4), 5),
            ("left_shift", left_shift, LongType(), Row(a=3), 6),
            ("multi_statement", multi_statement, LongType(), Row(a=5), 6),
            ("func_closure_capture", func_closure_capture, LongType(), Row(a=10), 17),
        ]

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            for label, func, return_type, row, expected in cases:
                with self.subTest(case=label):
                    import warnings as _warnings

                    with _warnings.catch_warnings(record=True) as caught_warnings:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, return_type)
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{label}: transpiler should not produce a Catalyst "
                        "expression for this AST shape",
                    )
                    fallback = [
                        w
                        for w in caught_warnings
                        if "Unable to transpile" in str(w.message)
                        or "Errors encountered" in str(w.message)
                        or "Exception transpiling" in str(w.message)
                    ]
                    self.assertTrue(
                        fallback,
                        f"{label}: expected a fallback warning when the "
                        "transpiler can't lower the function",
                    )
                    df = self.spark.createDataFrame([row])
                    [result] = df.select(pudf("a")).collect()
                    self.assertEqual(
                        result[0],
                        expected,
                        f"{label}: interpreted UDF result diverged from expected",
                    )

    def test_udf_transpile_boolean_and_or_lowered(self):
        # When `and`/`or` operands are syntactically boolean (Compare
        # results in this case), the transpiler should lower to `&`/`|`,
        # which short-circuit like Python, and produce results matching
        # the interpreted UDF.
        # Each UDF is a single top-level statement (the transpiler
        # doesn't support multi-statement bodies yet).
        from pyspark.sql.types import StructField, StructType

        def both_positive(x, y):
            return x > 0 and y > 0

        def either_positive(x, y):
            return x > 0 or y > 0

        schema = StructType(
            [
                StructField("a", LongType(), nullable=True),
                StructField("b", LongType(), nullable=True),
            ]
        )

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            # A NULL operand makes the lowered `>` raise (mirroring Python's
            # TypeError), unless an earlier operand already short-circuited the
            # result. We only assert on non-NULL inputs here; the NULL handling
            # itself is covered by test_udf_transpile_boolop_short_circuits and
            # the hypothesis suite.
            for func, x, y, expected in [
                (both_positive, 1, 2, True),
                (both_positive, 1, -1, False),
                (both_positive, -1, -1, False),
                (either_positive, -1, 2, True),
                (either_positive, -1, -1, False),
                (either_positive, 1, 1, True),
            ]:
                with self.subTest(func=func.__name__, x=x, y=y):
                    pudf = UserDefinedFunction(func, BooleanType())
                    self.assertTrue(
                        pudf.transpiled,
                        f"{func.__name__}: bool-typed and/or should transpile",
                    )
                    df = self.spark.createDataFrame([Row(a=x, b=y)], schema=schema)
                    [row] = df.select(pudf("a", "b")).collect()
                    self.assertEqual(row[0], expected)

    def test_udf_transpile_less_than_zero(self):
        # Restored from the unsupported-patterns matrix: now that the
        # transpiler handles ast.Lt, `x < 0` should lower to a Catalyst
        # expression and match interpreted Python. The ``is not None``
        # guard short-circuits None inputs through the else branch, so
        # the comparison itself never sees a NULL in this UDF.
        from pyspark.sql.types import StructField, StructType

        def less_than_zero(x):
            if x is not None:
                return x < 0

        schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            pudf = UserDefinedFunction(less_than_zero, BooleanType())
            self.assertTrue(pudf.transpiled, "less_than_zero should now transpile")
            for value, expected in [(-1, True), (0, False), (5, False), (None, None)]:
                with self.subTest(value=value):
                    df = self.spark.createDataFrame([Row(a=value)], schema=schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertEqual(row[0], expected)

    def test_udf_transpile_compare_with_none_raises(self):
        # When a comparison's operand is NULL in Spark, Python would have
        # raised TypeError ('>' not supported between NoneType and int).
        # The transpiler wraps Compare ops with a raise_error guard so
        # the rewritten plan fails loudly instead of silently producing
        # NULL three-valued-logic results.
        from pyspark.sql.types import StructField, StructType

        def gt_zero(x):
            return x > 0

        schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            pudf = UserDefinedFunction(gt_zero, BooleanType())
            self.assertTrue(pudf.transpiled, "gt_zero should transpile")
            df = self.spark.createDataFrame([Row(a=None)], schema=schema)
            with self.assertRaises(Exception) as ctx:
                df.select(pudf("a")).collect()
            self.assertIn("cannot compare NULL", str(ctx.exception))

    def test_udf_transpile_eq_none_semantics(self):
        # Python ``==``/``!=`` differ from Spark's three-valued NULL equality:
        # in Python ``None == None`` is ``True`` and ``None == 0`` is ``False``,
        # whereas SQL ``NULL = NULL`` and ``NULL = 0`` both yield ``NULL``. The
        # transpiler's ``_lower_eq`` reproduces Python's semantics; this test
        # exercises every arm of that logic.
        from pyspark.sql.types import StructField, StructType

        def x_eq_zero(x):
            if x is not None:
                return x == 0
            else:
                return None

        def x_neq_zero(x):
            if x is not None:
                return x != 0
            else:
                return None

        def x_eq_y(x, y):
            return x == y

        def x_neq_y(x, y):
            return x != y

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        two_col_schema = StructType(
            [
                StructField("a", LongType(), nullable=True),
                StructField("b", LongType(), nullable=True),
            ]
        )
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            # Single-arg ``x == 0`` / ``x != 0`` with a None guard.
            pudf_eq = UserDefinedFunction(x_eq_zero, BooleanType())
            pudf_neq = UserDefinedFunction(x_neq_zero, BooleanType())
            self.assertTrue(pudf_eq.transpiled, "x == 0 should transpile")
            self.assertTrue(pudf_neq.transpiled, "x != 0 should transpile")
            for value, eq_expected, neq_expected in [
                (0, True, False),
                (1, False, True),
                (-3, False, True),
                (None, None, None),
            ]:
                with self.subTest(value=value):
                    df = self.spark.createDataFrame([Row(a=value)], schema=long_schema)
                    [row_eq] = df.select(pudf_eq("a")).collect()
                    [row_neq] = df.select(pudf_neq("a")).collect()
                    self.assertEqual(row_eq[0], eq_expected)
                    self.assertEqual(row_neq[0], neq_expected)

            # Two-arg ``x == y`` / ``x != y`` exercising every NULL combination.
            pudf_eq_xy = UserDefinedFunction(x_eq_y, BooleanType())
            pudf_neq_xy = UserDefinedFunction(x_neq_y, BooleanType())
            self.assertTrue(pudf_eq_xy.transpiled, "x == y should transpile")
            self.assertTrue(pudf_neq_xy.transpiled, "x != y should transpile")
            # Python semantics:
            #   None == None -> True;     None != None -> False
            #   None == 0    -> False;    None != 0    -> True
            #   0    == None -> False;    0    != None -> True
            #   1    == 1    -> True;     1    != 1    -> False
            #   1    == 2    -> False;    1    != 2    -> True
            for x, y, eq_expected, neq_expected in [
                (None, None, True, False),
                (None, 0, False, True),
                (0, None, False, True),
                (1, 1, True, False),
                (1, 2, False, True),
            ]:
                with self.subTest(x=x, y=y):
                    df = self.spark.createDataFrame([Row(a=x, b=y)], schema=two_col_schema)
                    [row_eq] = df.select(pudf_eq_xy("a", "b")).collect()
                    [row_neq] = df.select(pudf_neq_xy("a", "b")).collect()
                    self.assertEqual(row_eq[0], eq_expected, f"({x} == {y})")
                    self.assertEqual(row_neq[0], neq_expected, f"({x} != {y})")

    def test_udf_transpile_lte_gte(self):
        # ``<=`` and ``>=`` go through the same ``_lower_value_compare`` path
        # as ``<`` / ``>`` (and so share the NULL-raises-TypeError guard), but
        # the entry points are not exercised elsewhere. Cover both with a None
        # guard so the comparison only sees non-NULL operands here.
        from pyspark.sql.types import StructField, StructType

        def lte_zero(x):
            if x is not None:
                return x <= 0

        def gte_zero(x):
            if x is not None:
                return x >= 0

        schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            pudf_lte = UserDefinedFunction(lte_zero, BooleanType())
            pudf_gte = UserDefinedFunction(gte_zero, BooleanType())
            self.assertTrue(pudf_lte.transpiled, "x <= 0 should transpile")
            self.assertTrue(pudf_gte.transpiled, "x >= 0 should transpile")
            for value, lte_expected, gte_expected in [
                (-1, True, False),
                (0, True, True),
                (1, False, True),
                (None, None, None),
            ]:
                with self.subTest(value=value):
                    df = self.spark.createDataFrame([Row(a=value)], schema=schema)
                    [row_lte] = df.select(pudf_lte("a")).collect()
                    [row_gte] = df.select(pudf_gte("a")).collect()
                    self.assertEqual(row_lte[0], lte_expected)
                    self.assertEqual(row_gte[0], gte_expected)

    def test_udf_transpile_chain_short_circuits(self):
        # THE test for the chained lowering. Python's chain short-circuits: when
        # ``a < b`` is false, ``b < c`` is never evaluated, so a NULL ``c`` cannot
        # raise. `&` short-circuits at evaluation time too, so the fold matches.
        # Nullable *columns* are required: a literal `None` has no column
        # category, so `1 < 0 < None` just falls back.
        #
        # Both of `And`'s two laziness implementations are exercised: `doGenCode`
        # under the default FALLBACK factory mode, and `eval` under NO_CODEGEN.
        # `spark.sql.codegen.wholeStage=false` is NOT enough for the latter -- it
        # disables whole-stage fusion, but expressions are still compiled through
        # the codegen factory, so `doGenCode` runs either way. Survival through the
        # OPTIMIZER is separate; see test_udf_transpile_boolop_short_circuits.
        def chained(a, b, c):
            return a < b < c

        schema = "a long, b long, c long"
        for factory_mode in ("FALLBACK", "NO_CODEGEN"):
            conf = dict(_TRANSPILE_ON)
            conf["spark.sql.codegen.factoryMode"] = factory_mode
            with self.subTest(factory_mode=factory_mode), self.sql_conf(conf):
                pudf = UserDefinedFunction(chained, BooleanType())
                self.assertTrue(pudf.transpiled, "3-column chain should transpile")
                df = self.spark.createDataFrame([(1, 0, None)], schema)
                [row] = df.select(pudf("a", "b", "c")).collect()
                self.assertEqual(
                    False,
                    row[0],
                    "first link is false, so the NULL second link must not be evaluated",
                )
                # ... but when the first link IS true the second link is reached
                # and its NULL guard fires, exactly as Python raises TypeError.
                df = self.spark.createDataFrame([(0, 1, None)], schema)
                with self.assertRaises(Exception) as ctx:
                    df.select(pudf("a", "b", "c")).collect()
                self.assertIn("cannot compare NULL", str(ctx.exception))

    def test_udf_transpile_boolop_short_circuits(self):
        # The `and` / `or` counterpart of the chain short-circuit: a NULL guard in
        # the second operand must not fire when the first already decided the
        # result.
        #
        # RED UNTIL `RaiseError` overrides `Expression.throwable` -- deliberately,
        # to pin the prerequisite. Without the override,
        # `splitConjunctivePredicates` decomposes the `And` and
        # `PushPredicateThroughJoin` pushes the guard to one side of the join,
        # where it fires on rows Python short-circuits past and raises
        # USER_RAISED_EXCEPTION instead of returning False.
        #
        # The join case disables AQE and broadcast on purpose: by default whether
        # the pushed-down guard fires races AQE eliminating the empty join, so the
        # failure would be intermittent rather than a clean red.
        def both_positive(a, c):
            return a > 0 and c > 0

        def either_positive(a, c):
            return a > 0 or c > 0

        with self.sql_conf(_TRANSPILE_ON):
            and_udf = UserDefinedFunction(both_positive, BooleanType())
            or_udf = UserDefinedFunction(either_positive, BooleanType())
            self.assertTrue(and_udf.transpiled, "and over compares should transpile")
            self.assertTrue(or_udf.transpiled, "or over compares should transpile")

            # Plain select: the second operand is NULL but the first already
            # decided the result, so Python's answer must come back unraised.
            df = self.spark.createDataFrame([(-5, None)], "a long, c long")
            self.assertEqual([False], [r[0] for r in df.select(and_udf("a", "c")).collect()])
            df = self.spark.createDataFrame([(5, None)], "a long, c long")
            self.assertEqual([True], [r[0] for r in df.select(or_udf("a", "c")).collect()])

        # Same thing across a join, where the NULL operand lives entirely on the
        # right side. Deterministic plan: no AQE, no broadcast, so the pushed-down
        # guard either exists or does not, rather than racing the join.
        deterministic = dict(_TRANSPILE_ON)
        deterministic["spark.sql.adaptive.enabled"] = False
        deterministic["spark.sql.autoBroadcastJoinThreshold"] = -1
        with self.sql_conf(deterministic):
            and_udf = UserDefinedFunction(both_positive, BooleanType())
            left = self.spark.createDataFrame([(1, -5)], "k long, a long")
            right = self.spark.createDataFrame([(1, None)], "k long, c long")
            joined = left.join(right, "k")
            self.assertEqual([False], [r[0] for r in joined.select(and_udf("a", "c")).collect()])

            # Filtering must return Python's answer rather than raising. RED
            # until `RaiseError` overrides `Expression.throwable`; see above.
            filtered = joined.filter(and_udf("a", "c"))
            self.assertEqual([], filtered.select("k").collect())

    def test_udf_transpile_chain_operator_mix_and_eq_nulls(self):
        # Mixed operators in one chain, and `x == y == z` over every NULL pattern.
        # Equality never raises in Python (`None == None` is True, `None == 0` is
        # False), so every pattern has a defined answer to match exactly.
        B = BooleanType()
        chained_eq = lambda a, b, c: a == b == c  # noqa: E731

        # (a, b, c, expected) -- every NULL pattern plus value cases.
        eq_cases = [
            (None, None, None, True),
            (None, None, 1, False),
            (None, 1, None, False),
            (None, 1, 2, False),
            (1, None, None, False),
            (1, None, 2, False),
            (1, 2, None, False),
            (1, 1, None, False),
            (1, 1, 1, True),
            (1, 1, 2, False),
            (1, 2, 3, False),
        ]
        self.assertEqual(
            self._vals(chained_eq, B, "a long, b long, c long", [c[:3] for c in eq_cases]),
            [c[3] for c in eq_cases],
            "a == b == c over the NULL patterns",
        )

        # Operator mixes, on non-NULL rows so the ordering guards stay quiet.
        lt_eq = lambda a, b, c: a < b == c  # noqa: E731
        eq_lt = lambda a, b, c: a == b < c  # noqa: E731
        gt_gte = lambda a, b, c: a > b >= c  # noqa: E731
        lte_lte = lambda a, b, c: a <= b <= c  # noqa: E731
        four_ops = lambda a, b, c, d, e: a < b < c < d < e  # noqa: E731
        for label, func, rows, expected in [
            ("a < b == c", lt_eq, [(1, 2, 2), (1, 2, 3), (2, 1, 1)], [True, False, False]),
            ("a == b < c", eq_lt, [(1, 1, 2), (1, 1, 0), (1, 2, 3)], [True, False, False]),
            ("a > b >= c", gt_gte, [(3, 2, 2), (3, 2, 5), (1, 2, 2)], [True, False, False]),
            ("a <= b <= c", lte_lte, [(1, 1, 2), (1, 2, 2), (2, 1, 3)], [True, True, False]),
        ]:
            with self.subTest(func=label):
                self.assertEqual(
                    self._vals(func, B, "a long, b long, c long", rows), expected, label
                )
        self.assertEqual(
            self._vals(
                four_ops,
                B,
                "a long, b long, c long, d long, e long",
                [(1, 2, 3, 4, 5), (1, 2, 3, 5, 4)],
            ),
            [True, False],
            "a chain of four operators is well under the cap and must transpile",
        )

    def test_udf_transpile_comparison_fallbacks(self):
        # Comparison forms the transpiler must REFUSE. Each case asserts both that
        # nothing transpiled and that a query using the UDF still returns Python's
        # answer. The second half matters because an option that survives is
        # type-checked by CheckAnalysis as a child of TranspiledPythonUDF: a
        # half-refused form would fail the whole query rather than fall back, so
        # "did not transpile" alone is not enough to prove the fallback works.
        import warnings as _warnings

        # `in` / `not in` are refused wholesale (SPARK-56925), so these cases all
        # take the same branch; they are kept because the reasons differ. `in` on a
        # str is substring containment rather than equality, a container of runtime
        # values would need runtime NULL analysis, and `in` can appear as a link in
        # a chain (`a < b in (1, 2)` is one Compare), which the refusal also covers.
        str_in = lambda x: x in "abc"  # noqa: E731
        runtime_in = lambda x, y: x in (y, 2)  # noqa: E731
        chained_in = lambda a, b: a < b in (1, 2)  # noqa: E731
        cross_chain = lambda x: 0 < x < "z"  # noqa: E731
        chain_long_rt = lambda x: 0 < x < 10  # noqa: E731
        in_long_rt = lambda x: x in (1, 2)  # noqa: E731

        # (label, func, schema, rows, expected). `expected` is what interpreted
        # Python produces, so the query must still run and return it.
        cases = [
            ("`in` on a str", str_in, "a string", [("b",), ("z",)], [True, False]),
            ("runtime `in`", runtime_in, "a long, b long", [(1, 1), (3, 5)], [True, False]),
            ("chain + `in`", chained_in, "a long, b long", [(0, 1), (1, 0)], [True, False]),
        ]
        with self.sql_conf(_TRANSPILE_ON):
            for label, func, schema, rows, expected in cases:
                with self.subTest(func=label):
                    with _warnings.catch_warnings(record=True):
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, BooleanType())
                    self.assertEqual([], pudf.transpiled, f"{label} must NOT transpile")
                    df = self.spark.createDataFrame(rows, schema)
                    self.assertEqual(
                        [r[0] for r in df.select(pudf(*df.columns)).collect()],
                        expected,
                        f"{label}: interpreted result diverged",
                    )
            # `0 < x < "z"` raises in interpreted Python too (cross-type ordering),
            # so assert only the fallback for it. And a chained / `in` body is
            # boolean, so a non-BooleanType declared return type must drop every
            # variant rather than emit a cast the interpreted path never performs.
            for func, rt, label in [
                (cross_chain, BooleanType(), "a cross-category chain"),
                (chain_long_rt, LongType(), "a chain with a LongType return type"),
                (in_long_rt, LongType(), "an `in` with a LongType return type"),
            ]:
                with self.subTest(label=label):
                    with _warnings.catch_warnings(record=True):
                        _warnings.simplefilter("always")
                        self.assertEqual(
                            [],
                            UserDefinedFunction(func, rt).transpiled,
                            f"{label} must NOT transpile",
                        )

    def test_udf_transpile_boolean_body_requires_boolean_return_type(self):
        # A comparison body is boolean whatever its operands look like, so it must
        # not satisfy a numeric declared return type. `_is_definitely_boolean` says
        # False for a comparison over a ternary / comparison / boolean op (it also
        # demands basic-type operands), and those used to fall through `_category`'s
        # numeric catch-all: `_transpile_from_ast` then accepted `LongType` and the
        # option returned `cast(<boolean> as bigint)` -- 1 or 0 -- where the
        # interpreted path returns NULL. `_category` now classifies every
        # `ast.Compare` as "bool" directly.
        import warnings as _warnings

        def ternary_compare(c, x, y):
            return (x if c > 0 else y) == 1

        def eq_true(a, b):
            # The same gap via `_lower_eq`; pre-dates chained comparisons.
            return (a < b) == True  # noqa: E712

        def chain_of_compare(a, b, c):
            # ... and via a chained comparison over comparison operands.
            return (a < b) <= (b < c)

        cases = [
            (ternary_compare, "c long, x long, y long", [(1, 1, 9)]),
            (eq_true, "a long, b long", [(1, 2)]),
            (chain_of_compare, "a long, b long, c long", [(1, 2, 3)]),
        ]
        with self.sql_conf(_TRANSPILE_ON):
            for func, schema, rows in cases:
                with self.subTest(func=func.__name__):
                    with _warnings.catch_warnings(record=True):
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, LongType())
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{func.__name__}: a boolean body must not satisfy LongType",
                    )
                    # The interpreted path returns NULL for a bool declared
                    # LongType; falling back reproduces that faithfully.
                    df = self.spark.createDataFrame(rows, schema)
                    self.assertEqual(
                        [None],
                        [r[0] for r in df.select(pudf(*df.columns)).collect()],
                        f"{func.__name__}: interpreted result diverged",
                    )
            # Declared BooleanType, the same bodies transpile and are correct.
            df = self.spark.createDataFrame([(1, 1, 9)], "c long, x long, y long")
            pudf = UserDefinedFunction(ternary_compare, BooleanType())
            self.assertTrue(pudf.transpiled, "ternary_compare should transpile for BooleanType")
            self.assertEqual([True], [r[0] for r in df.select(pudf("c", "x", "y")).collect()])

    def test_udf_transpile_lowering_caps(self):
        # The chain cap bounds plan growth. Exercise the boundary directly rather
        # than through a UDF, so the refusal cannot be confused with a fallback for
        # some other reason.
        import ast as _ast

        from pyspark.errors import UnsupportedOperationException
        from pyspark.sql.transpile import (
            CatalystTranspiler,
            _MAX_COMPARISON_OPS,
        )

        transpiler = CatalystTranspiler()
        transpiler._param_categories = {0: "numeric"}

        def compare_node(src):
            return _ast.parse(src, mode="eval").body

        def chain_src(n):
            # n operators over n + 1 operands: x < 0 < 1 < ... < n - 1
            return " < ".join(["x"] + [str(i) for i in range(n)])

        with self.sql_conf(_TRANSPILE_ON):
            # At the cap the lowering succeeds.
            transpiler._convert_chunk(["x"], compare_node(chain_src(_MAX_COMPARISON_OPS)))

            # One past it, it refuses so the UDF falls back.
            with self.assertRaises(UnsupportedOperationException) as ctx:
                transpiler._convert_chunk(["x"], compare_node(chain_src(_MAX_COMPARISON_OPS + 1)))
            self.assertIn("operators", str(ctx.exception))

            # `in` / `not in` are refused outright, deferred to SPARK-56925.
            with self.assertRaises(UnsupportedOperationException) as ctx:
                transpiler._convert_chunk(["x"], compare_node("x in (1, 2)"))
            self.assertIn("`in` / `not in`", str(ctx.exception))

    def test_udf_transpile_multi_row(self):
        # Every other transpile test uses a 1-row DataFrame; this one runs
        # the same arithmetic transpile on a multi-row input to catch any
        # column-reference / batch-boundary bug that single-row tests can't.
        from pyspark.sql.types import StructField, StructType

        def plus_four(x):
            if x is not None:
                return x + 4

        schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            pudf = UserDefinedFunction(plus_four, LongType())
            self.assertTrue(pudf.transpiled)
            inputs = [Row(a=v) for v in [-3, -1, 0, 1, 7, None, 100]]
            df = self.spark.createDataFrame(inputs, schema=schema)
            transformed_df = df.select(pudf("a").alias("result"))
            rows = transformed_df.collect()
            actual = [row[0] for row in rows]
            expected = [None if v is None else v + 4 for v in [-3, -1, 0, 1, 7, None, 100]]
            self.assertEqual(actual, expected)
            # Plan should also have the UDF stripped under the rewrite.
            physical_plan = transformed_df._jdf.queryExecution().executedPlan().toString()
            self.assertNotIn("UDF", physical_plan)

    def test_udf_transpile_falls_back_for_non_boolean_short_circuit(self):
        # Python's `x or 0` returns x if truthy else 0; Spark's `|` is
        # bitwise, so we'd silently produce wrong results. The transpiler
        # must refuse, fall back to interpreted Python, and still produce
        # the correct result.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def or_zero(x):
            return x or 0

        def and_one(x):
            return x and 1

        def not_int(x):
            return not 0 + x  # operand is BinOp, statically non-boolean

        long_schema = StructType([StructField("a", LongType(), nullable=True)])

        cases = [
            ("or_zero", or_zero, LongType(), long_schema, Row(a=5), 5),
            ("or_zero_none", or_zero, LongType(), long_schema, Row(a=None), 0),
            ("and_one", and_one, LongType(), long_schema, Row(a=5), 1),
            ("and_one_zero", and_one, LongType(), long_schema, Row(a=0), 0),
            ("not_int", not_int, BooleanType(), long_schema, Row(a=0), True),
            ("not_int_nonzero", not_int, BooleanType(), long_schema, Row(a=3), False),
        ]
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            for label, func, return_type, schema, row, expected in cases:
                with self.subTest(case=label):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, return_type)
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{label}: non-boolean and/or/not must NOT be lowered",
                    )
                    fallback = [
                        w
                        for w in caught
                        if "Unable to transpile" in str(w.message)
                        or "Errors encountered" in str(w.message)
                    ]
                    self.assertTrue(fallback, f"{label}: expected a fallback warning")
                    df = self.spark.createDataFrame([row], schema=schema)
                    [result] = df.select(pudf("a")).collect()
                    self.assertEqual(result[0], expected, f"{label}: interpreted mismatch")

    def test_udf_transpile_falls_back_for_bare_truthiness_test(self):
        # A bare `if x:` applied to a non-boolean column cannot be soundly
        # lowered: Python truthiness is type-dependent (0, "", [], None are
        # falsy) and the transpiler has no input type information at this
        # point. Emitting coalesce(x, false) either fails Spark analysis for
        # non-boolean columns or silently produces wrong answers.  The
        # transpiler must refuse and fall back to interpreted Python.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def truthy_int(x):
            if x:
                return x
            return -1

        def truthy_string(x):
            return x if x else "default"

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        str_schema = StructType([StructField("a", StringType(), nullable=True)])

        cases = [
            ("truthy_int_zero", truthy_int, LongType(), long_schema, Row(a=0), -1),
            ("truthy_int_nonzero", truthy_int, LongType(), long_schema, Row(a=3), 3),
            ("truthy_string_empty", truthy_string, StringType(), str_schema, Row(a=""), "default"),
            ("truthy_string_val", truthy_string, StringType(), str_schema, Row(a="hi"), "hi"),
        ]

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            for label, func, return_type, schema, row, expected in cases:
                with self.subTest(case=label):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, return_type)
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{label}: bare truthiness test must NOT be lowered to Catalyst",
                    )
                    fallback = [
                        w
                        for w in caught
                        if "Unable to transpile" in str(w.message)
                        or "Errors encountered" in str(w.message)
                    ]
                    self.assertTrue(fallback, f"{label}: expected a fallback warning")
                    df = self.spark.createDataFrame([row], schema=schema)
                    [result] = df.select(pudf("a")).collect()
                    self.assertEqual(result[0], expected, f"{label}: interpreted mismatch")

    def test_udf_transpile_falls_back_for_mismatched_branch_types(self):
        # An if/ternary whose two branches produce different Spark categories
        # (e.g. numeric vs string) would lower to a CASE WHEN whose branch
        # values share no common type under ANSI. That node is carried as a
        # child of the TranspiledPythonUDF and is type-checked by CheckAnalysis
        # before ConvertToCatalyst can drop it, so without a guard the whole
        # query would fail analysis instead of falling back. The transpiler must
        # refuse and run the UDF as interpreted Python.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def mixed_ternary(x):
            return 1 if x > 0 else "neg"

        def mixed_if(x):
            # Single top-level `if`/`else` so the If-statement lowering path
            # (not the "more than one statement" fallback) exercises the guard.
            if x > 0:
                return "pos"
            else:
                return x

        # Positive control: matching-category branches must still transpile, so
        # the guard does not over-refuse.
        def homogeneous(x):
            return x if x > 0 else 0

        long_schema = StructType([StructField("a", LongType(), nullable=True)])

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            # Inputs are chosen to take the string-returning branch so the
            # interpreted result is unambiguous.
            mismatch_cases = [
                ("mixed_ternary", mixed_ternary, Row(a=-3), "neg"),
                ("mixed_if", mixed_if, Row(a=10), "pos"),
            ]
            for label, func, row, expected in mismatch_cases:
                with self.subTest(case=label):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, StringType())
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{label}: mismatched branch types must NOT be lowered to Catalyst",
                    )
                    fallback = [w for w in caught if "Unable to transpile" in str(w.message)]
                    self.assertTrue(fallback, f"{label}: expected a fallback warning")
                    df = self.spark.createDataFrame([row], schema=long_schema)
                    # Must run without an analysis failure and match interpreted Python.
                    [result] = df.select(pudf("a")).collect()
                    self.assertEqual(result[0], expected, f"{label}: interpreted mismatch")

            with self.subTest(case="homogeneous"):
                pudf = UserDefinedFunction(homogeneous, LongType())
                self.assertNotEqual(
                    [],
                    pudf.transpiled,
                    "matching-category branches must still transpile",
                )
                df = self.spark.createDataFrame([Row(a=5), Row(a=-3)], schema=long_schema)
                results = [r[0] for r in df.select(pudf("a")).collect()]
                self.assertEqual(results, [5, 0], "homogeneous branch result mismatch")

    def test_udf_transpile_falls_back_for_cross_category_eq(self):
        # `x == True` on a numeric column would lower to `x = true`, which
        # fails ANSI analysis (BIGINT vs BOOLEAN) while the option is still a
        # child of the TranspiledPythonUDF -- breaking a working UDF. The
        # category gate must refuse so it runs as interpreted Python.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def eq_true(x):
            return x == True  # noqa: E712

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(eq_true, BooleanType())
            self.assertEqual([], pudf.transpiled, "cross-category == must not transpile")
            df = self.spark.createDataFrame([Row(a=5), Row(a=1)], schema=long_schema)
            results = [r[0] for r in df.select(pudf("a")).collect()]
            self.assertEqual(results, [5 == True, 1 == True])

    def test_udf_transpile_falls_back_for_nested_ternary_eq(self):
        # A ternary operand used inside `==` must contribute its branches'
        # category, not the old "numeric" catch-all: `("5" if c else "6") == 5`
        # previously passed the equality guard as numeric-vs-numeric and
        # Spark's string-number coercion silently returned True where Python's
        # cross-type == is False. (Reported by Codex review on PR #34.)
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def nested_ternary_eq(x):
            return ("5" if x > 0 else "6") == 5

        def none_branch_ternary_eq(x):
            return ("5" if x > 0 else None) == 5

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        df = self.spark.createDataFrame([Row(a=5), Row(a=-5)], schema=long_schema)
        with self.sql_conf(_TRANSPILE_ON):
            for func in [nested_ternary_eq, none_branch_ternary_eq]:
                with self.subTest(func=func.__name__):
                    with _warnings.catch_warnings(record=True):
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, BooleanType())
                    self.assertEqual(
                        [], pudf.transpiled, "string-ternary == int must not transpile"
                    )
                    results = [r[0] for r in df.select(pudf("a")).collect()]
                    self.assertEqual(results, [False, False], "must match Python's ==")

    def test_udf_transpile_str_int_compare_matches_python(self):
        # Comparing a value against a string literal (``x == "5"`` / ``x < "5"``)
        # under the untyped-parameter path produces two candidate options -- a
        # numeric variant and a string variant. The numeric variant mixes
        # categories (numeric column vs string literal): Python compares such
        # values as unequal / raises TypeError, while a lowered ``x = '5'`` would
        # coerce under ANSI and silently diverge -- so ``_lower_eq`` /
        # ``_lower_value_compare`` refuse it. Only the string variant survives.
        # When the UDF is applied to a numeric column, ResolveTranspiledPython-
        # UDFOptions drops the string option (no category match) and the UDF
        # falls back to interpreted Python. We verify the observable guarantee on
        # both sides: ``==`` returns Python's result (no coerced ``True``), and
        # ``<`` raises on the Python side directly and surfaces the same error
        # through Spark rather than returning a coerced answer.
        from pyspark.errors import PythonException
        from pyspark.sql.types import StructField, StructType

        def eq_str(x):
            return x == "5"

        def lt_str(x):
            return x < "5"

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            df = self.spark.createDataFrame([Row(a=5), Row(a=1)], schema=long_schema)

            # ``==`` : numeric column -> string option dropped -> interpreted
            # Python, so the result matches ``5 == "5"`` (False), not a coerced
            # ``True`` from ``bigint = '5'``.
            pudf_eq = UserDefinedFunction(eq_str, BooleanType())
            results = [r[0] for r in df.select(pudf_eq("a")).collect()]
            self.assertEqual(results, [5 == "5", 1 == "5"], "must match Python's ==")

            # ``<`` : Python raises TypeError for ``int < str``; the transpiled
            # string option is dropped for a numeric column, so Spark runs the
            # interpreted UDF and surfaces the same error rather than coercing.
            # Asserting PythonException (not a bare Exception) pins this to the
            # interpreted-fallback path: a Catalyst AnalysisException here would
            # instead mean the string option was wrongly kept and broke the
            # query rather than falling back.
            pudf_lt = UserDefinedFunction(lt_str, BooleanType())
            with self.assertRaises(TypeError):
                lt_str(5)
            with self.assertRaises(PythonException) as ctx:
                df.select(pudf_lt("a")).collect()
            self.assertIn("not supported between", str(ctx.exception))

    def test_udf_transpile_falls_back_for_bool_arithmetic(self):
        # `(x > 0) + 1` is valid Python (True + 1 == 2), but the lowered
        # Add(boolean, int) fails ANSI analysis. The category of a
        # boolean-producing operand is now "bool" (not the numeric catch-all),
        # so this refuses and runs as interpreted Python.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def bool_plus_one(x):
            return (x > 0) + 1

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(bool_plus_one, LongType())
            self.assertEqual([], pudf.transpiled, "bool arithmetic must not transpile")
            df = self.spark.createDataFrame([Row(a=5), Row(a=-5)], schema=long_schema)
            results = [r[0] for r in df.select(pudf("a")).collect()]
            self.assertEqual(results, [2, 1])

    def test_udf_transpile_falls_back_for_return_wrapped_bool_branch(self):
        # If-statement branches arrive as ast.Return nodes; the branch-category
        # guard must see through the wrapper. A boolean-returning branch vs a
        # numeric one previously slipped past the guard and failed analysis
        # (CASE WHEN [BOOLEAN, INT]) instead of falling back.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def mixed(x):
            if x > 0:
                return x > 5
            else:
                return 1

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(mixed, LongType())
            self.assertEqual([], pudf.transpiled, "bool-vs-int branches must not transpile")
            df = self.spark.createDataFrame([Row(a=-3)], schema=long_schema)
            [result] = df.select(pudf("a")).collect()
            self.assertEqual(result[0], 1)

    def test_udf_transpile_falls_back_for_plain_self_param(self):
        # A plain function whose first parameter is literally named `self`
        # must not be confused with a bound __call__ method: stripping it
        # previously emitted `_udf_param_-1` and threw an AnalysisException
        # at call construction.
        import warnings as _warnings

        def weird(self, other):
            return self + other

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(weird, LongType())
            self.assertEqual([], pudf.transpiled, "plain 'self' param must not transpile")
            df = self.spark.createDataFrame([Row(a=2, b=3)])
            [result] = df.select(pudf("a", "b")).collect()
            self.assertEqual(result[0], 5)

    def test_udf_transpile_falls_back_for_wraps_decorated_function(self):
        # inspect.getsource follows __wrapped__, so a functools.wraps-decorated
        # UDF previously transpiled the WRAPPED function's source while the
        # interpreted path ran the wrapper -- a silent wrong result.
        import functools
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def base(x):
            return x + 1

        @functools.wraps(base)
        def wrapper(x):
            return base(x) * 10

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(wrapper, LongType())
            self.assertEqual([], pudf.transpiled, "wraps-decorated UDF must not transpile")
            df = self.spark.createDataFrame([Row(a=5)], schema=long_schema)
            [result] = df.select(pudf("a")).collect()
            self.assertEqual(result[0], 60, "must run the wrapper, not the wrapped source")

    def test_udf_transpile_falls_back_for_none_in_boolop(self):
        # Python's `None and x` short-circuits to None, which Spark's three-valued
        # `null AND false` (false) does not reproduce. Any operand
        # `_is_never_null_boolean` cannot prove non-NULL -- a literal None, or a
        # ternary with a None branch -- must fall back rather than diverge.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def none_and(x):
            return None and (x > 0)

        def nullable_ternary_and(x):
            return (True if x > 0 else None) and (x > 0)

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            for func in (none_and, nullable_ternary_and):
                with self.subTest(func=func.__name__):
                    with _warnings.catch_warnings(record=True):
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, BooleanType())
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{func.__name__}: nullable and/or operand must not transpile",
                    )
            pudf = UserDefinedFunction(none_and, BooleanType())
            df = self.spark.createDataFrame([Row(a=-5)], schema=long_schema)
            [result] = df.select(pudf("a")).collect()
            self.assertIsNone(result[0])

    def test_udf_transpile_falls_back_for_uncastable_return_type(self):
        # The lowered expression is cast to the declared return type; a return
        # type no atomic lowering can be cast to (arrays, maps, datetimes, ...)
        # would make that Cast fail CheckAnalysis and break the whole query
        # (the options are children of TranspiledPythonUDF), so such UDFs must
        # fall back at construction instead. Interpreted execution still works
        # (the pickled-UDF converter nulls the type-mismatched results).
        import warnings as _warnings
        from pyspark.sql.types import ArrayType, TimestampType

        plus_one = lambda x: x + 1  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            for rt in (ArrayType(LongType()), TimestampType()):
                with _warnings.catch_warnings(record=True):
                    _warnings.simplefilter("always")
                    pudf = UserDefinedFunction(plus_one, rt)
                self.assertEqual([], pudf.transpiled, f"return type {rt} must not transpile")
            # Interpreted execution keeps working; an int result for an array
            # return type is nulled by the pickled-UDF converter. (Timestamp
            # is not exercised here: its converter accepts ints as micros.)
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                array_udf = UserDefinedFunction(plus_one, ArrayType(LongType()))
            df = self.spark.createDataFrame([Row(a=1)])
            [result] = df.select(array_udf("a")).collect()
            self.assertIsNone(result[0], "interpreted fallback nulls the mismatch")

    def test_udf_transpile_falls_back_for_cross_category_return_cast(self):
        # Per-variant guard: the body category must MATCH the declared return
        # type's category. Un-castable combos (binary body -> numeric return,
        # boolean body -> binary return) would fail analysis outright, and
        # analysis-valid cross-category casts (string -> long, numeric ->
        # boolean, anything -> decimal) diverge from the interpreted path,
        # which nulls type-mismatched results instead of casting -- e.g.
        # `def f(s: str): return s` declared LongType() would return 123 for
        # '123' (or raise CAST_INVALID_INPUT) where interpreted returns NULL.
        import warnings as _warnings
        from pyspark.sql.types import DecimalType

        def bytes_to_long(x: bytes):
            return x

        def bool_to_binary(x):
            return (x > 0) if x is not None else None

        def str_ident(s: str):
            return s

        def plus_one(x):
            return x + 1

        with self.sql_conf(_TRANSPILE_ON):
            for func, rt, label in (
                (bytes_to_long, LongType(), "binary body -> numeric return"),
                (bool_to_binary, BinaryType(), "boolean body -> binary return"),
                (bytes_to_long, StringType(), "binary body -> string return"),
                (str_ident, LongType(), "string body -> numeric return"),
                (plus_one, BooleanType(), "numeric body -> boolean return"),
                (plus_one, DecimalType(10, 2), "numeric body -> decimal return"),
            ):
                with _warnings.catch_warnings(record=True):
                    _warnings.simplefilter("always")
                    pudf = UserDefinedFunction(func, rt)
                self.assertEqual([], pudf.transpiled, f"{label} must not transpile")
            # Interpreted execution of the Codex-flagged example: NULL, not a
            # cast. (The transpiled cast would have returned 123.)
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                str_long = UserDefinedFunction(str_ident, LongType())
            df = self.spark.createDataFrame([("123",)], "s string")
            self.assertIsNone(df.select(str_long("s")).first()[0])

    def test_udf_transpile_falls_back_for_non_numeric_unary(self):
        # Unary +/- only lower for numeric operands: Python raises TypeError
        # on `+s`/`-s` for strings while Spark's ANSI string promotion would
        # silently coerce (`-'5'` -> -5.0), and `-x` on a boolean would fail
        # analysis outright rather than fall back.
        import warnings as _warnings

        def neg_str(s: str):
            return -s

        def pos_str(s: str):
            return +s

        def neg_bool(x: bool):
            return -x

        with self.sql_conf(_TRANSPILE_ON):
            for func in (neg_str, pos_str, neg_bool):
                with _warnings.catch_warnings(record=True):
                    _warnings.simplefilter("always")
                    pudf = UserDefinedFunction(func, LongType())
                self.assertEqual([], pudf.transpiled, f"{func.__name__} must not transpile")
        # Numeric unary still lowers and matches Python.
        neg = lambda x: -x  # noqa: E731
        self.assertEqual(self._vals(neg, LongType(), "a long", [(5,), (-3,)]), [-5, 3])

    def test_udf_transpile_falls_back_for_self_reference(self):
        # A __call__ body that references bare `self` has no column
        # equivalent; the offset scheme previously emitted `_udf_param_-1`,
        # which the JVM builder rejected with an AnalysisException at call
        # construction instead of falling back to interpreted Python.
        import warnings as _warnings

        class PickSelf:
            def __call__(self, x):
                return x if x is not None else self

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(PickSelf(), LongType())
            self.assertEqual([], pudf.transpiled, "`self` reference must not transpile")
            # Interpreted execution still works (previously the call itself
            # raised). Only non-null rows are exercised: a row that RETURNS
            # `self` would fail JVM-side unpickling of the instance, which is
            # interpreted-UDF behavior unrelated to this guard.
            df = self.spark.createDataFrame([(2,), (7,)], "a long")
            results = [r[0] for r in df.select(pudf("a")).collect()]
            self.assertEqual(results, [2, 7])

    def test_udf_transpile_preserves_auto_column_name(self):
        # The auto-generated column name must stay `f(a)` whether or not the
        # rewrite engages; the TranspiledPythonUDF wrapper (and its option
        # children) must not leak into user-visible schema names.
        from pyspark.sql.types import StructField, StructType

        def plus_four(x):
            return x + 4

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        df = self.spark.createDataFrame([Row(a=1)], schema=long_schema)
        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(plus_four, LongType())
            self.assertTrue(pudf.transpiled)
            self.assertEqual(df.select(pudf("a")).columns, ["plus_four(a)"])

    def test_udf_transpile_arity_mismatch_falls_back(self):
        # Calling with the wrong number of arguments is a user error that must
        # surface as the standard Python-side TypeError, not be silently
        # absorbed by a transpiled constant (zero-param case) nor raise a
        # misleading "internal error" AnalysisException (too-few-args case).
        import warnings as _warnings
        from pyspark.errors import PythonException
        from pyspark.sql.types import StructField, StructType

        def zero():
            return 42

        def two(x, y):
            return x + y

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        df = self.spark.createDataFrame([Row(a=5)], schema=long_schema)
        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf_zero = UserDefinedFunction(zero, LongType())
                pudf_two = UserDefinedFunction(two, LongType())
            with self.assertRaises(PythonException):
                df.select(pudf_zero("a")).collect()
            with self.assertRaises(PythonException):
                df.select(pudf_two("a")).collect()

    def test_udf_transpile_decimal_input_falls_back(self):
        # Python receives decimal.Decimal objects, which raise TypeError when
        # mixed with float literals; the transpiled numeric lowering would
        # silently succeed. Decimal columns must fall back to interpreted
        # Python (pruned by input category at analysis time).
        from pyspark.errors import PythonException

        def add_half(x):
            return x + 1.5

        with self.sql_conf(_TRANSPILE_ON):
            # DoubleType: the return type must category-match the numeric body
            # for the option to be emitted (a string return type would itself
            # force a fallback before the decimal-input pruning under test).
            pudf = UserDefinedFunction(add_half, DoubleType())
            self.assertTrue(pudf.transpiled, "numeric option should still be produced")
            df = self.spark.sql("SELECT CAST(1.0 AS DECIMAL(10,2)) AS d")
            with self.assertRaises(PythonException):
                df.select(pudf("d")).collect()

    def test_udf_transpile_collated_string_falls_back(self):
        # Under a non-binary collation Spark's `=` follows collation rules
        # ('abc' = 'ABC' is true under UTF8_LCASE) while Python compares
        # codepoints. Collated columns must fall back to interpreted Python.
        def eq_abc(s):
            return s == "ABC"

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(eq_abc, BooleanType())
            self.assertTrue(pudf.transpiled, "string option should still be produced")
            df = self.spark.sql("SELECT 'abc' COLLATE UTF8_LCASE AS s")
            [result] = df.select(pudf("s")).collect()
            self.assertIs(result[0], False, "must match Python, not collation semantics")

    def test_udf_transpile_is_none_semantics(self):
        # `x is None` and `None is x` (and their `is not` variants) should
        # transpile to isNull/isNotNull. Any other identity check (`x is 0`,
        # `x is y`, `x is True`) must NOT transpile -- Python's `is` is an
        # object-identity test with no SQL equivalent outside of None.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        long_schema = StructType([StructField("a", LongType(), nullable=True)])

        def x_is_none(x):
            return x is None

        def x_is_not_none(x):
            if x is not None:
                return x + 1

        def none_is_x(x):
            return None is x

        def none_is_not_x(x):
            if None is not x:
                return x + 1

        def x_is_zero(x):
            return x is 0  # noqa: F632  identity vs equality

        def x_is_true(x):
            return x is True

        def x_is_y(x, y):
            return x is y

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            # `x is None` and `None is x` should transpile and produce
            # identical results.
            for func, label in [(x_is_none, "x_is_none"), (none_is_x, "none_is_x")]:
                with self.subTest(case=label):
                    pudf = UserDefinedFunction(func, BooleanType())
                    self.assertTrue(
                        pudf.transpiled,
                        f"{label}: expected transpilation to succeed",
                    )
                    df = self.spark.createDataFrame([Row(a=None)], schema=long_schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertTrue(row[0], f"{label}: None is None should be True")
                    df = self.spark.createDataFrame([Row(a=1)], schema=long_schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertFalse(row[0], f"{label}: 1 is None should be False")

            # `x is not None` and `None is not x` should transpile.
            for func, label in [
                (x_is_not_none, "x_is_not_none"),
                (none_is_not_x, "none_is_not_x"),
            ]:
                with self.subTest(case=label):
                    pudf = UserDefinedFunction(func, LongType())
                    self.assertTrue(
                        pudf.transpiled,
                        f"{label}: expected transpilation to succeed",
                    )
                    df = self.spark.createDataFrame([Row(a=2)], schema=long_schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertEqual(row[0], 3, f"{label}: non-None input should return x+1")
                    df = self.spark.createDataFrame([Row(a=None)], schema=long_schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertIsNone(row[0], f"{label}: None input should return None")

            # Non-None identity checks must NOT transpile and must still
            # return correct results via interpreted Python.
            bool_schema = StructType([StructField("a", BooleanType(), nullable=True)])
            two_col_schema = StructType(
                [
                    StructField("a", LongType(), nullable=True),
                    StructField("b", LongType(), nullable=True),
                ]
            )
            non_none_cases = [
                # CPython interns small ints so `0 is 0` happens to be True in CPython,
                # but that is an implementation detail. The transpiler must still refuse
                # to lower these to isNull/isNotNull. We just verify: (a) no transpile,
                # (b) the interpreted result matches what Python actually produces.
                ("x_is_zero", x_is_zero, BooleanType(), long_schema, Row(a=0), True),
                # `True is True` is True because bool singletons are interned.
                ("x_is_true", x_is_true, BooleanType(), bool_schema, Row(a=True), True),
                ("x_is_y", x_is_y, BooleanType(), two_col_schema, Row(a=1, b=1), True),
            ]
            for label, func, return_type, schema, row, expected in non_none_cases:
                with self.subTest(case=label):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, return_type)
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{label}: non-None identity check must NOT transpile",
                    )
                    fallback = [
                        w
                        for w in caught
                        if "Unable to transpile" in str(w.message)
                        or "Errors encountered" in str(w.message)
                    ]
                    self.assertTrue(fallback, f"{label}: expected a fallback warning")
                    df = self.spark.createDataFrame([row], schema=schema)
                    args = ["a", "b"] if "b" in schema.fieldNames() else ["a"]
                    [result] = df.select(pudf(*args)).collect()
                    self.assertEqual(result[0], expected, f"{label}: interpreted result mismatch")

    def test_udf_transpile_not_bare_param_falls_back(self):
        # `not x` where x is a bare UDF parameter (unknown type at
        # transpile time) must NOT be lowered: Spark's `~` is bitwise, not
        # Python truthiness, so `not 0` would produce True via Python but
        # Spark's `~0L` is -1 (truthy). The transpiler must refuse and fall
        # back to interpreted Python.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def not_x(x):
            return not x

        long_schema = StructType([StructField("a", LongType(), nullable=True)])

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(not_x, BooleanType())
            self.assertEqual([], pudf.transpiled, "not x on bare param must NOT transpile")
            fallback = [
                w
                for w in caught
                if "Unable to transpile" in str(w.message) or "Errors encountered" in str(w.message)
            ]
            self.assertTrue(fallback, "expected a fallback warning for `not x`")
            # Verify interpreted result is still correct.
            for value, expected in [(0, True), (1, False), (None, True)]:
                with self.subTest(value=value):
                    df = self.spark.createDataFrame([Row(a=value)], schema=long_schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertEqual(row[0], expected)

    def test_udf_transpile_and_or_bare_param_falls_back(self):
        # `x and y` / `x or y` where x/y are bare UDF parameters (unknown
        # type) must NOT be lowered: Python returns one of the operands
        # (truthiness semantics) while Spark's `&`/`|` are bitwise. The
        # transpiler must refuse and fall back.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def x_and_y(x, y):
            return x and y

        def x_or_y(x, y):
            return x or y

        schema = StructType(
            [
                StructField("a", LongType(), nullable=True),
                StructField("b", LongType(), nullable=True),
            ]
        )

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            for func, label, row, expected in [
                (x_and_y, "x_and_y_falsy", Row(a=0, b=5), 0),
                (x_and_y, "x_and_y_truthy", Row(a=3, b=5), 5),
                (x_or_y, "x_or_y_falsy_left", Row(a=0, b=5), 5),
                (x_or_y, "x_or_y_truthy_left", Row(a=3, b=0), 3),
            ]:
                with self.subTest(case=label):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, LongType())
                    self.assertEqual(
                        [],
                        pudf.transpiled,
                        f"{label}: and/or on bare params must NOT transpile",
                    )
                    fallback = [
                        w
                        for w in caught
                        if "Unable to transpile" in str(w.message)
                        or "Errors encountered" in str(w.message)
                    ]
                    self.assertTrue(fallback, f"{label}: expected a fallback warning")
                    df = self.spark.createDataFrame([row], schema=schema)
                    [result] = df.select(pudf("a", "b")).collect()
                    self.assertEqual(result[0], expected, f"{label}: interpreted result mismatch")

    def test_cannot_convert_column_into_bool_includes_column_repr(self):
        # The error fired by ``Column.__bool__`` should name the offending
        # column so users can see which expression triggered the fallback.
        from pyspark.errors import PySparkValueError

        df = self.spark.createDataFrame([Row(a=1, b=2)])
        col_a = df["a"]
        with self.assertRaises(PySparkValueError) as ctx:
            bool(col_a)
        message = str(ctx.exception)
        self.assertIn("Cannot convert column into bool", message)
        # Column's stringification is JVM-side and may render the column
        # as ``a`` (unresolved) or with a backtick variant, so we just
        # require the column name appears somewhere in the message.
        self.assertIn("a", message)

    # ------------------------------------------------------------------
    # Edge cases (SPARK-55206 follow-up). Helpers build a UDF with
    # transpilation on; `_vals` runs it and returns outputs (asserting it
    # transpiled), `_raises` asserts it raises. Arg columns come from the
    # schema. Operator cases are table-driven. Plan-elision checks count
    # `EvalPython` nodes because an ordering compare's `raise_error` message
    # contains "UDF" (so the "UDF" substring is unreliable).
    # ------------------------------------------------------------------

    def _vals(self, func, return_type, schema, rows):
        with self.sql_conf(_TRANSPILE_ON):
            u = UserDefinedFunction(func, return_type)
            self.assertTrue(u.transpiled, str(func))
            df = self.spark.createDataFrame(rows, schema)
            return [r[0] for r in df.select(u(*df.columns)).collect()]

    def _raises(self, func, schema, rows, needle="numeric"):
        with self.sql_conf(_TRANSPILE_ON):
            u = UserDefinedFunction(func, LongType())
            self.assertTrue(u.transpiled, str(func))
            df = self.spark.createDataFrame(rows, schema)
            with self.assertRaises(Exception) as ctx:
                df.select(u(*df.columns)).collect()
            self.assertIn(needle, str(ctx.exception).lower(), str(func))

    @staticmethod
    def _eval_python_count(df):
        return df._jdf.queryExecution().executedPlan().toString().count("EvalPython")

    def test_udf_transpile_lowers_operators(self):
        # Operators lower to Catalyst and match Python: modulo sign-parity,
        # non-commutative -/* (parameter order), unary nesting, constant
        # body, not(compare), nested boolean, string ==/<, reversed-operand and
        # column-to-column comparisons, if/elif/else, and assigned lambdas.
        L, B = LongType(), BooleanType()
        modulo = lambda x, y: x % y  # noqa: E731
        subtract = lambda a, b: a - b  # noqa: E731
        multiply = lambda a, b: a * b  # noqa: E731
        double_neg = lambda x: --x  # noqa: E731
        unary_pm = lambda x: +(-x)  # noqa: E731
        constant = lambda x: 42  # noqa: E731
        not_pos = lambda x: (not (x > 0)) if x is not None else None  # noqa: E731
        nested = lambda x, y, z: ((x > 0) and (y > 0)) or (z == 0)  # noqa: E731
        str_eq = lambda x: (x == "foo") if x is not None else None  # noqa: E731
        str_lt = lambda x: (x < "m") if x is not None else None  # noqa: E731
        rev_lt = lambda x: (0 < x) if x is not None else None  # noqa: E731
        rev_eq = lambda x: 5 == x  # noqa: E731
        none_eq = lambda x: None == x  # noqa: E711,E731
        col_lt = lambda a, b: (a < b) if a is not None and b is not None else None  # noqa: E731
        assigned = lambda v: v + 1  # noqa: E731
        bounded = lambda x: 0 <= x <= 100  # noqa: E731
        str_chain = lambda x: "a" < x < "z"  # noqa: E731
        chain_and = lambda a, b, c: (0 < a < 10) and (b == c)  # noqa: E731
        chain_ternary = lambda x: (0 < x < 10) if x is not None else None  # noqa: E731

        def if_elif_else(x):
            if x is None:
                return -1
            elif x == 0:
                return 0
            else:
                return 1

        # (func, return_type, schema, rows, expected); arg columns come from the schema.
        cases = [
            (modulo, L, "a long, b long", [(7, 3), (7, -3), (-7, 3), (-7, -3)], [1, -2, 2, -1]),
            (subtract, L, "a long, b long", [(5, 3), (3, 5)], [2, -2]),
            (multiply, L, "a long, b long", [(4, 3), (-2, 5)], [12, -10]),
            (double_neg, L, "a long", [(5,), (-3,)], [5, -3]),
            (unary_pm, L, "a long", [(5,), (-3,)], [-5, 3]),
            (constant, L, "a long", [(1,), (999,)], [42, 42]),
            (not_pos, B, "a long", [(1,), (0,), (-1,), (None,)], [False, True, True, None]),
            (str_eq, B, "a string", [("foo",), ("bar",), (None,)], [True, False, None]),
            (str_lt, B, "a string", [("a",), ("z",), (None,)], [True, False, None]),
            (rev_lt, B, "a long", [(1,), (0,), (-1,)], [True, False, False]),
            (rev_eq, B, "a long", [(5,), (3,), (None,)], [True, False, False]),
            (none_eq, B, "a long", [(None,), (5,)], [True, False]),
            (col_lt, B, "a long, b long", [(1, 2), (2, 1), (1, 1)], [True, False, False]),
            (if_elif_else, L, "a long", [(None,), (0,), (5,), (-3,)], [-1, 0, 1, 1]),
            (assigned, L, "a long", [(1,), (10,)], [2, 11]),
            # Chained comparisons.
            (
                bounded,
                B,
                "a long",
                [(0,), (50,), (100,), (-1,), (101,)],
                [True, True, True, False, False],
            ),
            (str_chain, B, "a string", [("m",), ("a",), ("z",)], [True, False, False]),
            # A chain as a boolean operand and inside a ternary: the fold's result
            # is a non-NULL boolean, so the `and`/`or` gate accepts it and it
            # unifies with a None ternary branch.
            (
                chain_and,
                B,
                "a long, b long, c long",
                [(5, 1, 1), (5, 1, 2), (50, 1, 1)],
                [True, False, False],
            ),
            (chain_ternary, B, "a long", [(5,), (50,), (None,)], [True, False, None]),
            (
                nested,
                B,
                "a long, b long, c long",
                [(1, 1, 5), (-1, 1, 0), (-1, 1, 5)],
                [True, True, False],
            ),
        ]
        for i, (func, rt, schema, rows, expected) in enumerate(cases):
            with self.subTest(case=i):
                self.assertEqual(self._vals(func, rt, schema, rows), expected, f"case {i}: {rows}")

    def test_udf_transpile_callable_object_self_offset(self):
        # A callable instance carries `self`; the extractor offsets it so a/b
        # map to _udf_param_0/_udf_param_1 (non-commutative body proves order).
        class SubAB:
            def __call__(self, a, b):
                return a - b

        self.assertEqual(
            self._vals(SubAB(), LongType(), "a long, b long", [(5, 3), (3, 5)]), [2, -2]
        )

    def test_udf_transpile_plan_elision(self):
        # Transpiled UDFs are elided in filter (not just select); a mixed
        # non-convertible -> convertible -> non-convertible chain inlines only
        # the middle UDF, leaving exactly two Python eval nodes.
        offset = 3
        gt5 = lambda x: (x > 5) if x is not None else None  # noqa: E731
        add_offset = lambda x: x + offset  # noqa: E731  closure -> fallback
        plus_one = lambda x: x + 1  # noqa: E731  convertible
        div_two = lambda x: x / 2  # noqa: E731  `/` -> fallback
        with self.sql_conf(_TRANSPILE_ON):
            f = UserDefinedFunction(gt5, BooleanType())
            self.assertTrue(f.transpiled)
            fdf = self.spark.createDataFrame([(3,), (7,), (1,), (None,)], "a long").filter(f("a"))
            self.assertEqual([r[0] for r in fdf.collect()], [7])
            self.assertEqual(0, self._eval_python_count(fdf))

            u1 = UserDefinedFunction(add_offset, LongType())
            u2 = UserDefinedFunction(plus_one, LongType())
            u3 = UserDefinedFunction(div_two, DoubleType())
            self.assertEqual(([], True, []), (u1.transpiled, bool(u2.transpiled), u3.transpiled))
            chained = (
                self.spark.createDataFrame([(10,)], "a long")
                .select(u1("a").alias("x"))
                .select(u2("x").alias("y"))
                .select(u3("y").alias("z"))
            )
            self.assertEqual(chained.first()[0], 7.0)  # ((10 + 3) + 1) / 2
            self.assertEqual(2, self._eval_python_count(chained))

            # A chained comparison is fully lowered, so the EvalPython node
            # disappears for it too.
            bounded = lambda x: 0 < x < 10  # noqa: E731
            f = UserDefinedFunction(bounded, BooleanType())
            self.assertTrue(f.transpiled)
            fdf = self.spark.createDataFrame([(7,), (11,), (1,)], "a long").filter(f("a"))
            self.assertEqual(sorted(r[0] for r in fdf.collect()), [1, 7])
            self.assertEqual(0, self._eval_python_count(fdf))

    def test_udf_transpile_nondeterministic_argument_falls_back(self):
        # RED UNTIL the companion analyzer fix lands -- deliberately, to pin a real
        # wrong-results bug rather than tolerate it silently. Do not skip or xfail.
        #
        # A non-deterministic ARGUMENT must suppress transpilation at the call site
        # even though the UDF itself transpiled fine. (Distinct from
        # test_udf_transpile_skips_nondeterministic, which covers a UDF marked
        # non-deterministic.) `resolveUDFParams` substitutes the argument at every
        # `_udf_param_N` occurrence -- four of them for a two-link chain -- so a
        # non-deterministic one becomes several independent expressions with
        # independent state. Since `and` / `or` and chains evaluate later copies
        # only on the rows where earlier links held, the copies desynchronize:
        # `50 < x < 60` over `rand(7) * 100` is ~30% true instead of ~10%.
        #
        # The fix belongs in ResolveTranspiledPythonUDFOptions. It cannot go in
        # Python (no call site exists at UDF construction) nor in the JVM builder
        # (`rand(7)` is still an UnresolvedFunction there and reports
        # deterministic=true). Assert on the plan, not on observed proportions: a
        # statistical assertion would be flaky, and keeping EvalPython means the
        # interpreted UDF runs, which is correct by construction.
        from pyspark.sql.functions import col, rand

        band = lambda x: 50 < x < 60  # noqa: E731
        band_or = lambda x: x < 50 or x > 60  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            for label, func in (("50 < x < 60", band), ("x < 50 or x > 60", band_or)):
                # subTest so the chain failing does not hide the `or` case, which
                # has the same bug by the same mechanism.
                with self.subTest(func=label):
                    pudf = UserDefinedFunction(func, BooleanType())
                    self.assertTrue(pudf.transpiled, f"{label} should transpile on its own")
                    base = self.spark.range(0, 8).select((col("id") * 10).alias("a"))

                    # Deterministic argument -> transpiled and elided.
                    det = base.select(pudf("a"))
                    self.assertEqual(
                        0,
                        self._eval_python_count(det),
                        f"{label}: a deterministic argument should still be elided",
                    )

                    # Non-deterministic argument -> must fall back.
                    nondet = base.select(pudf(rand(7) * 100))
                    self.assertEqual(
                        1,
                        self._eval_python_count(nondet),
                        f"{label}: a non-deterministic argument must NOT be transpiled",
                    )
                    # And it still produces results.
                    self.assertEqual(8, len(nondet.collect()))

    def test_udf_transpile_config_toggle_no_stale_nodes(self):
        # Built with the flags on, executed with them off -> clean fallback to
        # interpreted Python (the optimizer drops the transpiled node), no error.
        plus_one = lambda x: x + 1  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            u = UserDefinedFunction(plus_one, LongType())
            self.assertTrue(u.transpiled)
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": False,
                "spark.sql.ansi.enabled": False,
            }
        ):
            df = self.spark.createDataFrame([(1,), (5,)], "a long")
            self.assertEqual([r[0] for r in df.select(u("a")).collect()], [2, 6])

    def test_udf_transpile_casts_to_return_type(self):
        # The lowered expression is cast to the declared return type.
        plus_one = lambda x: x + 1  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            d = UserDefinedFunction(plus_one, DoubleType())
            col = self.spark.createDataFrame([(1,)], "a long").select(d("a").alias("r"))
            self.assertEqual(col.schema["r"].dataType, DoubleType())
            self.assertEqual(col.first()[0], 2.0)
        self.assertEqual(self._vals(plus_one, LongType(), "a long", [(1,)]), [2])

    def test_udf_transpile_falls_back(self):
        # Shapes that must NOT transpile (and still compute via Python):
        # inline/wrapped/partial lambdas, default/variadic/keyword-only args, and
        # `%` string formatting. (String `+`/`*` now lower to concat/repeat -- see
        # test_udf_transpile_string_operands -- but `%` as a format is not handled.)
        import functools

        def wrapper(fn):
            return fn

        def with_default(a, b=0):
            return a + 10 * b

        def with_varargs(a, *rest):
            return a

        def with_kwargs(a, **opts):
            return a

        base = lambda v, w: v + w  # noqa: E731
        percent_fmt = lambda x: "n=%d" % x  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            # inline / wrapped / partial lambdas -> source can't be extracted
            self.assertEqual([], UserDefinedFunction(lambda v: v + 1, LongType()).transpiled)
            self.assertEqual(
                [], UserDefinedFunction(wrapper(lambda v: v + 1), LongType()).transpiled
            )
            self.assertEqual(
                [], UserDefinedFunction(functools.partial(base, 1), LongType()).transpiled
            )
            # default / variadic / keyword-only args, and `%` string formatting
            for func, rt in [
                (with_default, LongType()),
                (with_varargs, LongType()),
                (with_kwargs, LongType()),
                (percent_fmt, StringType()),
            ]:
                with self.subTest(func=func):
                    self.assertEqual([], UserDefinedFunction(func, rt).transpiled)
            # Fell back -> interpreted Python still computes correctly.
            wd = UserDefinedFunction(with_default, LongType())
            num = self.spark.createDataFrame([(5,)], "a long")
            self.assertEqual(
                [num.select(wd("a")).first()[0], num.select(wd("a", "a")).first()[0]], [5, 55]
            )

    def test_udf_transpile_known_value_divergences(self):
        # Transpile but DIVERGE from Python (documented in transpile.py; pinned so
        # a future fix is noticed): unguarded arithmetic on NULL yields NULL
        # (Python raises TypeError), and NaN > 0 is True (Python False; Spark
        # orders NaN highest). Mixed str/numeric arithmetic is handled or falls
        # back -- see test_udf_transpile_string_operands{,_fall_back}.
        unguarded = lambda x: x + 1  # noqa: E731
        nan_gt = lambda x: (x > 0) if x is not None else None  # noqa: E731
        eq_strlit = lambda x: (x == "5") if x is not None else None  # noqa: E731
        self.assertEqual(self._vals(unguarded, LongType(), "a long", [(None,), (5,)]), [None, 6])
        self.assertEqual(
            self._vals(nan_gt, BooleanType(), "a double", [(float("nan"),), (1.0,)]), [True, True]
        )
        # `x == "5"` used to be pinned as a coercion divergence (int == "5" ->
        # True). The eq category gate now drops the numeric variant, so on a
        # long column the string option is pruned and the UDF falls back to
        # interpreted Python -- matching Python's cross-type == (always False).
        self.assertEqual(
            self._vals(eq_strlit, BooleanType(), "a long", [(5,), (3,)]), [False, False]
        )
        # Chained comparisons inherit the same NaN divergence one link at a time --
        # no new mechanism, just easier to hit. Spark orders NaN above every value
        # (Python's `b < nan` is False) and treats `NaN = NaN` as true (Python's
        # `nan == nan` is False). These assertions pin the CURRENT divergent
        # behavior so a change is visible; SPARK-58781 decides what to do about it.
        nan_chain = lambda a, b, c: a < b < c  # noqa: E731
        nan_eq_chain = lambda a, b, c: a == b == c  # noqa: E731
        nan = float("nan")
        self.assertEqual(
            self._vals(nan_chain, BooleanType(), "a double, b double, c double", [(1.0, 2.0, nan)]),
            [True],
            "Spark orders NaN highest; Python would return False",
        )
        self.assertEqual(
            self._vals(
                nan_eq_chain, BooleanType(), "a double, b double, c double", [(nan, nan, nan)]
            ),
            [True],
            "Spark treats NaN = NaN as true; Python would return False",
        )

    def test_udf_transpile_overflow_and_modulo_zero_raise(self):
        # Transpiled arithmetic that raises at runtime: `*` overflow raises under
        # ANSI where Python promotes to a big int (a real divergence, SPARK-55210),
        # while `% 0` raises in both Spark and Python (compatible -- pinned here so
        # it isn't mistaken for a divergence).
        overflow = lambda x: x * x  # noqa: E731
        modulo_zero = lambda x: x % 0  # noqa: E731
        self._raises(overflow, "a long", [(4000000000,)], "overflow")
        self._raises(modulo_zero, "a long", [(5,)], "zero")

    def test_udf_transpile_string_operands(self):
        # Textual `+`/`*` lower to Catalyst string ops and match Python: `str +
        # str` -> concat, and `str * int` / `int * str` -> repeat (including a
        # string column times a numeric literal). The transpiler emits a string-
        # typed variant whose declared categories the JVM matches against the bound
        # column types (see UserDefinedPythonFunction.builder).
        S = StringType()
        add = lambda a, b: a + b  # noqa: E731
        mul = lambda a, b: a * b  # noqa: E731
        mul3 = lambda a: a * 3  # noqa: E731
        concat_right = lambda a: a + "!"  # noqa: E731
        concat_left = lambda a: "pre-" + a  # noqa: E731
        repeat_lit = lambda x: "ab" * x  # noqa: E731
        # (func, return_type, schema, rows, expected); arg columns come from schema.
        cases = [
            (add, S, "a string, b string", [("x", "y"), ("a", "b")], ["xy", "ab"]),
            (mul, S, "a string, b long", [("ab", 3)], ["ababab"]),
            (mul, S, "a long, b string", [(3, "ab")], ["ababab"]),
            (mul3, S, "a string", [("2",), ("ab",)], ["222", "ababab"]),
            (concat_right, S, "a string", [("hi",)], ["hi!"]),
            (concat_left, S, "a string", [("x",)], ["pre-x"]),
            (repeat_lit, S, "a long", [(3,)], ["ababab"]),
        ]
        for i, (func, rt, schema, rows, expected) in enumerate(cases):
            with self.subTest(case=i):
                self.assertEqual(self._vals(func, rt, schema, rows), expected, f"case {i}")

    def test_udf_transpile_string_operands_fall_back(self):
        # Operand/type combos with no valid string lowering for the bound column
        # types fall back to the Python UDF, which raises the same way CPython does:
        # `str + int` (and reversed), `str - int`, `str * str`, `str % int`, and a
        # string column plus a numeric literal. The transpiler still emits numeric
        # (and/or concat/repeat) variants, but none match the column types, so the
        # JVM drops them and runs Python -- matching its TypeError.
        add = lambda a, b: a + b  # noqa: E731
        sub = lambda a, b: a - b  # noqa: E731
        mul = lambda a, b: a * b  # noqa: E731
        mod = lambda a, b: a % b  # noqa: E731
        add5 = lambda a: a + 5  # noqa: E731
        # needle="" -> assert only that it raises (the message is CPython's).
        for func, schema, rows in [
            (add, "a string, b long", [("10", 5)]),  # str + int
            (add, "a long, b string", [(5, "10")]),  # int + str
            (sub, "a string, b long", [("10", 5)]),  # str - int
            (mul, "a string, b string", [("a", "b")]),  # str * str
            (mod, "a string, b long", [("10", 3)]),  # str % int
            (add5, "a string", [("10",)]),  # str column + numeric literal
        ]:
            with self.subTest(func=func, schema=schema):
                self._raises(func, schema, rows, needle="")

    def test_udf_transpile_power_falls_back(self):
        # `**` is intentionally not lowered (Spark's pow is DOUBLE and loses
        # precision for large ints), so a UDF using it falls back to interpreted
        # Python. TODO(SPARK-55210): revisit once an exact integer-power lowering
        # exists.
        square = lambda x: x**2  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            self.assertFalse(UserDefinedFunction(square, LongType()).transpiled)

    def test_udf_transpile_non_numeric_constant_falls_back(self):
        # bool/None constants have no faithful numeric/string lowering, so
        # arithmetic against them must fall back rather than emit an option that
        # crashes analysis (`x * True`) or silently returns NULL (`x + None`).
        mul_bool = lambda x: x * True  # noqa: E731
        add_none = lambda x: x + None  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            self.assertFalse(UserDefinedFunction(mul_bool, LongType()).transpiled)
            self.assertFalse(UserDefinedFunction(add_none, LongType()).transpiled)

    def test_udf_transpile_mixed_type_comparison_falls_back(self):
        # Python forbids ordering across types (`a < b` for int/str -> TypeError);
        # Spark would coerce and return a wrong boolean. A comparison whose
        # operand categories differ is dropped (so int-vs-str `<` falls back),
        # while a same-category comparison still transpiles.
        def lt_mixed(a: int, b: str):
            return (a < b) if a is not None and b is not None else None

        def lt_same(a: int, b: int):
            return (a < b) if a is not None and b is not None else None

        with self.sql_conf(_TRANSPILE_ON):
            self.assertFalse(UserDefinedFunction(lt_mixed, BooleanType()).transpiled)
            self.assertTrue(UserDefinedFunction(lt_same, BooleanType()).transpiled)

    def test_udf_transpile_skips_nondeterministic(self):
        # A nondeterministic UDF must not be transpiled: the optimizer could
        # fold/reorder/duplicate the plain expression, dropping the barrier.
        # Holds whether marked at construction or via asNondeterministic().
        plus_one = lambda x: x + 1  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            self.assertTrue(UserDefinedFunction(plus_one, LongType()).transpiled)
            self.assertFalse(
                UserDefinedFunction(plus_one, LongType()).asNondeterministic().transpiled
            )
            self.assertFalse(
                UserDefinedFunction(plus_one, LongType(), deterministic=False).transpiled
            )

    def test_udf_transpile_bool_and_binary_params(self):
        # bool/bytes annotations map to the "bool"/"binary" categories and match
        # Boolean/Binary columns. Identity and same-category comparison transpile
        # (and match Python); boolean arithmetic has no lowering and falls back.
        def bool_ident(x: bool):
            return x

        def bool_lt(a: bool, b: bool):
            return (a < b) if a is not None and b is not None else None

        def bool_add(x: bool):
            return x + 1  # no boolean arithmetic lowering -> fall back

        def bytes_ident(x: bytes):
            return x

        self.assertEqual(
            self._vals(bool_ident, BooleanType(), "a boolean", [(True,), (False,), (None,)]),
            [True, False, None],
        )
        self.assertEqual(
            self._vals(
                bool_lt,
                BooleanType(),
                "a boolean, b boolean",
                [(False, True), (True, False), (True, True)],
            ),
            [True, False, False],
        )
        with self.sql_conf(_TRANSPILE_ON):
            self.assertFalse(UserDefinedFunction(bool_add, LongType()).transpiled)
            self.assertTrue(UserDefinedFunction(bytes_ident, BinaryType()).transpiled)

    def test_param_category_combos_caps_preserve_typed_pins(self):
        # With more than three untyped params the cap collapses the untyped ones
        # to numeric/string but keeps each typed param pinned (here a: str).
        import ast as _ast

        from pyspark.sql.transpile import _param_category_combos

        fn = _ast.parse("def f(a: str, b, c, d, e): return a").body[0]
        combos = _param_category_combos(fn, ["a", "b", "c", "d", "e"])
        self.assertEqual(len(combos), 2)
        for combo in combos:
            self.assertEqual(combo[0], "string")


if __name__ == "__main__":
    from pyspark.testing import main

    main()
