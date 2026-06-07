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
        # results in this case), the transpiler should lower to bitwise
        # `&`/`|` and produce results matching the interpreted UDF.
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
            # NULL inputs propagate through `>` to NULL, which then
            # passes through `&` / `|` per SQL three-valued logic. We
            # only assert on non-NULL inputs here since Python's
            # interpreted `x > 0 and y > 0` would raise on None; the
            # NULL handling itself is covered by the hypothesis suite.
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

    def test_udf_transpile_chained_comparison_falls_back(self):
        # ``a < b < c`` is a chained comparison: Python evaluates it as
        # ``(a < b) and (b < c)``. The transpiler refuses chained Compare
        # nodes (``len(ops) != 1``) and must fall back to interpreted Python.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def chained(x):
            return 0 < x < 10

        schema = StructType([StructField("a", LongType(), nullable=False)])
        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": True,
                "spark.sql.ansi.enabled": True,
            }
        ):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(chained, BooleanType())
            self.assertEqual([], pudf.transpiled, "chained comparison must NOT transpile")
            fallback = [
                w
                for w in caught
                if "Unable to transpile" in str(w.message) or "Errors encountered" in str(w.message)
            ]
            self.assertTrue(fallback, "expected a fallback warning")
            for value, expected in [(5, True), (0, False), (10, False), (-3, False)]:
                with self.subTest(value=value):
                    df = self.spark.createDataFrame([Row(a=value)], schema=schema)
                    [row] = df.select(pudf("a")).collect()
                    self.assertEqual(row[0], expected)

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
    # Edge cases (SPARK-55206 follow-up). Behaviors below were confirmed
    # against the running transpiler; fallbacks are locked in as the safe
    # contract. Note: ordering comparisons emit a ``raise_error`` whose
    # message literal contains "UDF", so plan-elision checks count
    # ``EvalPython`` nodes rather than the "UDF" substring.
    # ------------------------------------------------------------------

    @staticmethod
    def _eval_python_count(df):
        plan = df._jdf.queryExecution().executedPlan().toString()
        return plan.count("EvalPython")

    def test_udf_transpile_modulo_sign_parity(self):
        # Python's ``%`` takes the sign of the divisor; Spark's ``%`` takes
        # the sign of the dividend. The transpiler rewrites ``a % b`` as
        # ``sign(b) * pmod(sign(b) * a, abs(b))`` so the result matches
        # Python for negative operands. Pin that parity deterministically
        # (the hypothesis suite that fuzzes this is gated behind
        # RUN_HYPOTHESIS and skips in normal runs).
        def py_mod(x, y):
            if x is not None and y is not None:
                return x % y

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(py_mod, LongType())
            self.assertTrue(pudf.transpiled, "two-arg modulo should transpile")
            for a, b in [(7, 3), (7, -3), (-7, 3), (-7, -3), (0, 3), (5, -5), (-8, 3)]:
                with self.subTest(a=a, b=b):
                    df = self.spark.createDataFrame([(a, b)], "a long, b long")
                    self.assertEqual(
                        df.select(pudf("a", "b")).first()[0],
                        a % b,
                        f"{a} % {b} should match Python semantics",
                    )

    def test_udf_transpile_binary_arithmetic_param_order(self):
        # ``-``, ``*``, ``**`` lower to the matching Column ops. The two-arg
        # non-commutative cases also prove parameter order is preserved
        # (a -> _udf_param_0, b -> _udf_param_1), not swapped.
        def sub_ab(a, b):
            if a is not None and b is not None:
                return a - b

        def mul_ab(a, b):
            if a is not None and b is not None:
                return a * b

        def square(x):
            if x is not None:
                return x ** 2

        with self.sql_conf(_TRANSPILE_ON):
            sub_udf = UserDefinedFunction(sub_ab, LongType())
            mul_udf = UserDefinedFunction(mul_ab, LongType())
            sq_udf = UserDefinedFunction(square, LongType())
            self.assertTrue(sub_udf.transpiled and mul_udf.transpiled and sq_udf.transpiled)
            for a, b, expected in [(5, 3, 2), (3, 5, -2), (0, 7, -7)]:
                with self.subTest(op="sub", a=a, b=b):
                    df = self.spark.createDataFrame([(a, b)], "a long, b long")
                    self.assertEqual(df.select(sub_udf("a", "b")).first()[0], expected)
            for a, b, expected in [(4, 3, 12), (-2, 5, -10)]:
                with self.subTest(op="mul", a=a, b=b):
                    df = self.spark.createDataFrame([(a, b)], "a long, b long")
                    self.assertEqual(df.select(mul_udf("a", "b")).first()[0], expected)
            for x, expected in [(6, 36), (-3, 9), (0, 0)]:
                with self.subTest(op="pow", x=x):
                    df = self.spark.createDataFrame([(x,)], "a long")
                    self.assertEqual(df.select(sq_udf("a")).first()[0], expected)

    def test_udf_transpile_assigned_lambda_transpiles(self):
        # A lambda bound to a name parses as ``Assign(value=Lambda)``, which
        # the extractor handles, so it transpiles.
        adder = lambda v: v + 1  # noqa: E731
        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(adder, LongType())
            self.assertTrue(pudf.transpiled, "assigned lambda should transpile")
            df = self.spark.createDataFrame([(1,), (10,)], "a long")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [2, 11])

    def test_udf_transpile_inline_and_wrapped_lambda_fall_back(self):
        # ``inspect.getsource`` on an inline or wrapped lambda returns the
        # enclosing ``Assign(value=Call(...))``; the extractor has no
        # ``ast.Call`` branch, so these fall back to interpreted Python (the
        # docstring's "wrapped lambda" support is aspirational). Each must
        # still compute the correct value.
        import functools

        def wrapper(fn):
            return fn

        with self.sql_conf(_TRANSPILE_ON):
            inline = UserDefinedFunction(lambda v: v + 1, LongType())
            self.assertEqual([], inline.transpiled, "inline lambda must fall back")

            wrapped = UserDefinedFunction(wrapper(lambda v: v + 1), LongType())
            self.assertEqual([], wrapped.transpiled, "wrapped lambda must fall back")

            base = lambda v, w: v + w  # noqa: E731
            partial = UserDefinedFunction(functools.partial(base, 1), LongType())
            self.assertEqual([], partial.transpiled, "functools.partial must fall back")

            df = self.spark.createDataFrame([(4,)], "a long")
            self.assertEqual(df.select(inline("a")).first()[0], 5)
            self.assertEqual(df.select(wrapped("a")).first()[0], 5)
            self.assertEqual(df.select(partial("a")).first()[0], 5)

    def test_udf_transpile_callable_object_multi_arg(self):
        # A callable instance carries ``self`` as the first parameter; the
        # extractor offsets it so ``a``/``b`` map to _udf_param_0/_udf_param_1.
        # A non-commutative body proves both the offset and the order.
        class SubAB:
            def __call__(self, a, b):
                if a is not None and b is not None:
                    return a - b

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(SubAB(), LongType())
            self.assertTrue(pudf.transpiled, "callable object should transpile")
            for a, b, expected in [(5, 3, 2), (3, 5, -2)]:
                with self.subTest(a=a, b=b):
                    df = self.spark.createDataFrame([(a, b)], "a long, b long")
                    self.assertEqual(df.select(pudf("a", "b")).first()[0], expected)

    def test_udf_transpile_not_of_comparison(self):
        # ``not (x > 0)`` -- the operand is a Compare (statically boolean), so
        # ``not`` lowers (the positive control for the bare-``not`` fallbacks).
        def not_positive(x):
            if x is not None:
                return not (x > 0)

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(not_positive, BooleanType())
            self.assertTrue(pudf.transpiled, "not(comparison) should transpile")
            df = self.spark.createDataFrame([(1,), (0,), (-1,), (None,)], "a long")
            self.assertEqual(
                [r[0] for r in df.select(pudf("a")).collect()],
                [False, True, True, None],
            )

    def test_udf_transpile_nested_boolean(self):
        # Compose and/or over comparisons: ``(x > 0 and y > 0) or z == 0``.
        def nested(x, y, z):
            if x is not None and y is not None:
                return (x > 0 and y > 0) or z == 0

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(nested, BooleanType())
            self.assertTrue(pudf.transpiled, "nested boolean should transpile")
            for x, y, z, expected in [
                (1, 1, 5, True),
                (-1, 1, 0, True),
                (-1, 1, 5, False),
                (1, -1, 9, False),
            ]:
                with self.subTest(x=x, y=y, z=z):
                    df = self.spark.createDataFrame([(x, y, z)], "a long, b long, c long")
                    self.assertEqual(df.select(pudf("a", "b", "c")).first()[0], expected)

    def test_udf_transpile_string_equality_and_ordering(self):
        # String columns transpile for ``==`` and ``<`` like numerics do.
        def eq_foo(x):
            if x is not None:
                return x == "foo"

        def lt_m(x):
            if x is not None:
                return x < "m"

        with self.sql_conf(_TRANSPILE_ON):
            eq_udf = UserDefinedFunction(eq_foo, BooleanType())
            lt_udf = UserDefinedFunction(lt_m, BooleanType())
            self.assertTrue(eq_udf.transpiled and lt_udf.transpiled)
            eq_df = self.spark.createDataFrame([("foo",), ("bar",), (None,)], "a string")
            self.assertEqual(
                [r[0] for r in eq_df.select(eq_udf("a")).collect()], [True, False, None]
            )
            lt_df = self.spark.createDataFrame([("a",), ("z",), (None,)], "a string")
            self.assertEqual(
                [r[0] for r in lt_df.select(lt_udf("a")).collect()], [True, False, None]
            )

    def test_udf_transpile_if_elif_else(self):
        # ``elif`` is a nested ``If`` in the else slot (one statement), so a
        # null-safe if/elif/else chain transpiles.
        def classify(x):
            if x is None:
                return -1
            elif x == 0:
                return 0
            else:
                return 1

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(classify, LongType())
            self.assertTrue(pudf.transpiled, "if/elif/else should transpile")
            df = self.spark.createDataFrame([(None,), (0,), (5,), (-3,)], "a long")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [-1, 0, 1, 1])

    def test_udf_transpile_filter_elision(self):
        # A transpiled boolean UDF used in ``filter`` is elided like one used
        # in ``select`` (extends coverage beyond projection).
        def gt5(x):
            if x is not None:
                return x > 5

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(gt5, BooleanType())
            self.assertTrue(pudf.transpiled, "filter predicate should transpile")
            df = self.spark.createDataFrame([(3,), (7,), (1,), (None,)], "a long")
            fdf = df.filter(pudf("a"))
            self.assertEqual([r[0] for r in fdf.collect()], [7])
            self.assertEqual(
                0,
                self._eval_python_count(fdf),
                "transpiled filter predicate should leave no Python eval node",
            )

    def test_udf_transpile_mixed_chain_elision(self):
        # Non-convertible (closure) -> convertible (``x + 1``) -> non-convertible
        # (``/``). Only the middle UDF is rewritten; it is inlined into the
        # outer UDF's argument, leaving exactly two Python eval nodes.
        offset = 3

        def add_offset(x):  # closure -> fallback
            if x is not None:
                return x + offset

        def plus_one(x):  # convertible
            if x is not None:
                return x + 1

        def div_two(x):  # `/` -> fallback
            if x is not None:
                return x / 2

        with self.sql_conf(_TRANSPILE_ON):
            u1 = UserDefinedFunction(add_offset, LongType())
            u2 = UserDefinedFunction(plus_one, LongType())
            u3 = UserDefinedFunction(div_two, DoubleType())
            self.assertEqual([], u1.transpiled, "closure UDF must fall back")
            self.assertTrue(u2.transpiled, "middle UDF must transpile")
            self.assertEqual([], u3.transpiled, "division UDF must fall back")
            df = self.spark.createDataFrame([(10,)], "a long")
            chained = (
                df.select(u1("a").alias("x1"))
                .select(u2("x1").alias("x2"))
                .select(u3("x2").alias("x3"))
            )
            self.assertEqual(chained.first()[0], 7.0)  # ((10 + 3) + 1) / 2
            self.assertEqual(
                2,
                self._eval_python_count(chained),
                "only the two non-convertible UDFs should remain as eval nodes",
            )

    def test_udf_transpile_config_toggle_no_stale_nodes(self):
        # A UDF built with the flags ON carries a transpiled expression, but
        # executing it with the flags OFF must fall back cleanly to interpreted
        # Python (the optimizer drops the transpiled node) -- no stale or
        # unevaluable plan node, and a correct result.
        def plus_one(x):
            if x is not None:
                return x + 1

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(plus_one, LongType())
            self.assertTrue(pudf.transpiled, "should transpile while flags are on")

        with self.sql_conf(
            {
                "spark.sql.experimental.optimizer.transpilePyUDFs": False,
                "spark.sql.ansi.enabled": False,
            }
        ):
            df = self.spark.createDataFrame([(1,), (5,)], "a long")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [2, 6])

    def test_udf_transpile_return_type_cast(self):
        # The lowered expression is cast to the declared return type, so the
        # output schema and values follow the declared type.
        def plus_one(x):
            if x is not None:
                return x + 1

        with self.sql_conf(_TRANSPILE_ON):
            double_udf = UserDefinedFunction(plus_one, DoubleType())
            long_udf = UserDefinedFunction(plus_one, LongType())
            self.assertTrue(double_udf.transpiled and long_udf.transpiled)
            df = self.spark.createDataFrame([(1,)], "a long")
            double_col = df.select(double_udf("a").alias("r"))
            self.assertEqual(double_col.schema["r"].dataType, DoubleType())
            self.assertEqual(double_col.first()[0], 2.0)
            self.assertEqual(df.select(long_udf("a")).first()[0], 2)

    def test_udf_transpile_reversed_operand_comparison(self):
        # Literal-on-the-left comparisons exercise the asymmetry in
        # ``_lower_value_compare`` / ``_lower_eq`` (which take the left column
        # and the right AST node).
        def lit_lt(x):
            if x is not None:
                return 0 < x

        def lit_eq(x):
            return 5 == x

        def none_eq(x):
            return None == x  # noqa: E711

        with self.sql_conf(_TRANSPILE_ON):
            lt_udf = UserDefinedFunction(lit_lt, BooleanType())
            eq_udf = UserDefinedFunction(lit_eq, BooleanType())
            none_udf = UserDefinedFunction(none_eq, BooleanType())
            self.assertTrue(lt_udf.transpiled and eq_udf.transpiled and none_udf.transpiled)

            lt_df = self.spark.createDataFrame([(1,), (0,), (-1,), (None,)], "a long")
            self.assertEqual(
                [r[0] for r in lt_df.select(lt_udf("a")).collect()],
                [True, False, False, None],
            )
            eq_df = self.spark.createDataFrame([(5,), (3,), (None,)], "a long")
            self.assertEqual(
                [r[0] for r in eq_df.select(eq_udf("a")).collect()],
                [True, False, False],
            )
            none_df = self.spark.createDataFrame([(None,), (5,)], "a long")
            self.assertEqual(
                [r[0] for r in none_df.select(none_udf("a")).collect()],
                [True, False],
            )

    def test_udf_transpile_column_to_column_ordering(self):
        # Ordering comparison between two parameters (not a literal).
        def a_lt_b(a, b):
            if a is not None and b is not None:
                return a < b

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(a_lt_b, BooleanType())
            self.assertTrue(pudf.transpiled, "column-to-column ordering should transpile")
            for a, b, expected in [(1, 2, True), (2, 1, False), (1, 1, False)]:
                with self.subTest(a=a, b=b):
                    df = self.spark.createDataFrame([(a, b)], "a long, b long")
                    self.assertEqual(df.select(pudf("a", "b")).first()[0], expected)

    def test_udf_transpile_nested_unary(self):
        # Stacked unary ops: ``- -x`` (USub of USub) and ``+(-x)`` (UAdd of
        # USub, where UAdd is identity).
        def double_neg(x):
            if x is not None:
                return - -x

        def plus_neg(x):
            if x is not None:
                return +(-x)

        with self.sql_conf(_TRANSPILE_ON):
            dn_udf = UserDefinedFunction(double_neg, LongType())
            pn_udf = UserDefinedFunction(plus_neg, LongType())
            self.assertTrue(dn_udf.transpiled and pn_udf.transpiled)
            df = self.spark.createDataFrame([(5,), (-3,)], "a long")
            self.assertEqual([r[0] for r in df.select(dn_udf("a")).collect()], [5, -3])
            self.assertEqual([r[0] for r in df.select(pn_udf("a")).collect()], [-5, 3])

    def test_udf_transpile_constant_body(self):
        # A body that ignores its parameter and returns a constant lowers to a
        # literal (no parameter placeholder needed).
        def always_42(x):
            return 42

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(always_42, LongType())
            self.assertTrue(pudf.transpiled, "constant body should transpile")
            df = self.spark.createDataFrame([(1,), (999,)], "a long")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [42, 42])

    def test_udf_transpile_default_args_fall_back(self):
        # The positional ``_udf_param_N`` placeholder scheme can't represent
        # default / variadic / keyword-only arguments: a call site may omit a
        # defaulted argument, dangling a placeholder past the bound arguments.
        # Such functions must fall back to interpreted Python (regression for
        # the INVALID_UDF_PARAMETER_PLACEHOLDER_INDEX failure).
        def with_default(a, b=0):
            return a + 10 * b

        def with_varargs(a, *rest):
            return a

        def with_kwargs(a, **opts):
            return a

        with self.sql_conf(_TRANSPILE_ON):
            for label, func in [
                ("default", with_default),
                ("varargs", with_varargs),
                ("kwargs", with_kwargs),
            ]:
                with self.subTest(case=label):
                    pudf = UserDefinedFunction(func, LongType())
                    self.assertEqual(
                        [], pudf.transpiled, f"{label}: must fall back, not transpile"
                    )
            # The defaulted UDF still works (interpreted), with and without
            # the optional argument.
            df = self.spark.createDataFrame([(5,)], "a long")
            pudf = UserDefinedFunction(with_default, LongType())
            self.assertEqual(df.select(pudf("a")).first()[0], 5)  # b defaults to 0
            self.assertEqual(df.select(pudf("a", "a")).first()[0], 55)  # 5 + 10 * 5

    # ------------------------------------------------------------------
    # Incorrect-transpilation hazards (correctness audit). Python overloads
    # +, *, % for text and treats NULL / NaN / overflow differently from
    # Spark, and the transpiler has no column-type info, so it assumes numeric
    # operands. String/bytes *literal* operands are now refused (fall back);
    # the cases that still transpile are pinned here as documented divergences
    # so a future change to any of them is noticed.
    # ------------------------------------------------------------------

    def test_udf_transpile_string_literal_operator_overloading_falls_back(self):
        # Python's +, *, % over text (concatenation, repetition, %-formatting)
        # are NOT arithmetic. A string/bytes literal operand makes that
        # statically detectable, so the transpiler falls back rather than emit
        # a numeric op over text (which Spark would reject or miscompute).
        def concat_right(a):
            if a is not None:
                return a + "!"

        def concat_left(a):
            if a is not None:
                return "pre-" + a

        def percent_format(x):
            if x is not None:
                return "n=%d" % x

        def repeat(x):
            if x is not None:
                return "ab" * x

        with self.sql_conf(_TRANSPILE_ON):
            cr = UserDefinedFunction(concat_right, StringType())
            cl = UserDefinedFunction(concat_left, StringType())
            pf = UserDefinedFunction(percent_format, StringType())
            rp = UserDefinedFunction(repeat, StringType())
            for label, pudf in [
                ("concat_right", cr),
                ("concat_left", cl),
                ("percent_format", pf),
                ("repeat", rp),
            ]:
                with self.subTest(case=label):
                    self.assertEqual([], pudf.transpiled, f"{label}: must fall back")
            # Interpreted Python still produces the right text.
            sdf = self.spark.createDataFrame([("hi",)], "a string")
            self.assertEqual(sdf.select(cr("a")).first()[0], "hi!")
            self.assertEqual(sdf.select(cl("a")).first()[0], "pre-hi")
            idf = self.spark.createDataFrame([(5,)], "a long")
            self.assertEqual(idf.select(pf("a")).first()[0], "n=5")
            self.assertEqual(idf.select(rp("a")).first()[0], "ababababab")  # "ab" * 5

    def test_udf_transpile_string_column_arithmetic_known_divergence(self):
        # KNOWN LIMITATION: a string *column* combined with a number can't be
        # detected statically, so it transpiles and diverges from Python --
        # Spark coerces the string to a number, while Python would concatenate,
        # repeat, or raise. Pinned so schema-aware handling (if it ever lands)
        # trips this test.
        def col_plus_5(a):
            if a is not None:
                return a + 5

        def col_times_3(a):
            if a is not None:
                return a * 3

        with self.sql_conf(_TRANSPILE_ON):
            plus_udf = UserDefinedFunction(col_plus_5, LongType())
            times_udf = UserDefinedFunction(col_times_3, StringType())
            self.assertTrue(
                plus_udf.transpiled and times_udf.transpiled,
                "string-column arithmetic still transpiles today (no schema info)",
            )
            # "10" + 5 -> Spark coerces to 15; interpreted Python raises TypeError.
            num_df = self.spark.createDataFrame([("10",)], "a string")
            self.assertEqual(num_df.select(plus_udf("a")).first()[0], 15)
            # "2" * 3 -> Spark coerces to "6"; interpreted Python gives "222".
            two_df = self.spark.createDataFrame([("2",)], "a string")
            self.assertEqual(two_df.select(times_udf("a")).first()[0], "6")

    def test_udf_transpile_unguarded_arithmetic_on_null_known_divergence(self):
        # KNOWN DIVERGENCE: arithmetic is not null-guarded (unlike the ordering
        # comparisons, which raise to mirror Python). An UNguarded `x + 1` thus
        # yields NULL on a NULL input, whereas interpreted Python raises
        # TypeError (None + 1). Guard with `is not None` for parity.
        def add_one_unguarded(x):
            return x + 1

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(add_one_unguarded, LongType())
            self.assertTrue(pudf.transpiled)
            df = self.spark.createDataFrame([(None,), (5,)], "a long")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [None, 6])

    def test_udf_transpile_overflow_and_modulo_by_zero_raise(self):
        # Under ANSI the transpiled arithmetic raises on overflow and on
        # modulo-by-zero. Interpreted Python would promote `x * x` to a big int
        # (a divergence; see SPARK-55210), and raises ZeroDivisionError for
        # `x % 0` (compatible -- both raise).
        def square(x):
            if x is not None:
                return x * x

        def mod_zero(x):
            if x is not None:
                return x % 0

        with self.sql_conf(_TRANSPILE_ON):
            sq = UserDefinedFunction(square, LongType())
            mz = UserDefinedFunction(mod_zero, LongType())
            self.assertTrue(sq.transpiled and mz.transpiled)
            big = self.spark.createDataFrame([(4000000000,)], "a long")  # 4e9^2 overflows long
            with self.assertRaises(Exception) as ctx:
                big.select(sq("a")).collect()
            self.assertIn("OVERFLOW", str(ctx.exception).upper())
            zero = self.spark.createDataFrame([(5,)], "a long")
            with self.assertRaises(Exception) as ctx2:
                zero.select(mz("a")).collect()
            self.assertIn("ZERO", str(ctx2.exception).upper())

    def test_udf_transpile_nan_comparison_known_divergence(self):
        # KNOWN DIVERGENCE: Spark orders NaN as greater than every value, so a
        # transpiled `x > 0` returns True for NaN, whereas interpreted Python
        # (`nan > 0`) returns False. The null-guard checks isNull, not isNaN.
        def gt_zero(x):
            if x is not None:
                return x > 0

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(gt_zero, BooleanType())
            self.assertTrue(pudf.transpiled)
            df = self.spark.createDataFrame([(float("nan"),), (1.0,)], "a double")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [True, True])

    def test_udf_transpile_cross_type_equality_coerces(self):
        # KNOWN DIVERGENCE: comparing a column to a literal of a different type
        # coerces in Spark, so a transpiled `x == "5"` on an int column is True
        # for 5, whereas Python's `5 == "5"` is False (no cross-type coercion).
        # Equality with a string literal is intentionally NOT forced to fall
        # back, since `stringcol == "5"` is a legitimate, correct comparison.
        def eq_str_five(x):
            if x is not None:
                return x == "5"

        with self.sql_conf(_TRANSPILE_ON):
            pudf = UserDefinedFunction(eq_str_five, BooleanType())
            self.assertTrue(pudf.transpiled)
            df = self.spark.createDataFrame([(5,), (3,)], "a long")
            self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [True, False])


if __name__ == "__main__":
    from pyspark.testing import main

    main()
