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


# Fixtures for the scope-capture tests (SPARK-55207).
_CAPTURED_INT = 7
_CAPTURED_STR = "-suffix"
_CAPTURED_LIST = [1, 2]
# Mutated by the capture-timing tests; see ``_timing_probe``.
_TIMING_VALUE = 3


def _importable_reads_global(x):
    return x + _CAPTURED_INT


_module_lambda_reads_global = lambda x: x + _CAPTURED_INT  # noqa: E731
_timing_reads_global = lambda x: x + _TIMING_VALUE  # noqa: E731


def _make_adder(off):
    """A lambda capturing ``off`` as a closure cell (the factory pattern)."""
    return lambda x: x + off


def _make_nested_adder(a):
    def mid(b):
        def inner(x):
            return x + a + b

        return inner

    return mid(20)


def _make_capturing_def(value):
    def captures(x):
        return x + value

    return captures


def _make_returning_capture(value):
    return lambda x: value


def _make_bool_capture(flag):
    return lambda x: (x > 0) and flag


def _make_none_capture(nothing):
    return lambda x: x if nothing is None else 0


class _AddInstanceAttr:
    """``self.k`` lives in the instance ``__dict__``, so cloudpickle snapshots it."""

    def __init__(self, k):
        self.k = k

    def __call__(self, x):
        return x + self.k


class _AddClassAttr:
    """A class attribute on an importable class follows the class's pickling mode.

    The class is importable from this module, so it is pickled by reference and
    the attribute must not be baked.
    """

    K = 3

    def __call__(self, x):
        return x + self.K


class _AttrBase:
    """Base holding a class attribute, to exercise the MRO walk."""

    K = 3


class _AddInheritedClassAttr(_AttrBase):
    """`self.K` resolves through the MRO to an importable base -- also live."""

    def __call__(self, x):
        return x + self.K


class _BoundMethodHolder:
    """A bound method used as a UDF: the receiver comes from ``__self__``."""

    def __init__(self, k):
        self.k = k

    def add_k(self, x):
        return x + self.k

    def add_global(self, x):
        """Reads a module global, to pin the by-reference gate for bound methods.

        The gate must key off this (importable) class, not off
        ``type(bound_method)`` -- that is always ``types.MethodType``, which has
        no importable qualified name and so looks by-value to cloudpickle.
        """
        return x + _CAPTURED_INT


class _AddPropertyAttr:
    def __init__(self):
        self.n = 1

    @property
    def k(self):
        return 5

    def __call__(self, x):
        return x + self.k


class _AddSlotsAttr:
    __slots__ = ("k",)

    def __init__(self, k):
        self.k = k

    def __call__(self, x):
        return x + self.k


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

        cases = [
            ("divide_by_two", divide_by_two, DoubleType(), Row(a=4.0), 2.0),
            ("floor_divide_by_two", floor_divide_by_two, LongType(), Row(a=5), 2),
            ("bit_and_one", bit_and_one, LongType(), Row(a=5), 1),
            ("bit_or_one", bit_or_one, LongType(), Row(a=4), 5),
            ("left_shift", left_shift, LongType(), Row(a=3), 6),
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
        # Python's `None and x` short-circuits to None; Spark's three-valued
        # `null AND false` is false. A literal None operand must force a
        # fallback rather than silently diverge.
        import warnings as _warnings
        from pyspark.sql.types import StructField, StructType

        def none_and(x):
            return None and (x > 0)

        long_schema = StructType([StructField("a", LongType(), nullable=True)])
        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True):
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(none_and, BooleanType())
            self.assertEqual([], pudf.transpiled, "literal None in and/or must not transpile")
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
        gt5 = lambda x: (x > 5) if x is not None else None  # noqa: E731
        floor_div = lambda x: x // 2  # noqa: E731  `//` -> fallback
        plus_one = lambda x: x + 1  # noqa: E731  convertible
        div_two = lambda x: x / 2  # noqa: E731  `/` -> fallback
        with self.sql_conf(_TRANSPILE_ON):
            f = UserDefinedFunction(gt5, BooleanType())
            self.assertTrue(f.transpiled)
            fdf = self.spark.createDataFrame([(3,), (7,), (1,), (None,)], "a long").filter(f("a"))
            self.assertEqual([r[0] for r in fdf.collect()], [7])
            self.assertEqual(0, self._eval_python_count(fdf))

            u1 = UserDefinedFunction(floor_div, LongType())
            u2 = UserDefinedFunction(plus_one, LongType())
            u3 = UserDefinedFunction(div_two, DoubleType())
            self.assertEqual(([], True, []), (u1.transpiled, bool(u2.transpiled), u3.transpiled))
            chained = (
                self.spark.createDataFrame([(10,)], "a long")
                .select(u1("a").alias("x"))
                .select(u2("x").alias("y"))
                .select(u3("y").alias("z"))
            )
            self.assertEqual(chained.first()[0], 3.0)  # ((10 // 2) + 1) / 2
            self.assertEqual(2, self._eval_python_count(chained))

    def test_udf_transpile_closure_capture_is_merged(self):
        # A closure-capturing UDF now lowers fully, so no Python eval node
        # survives (this pinned the opposite before SPARK-55207).
        with self.sql_conf(_TRANSPILE_ON):
            u = UserDefinedFunction(_make_adder(3), LongType())
            self.assertTrue(u.transpiled, "closure capture should transpile")
            df = self.spark.createDataFrame([(10,)], "a long").select(u("a").alias("out"))
            self.assertEqual(df.first()[0], 13)
            self.assertEqual(0, self._eval_python_count(df))

    def test_udf_transpile_captured_int_outside_long_range_falls_back(self):
        # Python integers are unbounded, so a capture can exceed LongType. That
        # must be refused by an explicit guard with a readable message -- not by
        # `lit` happening to throw a JVM NumberFormatException -- and the result
        # must still match the interpreted path.
        L = LongType()
        boundary = 2**63 - 1 - 10
        self.assertEqual(self._vals(_make_adder(boundary), L, "a long", [(10,)]), [boundary + 10])
        for label, off in [
            ("2**63", 2**63),
            ("2**70", 2**70),
            ("-(2**70)", -(2**70)),
        ]:
            with self.subTest(case=label):
                pudf, warned = self._fallback_warnings(_make_adder(off), L)
                self.assertEqual([], pudf.transpiled, f"{label} must not be baked")
                self.assertTrue(warned, f"{label}: expected a fallback warning")
                self.assertIn(
                    "outside the range of a 64-bit integer",
                    str(warned[0].message),
                    f"{label}: expected the explicit range guard, not a JVM error",
                )
                # Both paths agree: the interpreted converter nulls a return
                # value that does not fit the declared LongType.
                with self.sql_conf(_TRANSPILE_ON):
                    df = self.spark.createDataFrame([(10,)], "a long")
                    self.assertIsNone(df.select(pudf("a")).collect()[0][0], label)
        # A captured bool subclasses int but is only ever 0/1, so it stays
        # eligible for the type check rather than tripping the range guard.
        pudf, _ = self._fallback_warnings(_make_adder(True), L)
        with self.sql_conf(_TRANSPILE_ON):
            df = self.spark.createDataFrame([(10,)], "a long")
            self.assertEqual(df.select(pudf("a")).collect()[0][0], 11)

    def test_udf_transpile_repeated_lowering_is_idempotent(self):
        # A UDF is lowered more than once: at construction to validate it, on
        # every `.transpiled` access, and again when its judf is built. The
        # normalization pass rewrites the AST in place, so it must work on a
        # copy -- otherwise each build re-inlines the previous build's output and
        # `a = a + 1; return a * 2` walks from `(a + 1) * 2` to
        # `((a + 1) + 1) * 2` to `(((a + 1) + 1) + 1) * 2`. That produces a wrong
        # answer rather than a fallback, so pin the invariant directly instead of
        # relying on other tests to exercise it incidentally.
        import ast as _ast

        from pyspark.sql.transpile import _analyze_func, _normalize_function

        def rebinds(a):
            a = a + 1
            return a * 2

        analysis, errors = _analyze_func(rebinds, LongType())
        self.assertIsNotNone(analysis, f"expected an analysis, got {errors}")
        before = _ast.dump(analysis.function_ast)
        lowered = [
            _ast.unparse(
                _normalize_function(rebinds, analysis.function_ast, analysis.params).body[0]
            )
            for _ in range(5)
        ]
        self.assertEqual(1, len(set(lowered)), f"repeated lowering drifted: {lowered}")
        self.assertEqual(
            before,
            _ast.dump(analysis.function_ast),
            "normalization mutated the stored AST instead of a copy",
        )

        # And end to end: many `.transpiled` accesses before executing must not
        # change the answer.
        with self.sql_conf(_TRANSPILE_ON):
            u = UserDefinedFunction(rebinds, LongType())
            for _ in range(4):
                self.assertTrue(u.transpiled)
            df = self.spark.createDataFrame([(10,)], "a long")
            self.assertEqual(rebinds(10), 22, "bad test expectation")
            self.assertEqual(df.select(u("a")).collect()[0][0], 22)

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

    # ------------------------------------------------------------------
    # Scope capture and local assignment (SPARK-55207).
    #
    # A captured value may only be baked into the plan as a literal when
    # cloudpickle would have snapshotted the same value for the interpreted
    # path, so these tests are organised around that boundary rather than
    # around Python's own name-resolution rules.
    # ------------------------------------------------------------------

    def _fallback_warnings(self, func, return_type):
        """Build ``func`` with transpilation on; return (udf, fallback warnings)."""
        import warnings as _warnings

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(func, return_type)
            return pudf, [w for w in caught if "Unable to transpile" in str(w.message)]

    def test_udf_transpile_captures_closure_freevars(self):
        # A closure cell always travels inside the pickled payload, so it is
        # safe to bake regardless of where the UDF was defined.
        L = LongType()
        cases = [
            ("factory lambda", _make_adder(3), L, "a long", [(10,), (-1,)], [13, 2]),
            ("factory def", _make_capturing_def(5), L, "a long", [(10,)], [15]),
            ("two-level nesting", _make_nested_adder(1), L, "a long", [(10,)], [31]),
        ]
        for label, func, rt, schema, rows, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(self._vals(func, rt, schema, rows), expected, label)

    def test_udf_transpile_captures_basic_types(self):
        # Each bakeable value type, in the position where its category matters.
        L, B, S = LongType(), BooleanType(), StringType()
        cases = [
            ("int", _make_adder(3), L, "a long", [(1,)], [4]),
            ("float", _make_adder(0.5), DoubleType(), "a double", [(1.0,)], [1.5]),
            ("str concat", _make_adder("!"), S, "a string", [("x",)], ["x!"]),
            ("bool in and", _make_bool_capture(True), B, "a long", [(1,), (-1,)], [True, False]),
            ("bool False", _make_bool_capture(False), B, "a long", [(1,)], [False]),
            ("None identity", _make_none_capture(None), L, "a long", [(7,)], [7]),
            (
                "bytes value",
                _make_returning_capture(b"ab"),
                BinaryType(),
                "a binary",
                [(b"z",)],
                [bytearray(b"ab")],
            ),
        ]
        for label, func, rt, schema, rows, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(self._vals(func, rt, schema, rows), expected, label)

    def test_udf_transpile_global_baked_only_when_pickled_by_value(self):
        # A module-level lambda has no importable qualified name, so cloudpickle
        # snapshots it (and its globals) by value -> safe to bake.
        self.assertEqual(
            self._vals(_module_lambda_reads_global, LongType(), "a long", [(1,)]),
            [1 + _CAPTURED_INT],
        )
        # A top-level ``def`` in this (importable) module is pickled BY
        # REFERENCE: the executor re-imports the module and reads whatever the
        # global holds there, so baking the driver's value could disagree. It
        # must fall back -- and still compute the right answer.
        pudf, warned = self._fallback_warnings(_importable_reads_global, LongType())
        self.assertEqual([], pudf.transpiled, "globals of a by-reference UDF must not be baked")
        self.assertTrue(warned, "expected a fallback warning")
        self.assertIn("pickled by reference", str(warned[0].message))
        with self.sql_conf(_TRANSPILE_ON):
            df = self.spark.createDataFrame([(1,)], "a long")
            self.assertEqual(df.select(pudf("a")).collect()[0][0], 1 + _CAPTURED_INT)

    def test_udf_transpile_register_pickle_by_value_opens_the_gate(self):
        # Registering this module for by-value pickling makes cloudpickle
        # snapshot its globals, so the transpiler may bake them -- the two stay
        # consistent because they consult the same predicate. This is the
        # documented way to get a top-level ``def`` transpiled.
        import sys

        from pyspark import cloudpickle

        module = sys.modules[__name__]
        cloudpickle.register_pickle_by_value(module)
        try:
            self.assertEqual(
                self._vals(_importable_reads_global, LongType(), "a long", [(1,)]),
                [1 + _CAPTURED_INT],
            )
        finally:
            cloudpickle.unregister_pickle_by_value(module)
        # Back to falling back once unregistered.
        pudf, _ = self._fallback_warnings(_importable_reads_global, LongType())
        self.assertEqual([], pudf.transpiled)

    def test_udf_transpile_falls_back_for_non_bakeable_captures(self):
        # Only None and basic scalars have a column equivalent. Everything else
        # must fall back rather than guess -- and in the recursive case the
        # captured value is the function itself.
        import decimal

        def _recursive(x):
            return _recursive(x)

        cases = [
            ("list", _make_adder(_CAPTURED_LIST)),
            ("dict", _make_returning_capture({"a": 1})),
            ("tuple", _make_returning_capture((1, 2))),
            ("function", _make_returning_capture(len)),
            ("module", _make_returning_capture(unittest)),
            ("Decimal", _make_adder(decimal.Decimal("1.5"))),
            ("complex", _make_adder(complex(1, 2))),
            ("recursive self-reference", _recursive),
        ]
        for label, func in cases:
            with self.subTest(case=label):
                pudf, warned = self._fallback_warnings(func, LongType())
                self.assertEqual([], pudf.transpiled, f"{label} must not transpile")
                self.assertTrue(warned, f"{label}: expected a fallback warning")

    def test_udf_transpile_falls_back_for_unresolvable_names(self):
        # ``global`` / ``nonlocal`` rebind names outside the function, and a
        # name that resolves to nothing at all cannot be baked.
        def uses_global_stmt(x):
            global _CAPTURED_INT
            return x + _CAPTURED_INT

        def make_nonlocal():
            captured = 1

            def inner(x):
                nonlocal captured
                return x + captured

            return inner

        def undefined_name(x):
            return x + _not_defined_anywhere  # noqa: F821

        for label, func in [
            ("global statement", uses_global_stmt),
            ("nonlocal statement", make_nonlocal()),
            ("undefined name", undefined_name),
        ]:
            with self.subTest(case=label):
                pudf, warned = self._fallback_warnings(func, LongType())
                self.assertEqual([], pudf.transpiled, f"{label} must not transpile")
                self.assertTrue(warned, f"{label}: expected a fallback warning")

    def test_udf_transpile_self_attr_capture(self):
        # An instance ``__dict__`` attribute is pickled by value -> bakeable,
        # whether the UDF is a callable instance or a bound method (the latter
        # takes its receiver from ``__self__``).
        self.assertEqual(
            self._vals(_AddInstanceAttr(4), LongType(), "a long", [(1,), (10,)]), [5, 14]
        )
        self.assertEqual(
            self._vals(_BoundMethodHolder(4).add_k, LongType(), "a long", [(1,), (10,)]),
            [5, 14],
        )
        # A class attribute follows the class's own pickling mode -- by reference
        # here, whether declared on the class or inherited through the MRO, so
        # the executor would re-read it. A descriptor's value is computed per
        # process, and a ``__slots__`` instance has no ``__dict__``. All must
        # fall back while still producing the interpreted result.
        for label, func, expected in [
            ("class attribute", _AddClassAttr(), 1 + _AddClassAttr.K),
            ("inherited class attribute", _AddInheritedClassAttr(), 1 + _AttrBase.K),
            ("property", _AddPropertyAttr(), 6),
            ("__slots__", _AddSlotsAttr(2), 3),
        ]:
            with self.subTest(case=label):
                pudf, warned = self._fallback_warnings(func, LongType())
                self.assertEqual([], pudf.transpiled, f"{label} must not transpile")
                self.assertTrue(warned, f"{label}: expected a fallback warning")
                with self.sql_conf(_TRANSPILE_ON):
                    df = self.spark.createDataFrame([(1,)], "a long")
                    self.assertEqual(df.select(pudf("a")).collect()[0][0], expected, label)

    def test_udf_transpile_bound_method_globals_follow_its_class(self):
        # A bound method's globals are snapshotted only when its CLASS is pickled
        # by value. The class here is importable, so the executor re-imports it
        # and re-reads the global -- the driver's value must not be baked.
        # ``type(bound_method)`` is ``types.MethodType``, which has no importable
        # qualified name and so looks by-value to cloudpickle; keying the gate off
        # it would wave through every bound method's globals.
        holder = _BoundMethodHolder(4)
        pudf, warned = self._fallback_warnings(holder.add_global, LongType())
        self.assertEqual(
            [], pudf.transpiled, "globals of a by-reference bound method must not be baked"
        )
        self.assertTrue(warned, "expected a fallback warning")
        self.assertIn("pickled by reference", str(warned[0].message))
        with self.sql_conf(_TRANSPILE_ON):
            df = self.spark.createDataFrame([(1,)], "a long")
            self.assertEqual(df.select(pudf("a")).collect()[0][0], 1 + _CAPTURED_INT)

    def test_udf_transpile_self_attr_gated_per_class_in_the_mro(self):
        # cloudpickle decides by reference vs by value per class: a by-value class
        # ships only its own ``__dict__``, and each base is pickled separately.
        # This subclass is dynamic (defined in a function body, so not importable)
        # but inherits ``K`` from an importable base, which the executor
        # re-imports and re-reads -- so ``K`` must not be baked even though the
        # most-derived class is by value.
        class _DynamicSubOfImportableBase(_AttrBase):
            def __call__(self, x):
                return x + self.K

        func = _DynamicSubOfImportableBase()
        pudf, warned = self._fallback_warnings(func, LongType())
        self.assertEqual(
            [],
            pudf.transpiled,
            "an attribute inherited from a by-reference base must not be baked",
        )
        self.assertTrue(warned, "expected a fallback warning")
        self.assertIn("pickles by reference", str(warned[0].message))
        with self.sql_conf(_TRANSPILE_ON):
            df = self.spark.createDataFrame([(1,)], "a long")
            self.assertEqual(df.select(pudf("a")).collect()[0][0], 1 + _AttrBase.K)

    def test_udf_transpile_assignment_forms(self):
        # Local bindings are inlined at their use sites; every assignment form
        # the transpiler accepts is exercised here against real values.
        L, B, S = LongType(), BooleanType(), StringType()

        def single(a):
            b = a + 1
            return b * 2

        def reuse(a):
            b = a + 1
            return b * b

        def multi_target(a):
            b = c = a + 1
            return b + c

        def augmented(a):
            b = a
            b += 5
            b -= 1
            b *= 2
            b %= 7
            return b

        def annotated(a):
            b: int = a + 1
            return b

        def reassigned(a):
            b = a + 1
            b = b + 1
            return b

        def chain_of_three(a):
            b = a + 1
            c = b + 2
            d = c + 3
            return d

        def uses_capture(a):
            b = a + 1
            return b * 2

        def shadows_global(a):
            _CAPTURED_INT = 100
            return a + _CAPTURED_INT

        def string_repeat(n):
            s = "ab"
            return s * n

        def boolean_binding(x):
            b = x > 0
            return b

        def two_params(a, b):
            total = a + b
            doubled = total + total
            return doubled

        cases = [
            ("single", single, L, "a long", [(10,)], [22]),
            ("reuse", reuse, L, "a long", [(3,)], [16]),
            ("multi target", multi_target, L, "a long", [(3,)], [8]),
            ("augmented", augmented, L, "a long", [(3,)], [0]),
            ("annotated", annotated, L, "a long", [(3,)], [4]),
            ("reassigned", reassigned, L, "a long", [(3,)], [5]),
            ("chain of three", chain_of_three, L, "a long", [(1,)], [7]),
            ("uses capture", uses_capture, L, "a long", [(1,)], [4]),
            ("shadows global", shadows_global, L, "a long", [(1,)], [101]),
            ("string repeat", string_repeat, S, "a long", [(3,)], ["ababab"]),
            ("boolean binding", boolean_binding, B, "a long", [(1,), (-1,)], [True, False]),
            ("two params", two_params, L, "a long, b long", [(2, 3)], [10]),
        ]
        for label, func, rt, schema, rows, expected in cases:
            with self.subTest(case=label):
                # Cross-check the expectation against real Python so a wrong
                # expected value cannot make a wrong lowering look correct.
                self.assertEqual(
                    [func(*row) for row in rows], expected, f"{label}: bad test expectation"
                )
                self.assertEqual(self._vals(func, rt, schema, rows), expected, label)

    def test_udf_transpile_rebinding_a_parameter(self):
        # A parameter is an ordinary local and may be rebound, after which every
        # read must see the new expression. Resolving parameters before the
        # binding table would turn `a = a + 1; return a * 2` into `a * 2`, which
        # returns a wrong answer rather than falling back -- so assert values.
        L = LongType()

        def rebind_once(a):
            a = a + 1
            return a * 2

        def rebind_twice(a):
            a = a + 1
            a = a * 3
            return a

        def rebind_from_other(a, b):
            a = b
            b = a
            return a + b

        def rebind_repeatedly(a):
            a = a + 1
            a = a + 1
            a = a + 1
            a = a + 1
            return a

        def rebind_then_bind(a):
            a = a + 1
            b = a + a
            return b

        cases = [
            ("rebind once", rebind_once, "a long", [(10,)], [22]),
            ("rebind twice", rebind_twice, "a long", [(10,)], [33]),
            ("rebind from other param", rebind_from_other, "a long, b long", [(10, 4)], [8]),
            ("rebind repeatedly", rebind_repeatedly, "a long", [(0,)], [4]),
            ("rebind then bind", rebind_then_bind, "a long", [(10,)], [22]),
        ]
        for label, func, schema, rows, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    [func(*row) for row in rows], expected, f"{label}: bad test expectation"
                )
                self.assertEqual(self._vals(func, L, schema, rows), expected, label)

    def test_udf_transpile_assignment_falls_back(self):
        # Assignment shapes the transpiler refuses. Each must fall back cleanly
        # and still compute the interpreted result.
        def tuple_unpack(a):
            b, c = a, a
            return b + c

        def starred_unpack(a):
            b, *rest = [a, a]
            return b + len(rest)

        def subscript_target(a):
            holder = [0]
            holder[0] = a
            return holder[0]

        def attribute_target(a):
            class Box:
                pass

            box = Box()
            box.v = a
            return box.v

        def statement_after_terminal(a):
            b = a + 1
            if b > 0:
                return b
            return 0

        def read_before_assign(a):
            # Intentionally UnboundLocalError: the assignment below makes the
            # name local for the whole body, so this read cannot see the global.
            y = a + _CAPTURED_INT  # noqa: F823
            _CAPTURED_INT = 1
            return y

        def assign_inside_if(a):
            if a > 0:
                b = a + 1
                return b
            return 0

        cases = [
            ("tuple unpack", tuple_unpack, 20),
            ("starred unpack", starred_unpack, 11),
            ("subscript target", subscript_target, 10),
            ("attribute target", attribute_target, 10),
            ("statement after terminal", statement_after_terminal, 11),
            ("assign inside if", assign_inside_if, 11),
        ]
        for label, func, expected in cases:
            with self.subTest(case=label):
                pudf, warned = self._fallback_warnings(func, LongType())
                self.assertEqual([], pudf.transpiled, f"{label} must not transpile")
                self.assertTrue(warned, f"{label}: expected a fallback warning")
                with self.sql_conf(_TRANSPILE_ON):
                    df = self.spark.createDataFrame([(10,)], "a long")
                    self.assertEqual(df.select(pudf("a")).collect()[0][0], expected, label)

        # ``read_before_assign`` is UnboundLocalError in Python because the
        # assignment makes the name local for the whole body. The transpiler
        # must refuse rather than resolve it from the module globals, which
        # would return a value where the UDF actually raises.
        with self.subTest(case="read before assign"):
            pudf, warned = self._fallback_warnings(read_before_assign, LongType())
            self.assertEqual([], pudf.transpiled)
            self.assertTrue(warned)
            self.assertRaises(UnboundLocalError, read_before_assign, 1)
            with self.sql_conf(_TRANSPILE_ON):
                df = self.spark.createDataFrame([(10,)], "a long")
                with self.assertRaises(Exception):
                    df.select(pudf("a")).collect()

    def test_udf_transpile_assignment_node_budget(self):
        # Inlining duplicates a binding at each use, so a doubling chain grows
        # exponentially. A modest chain still transpiles; a deep one must fall
        # back rather than emit a huge plan.
        def shallow_chain(a):
            b = a + a
            c = b + b
            d = c + c
            return d + d

        def deep_chain(a):
            z0 = a + a
            z1 = z0 + z0
            z2 = z1 + z1
            z3 = z2 + z2
            z4 = z3 + z3
            z5 = z4 + z4
            z6 = z5 + z5
            z7 = z6 + z6
            z8 = z7 + z7
            z9 = z8 + z8
            return z9 + z9

        self.assertEqual(self._vals(shallow_chain, LongType(), "a long", [(1,)]), [16])
        pudf, warned = self._fallback_warnings(deep_chain, LongType())
        self.assertEqual([], pudf.transpiled, "a deep inlining chain must fall back")
        self.assertTrue(warned)
        self.assertIn("nodes", str(warned[0].message))
        # The budget must be enforced per BINDING, not only on the finished body.
        # Checking only at the end means building the whole exponential tree
        # first: 20 doubling assignments reach ~8.4M nodes, which took ~147s and
        # ~3.6GB at UDF construction before falling back anyway. The message
        # naming the binding is the observable proof it was caught early -- a
        # regression here is a driver hang rather than a wrong answer, so it
        # would otherwise be easy to miss.
        self.assertIn(
            "the binding of",
            str(warned[0].message),
            "the budget must be enforced while inlining each binding, not "
            "only once the whole body has been built",
        )
        with self.sql_conf(_TRANSPILE_ON):
            df = self.spark.createDataFrame([(1,)], "a long")
            self.assertEqual(df.select(pudf("a")).collect()[0][0], 2048)

        # A chain deep enough that late checking would be pathological must
        # still be refused promptly. Bounded work, so this cannot hang.
        def very_deep_chain(a):
            z0 = a + a
            z1 = z0 + z0
            z2 = z1 + z1
            z3 = z2 + z2
            z4 = z3 + z3
            z5 = z4 + z4
            z6 = z5 + z5
            z7 = z6 + z6
            z8 = z7 + z7
            z9 = z8 + z8
            z10 = z9 + z9
            z11 = z10 + z10
            z12 = z11 + z11
            z13 = z12 + z12
            z14 = z13 + z13
            z15 = z14 + z14
            z16 = z15 + z15
            z17 = z16 + z16
            z18 = z17 + z17
            z19 = z18 + z18
            return z19 + z19

        pudf, warned = self._fallback_warnings(very_deep_chain, LongType())
        self.assertEqual([], pudf.transpiled)
        self.assertIn("the binding of", str(warned[0].message))

    def test_udf_transpile_no_return_yields_null_and_warns(self):
        # Falling off the end of a function returns None in Python, which the
        # interpreted path surfaces as NULL. Transpile it to a NULL literal and
        # warn, since it is usually a mistake.
        import warnings as _warnings

        def bare_expression(x):
            x + x

        def trailing_assignment(x):
            y = x + 1  # noqa: F841

        def just_pass(x):
            pass

        def docstring_only(x):
            """Documented, but returns nothing."""

        for label, func in [
            ("bare expression", bare_expression),
            ("trailing assignment", trailing_assignment),
            ("pass", just_pass),
            ("docstring only", docstring_only),
        ]:
            with self.subTest(case=label):
                self.assertIsNone(func(1), f"{label}: bad test expectation")
                with self.sql_conf(_TRANSPILE_ON):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        pudf = UserDefinedFunction(func, LongType())
                    self.assertTrue(pudf.transpiled, f"{label} should transpile to NULL")
                    self.assertTrue(
                        [w for w in caught if "no return statement" in str(w.message)],
                        f"{label}: expected a no-return warning",
                    )
                    df = self.spark.createDataFrame([(1,), (2,)], "a long")
                    self.assertEqual([r[0] for r in df.select(pudf("a")).collect()], [None, None])

        # A discarded expression that could raise must NOT be lowered away:
        # dropping it would turn a Python error into a NULL. It still warns,
        # because "always returns None or raises" is worth telling the user
        # regardless of whether we could transpile it.
        def discards_a_raise(x):
            x % 0

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(discards_a_raise, LongType())
            self.assertEqual([], pudf.transpiled, "a discarded raising expression must fall back")
            self.assertTrue(
                [w for w in caught if "no return statement" in str(w.message)],
                "expected a no-return warning even though it fell back",
            )
            self.assertTrue([w for w in caught if "Unable to transpile" in str(w.message)])
            # Python raises, and falling back means the query raises too.
            self.assertRaises(ZeroDivisionError, discards_a_raise, 1)
            df = self.spark.createDataFrame([(1,)], "a long")
            with self.assertRaises(Exception):
                df.select(pudf("a")).collect()

        # A generator also ends in a discarded expression, but calling it
        # returns a generator object rather than None -- so it must fall back
        # WITHOUT claiming the function returns None, which would be untrue.
        def generator_udf(x):
            yield x

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                pudf = UserDefinedFunction(generator_udf, LongType())
            self.assertEqual([], pudf.transpiled, "a generator must not transpile")
            self.assertFalse(
                [w for w in caught if "no return statement" in str(w.message)],
                "a generator does not return None; the no-return warning would lie",
            )

        # The warning is about a MISSING `return`, not about a call that happens
        # to produce None. Once the author wrote a `return`, a path that falls
        # past it is far more likely deliberate than forgotten, so these stay
        # quiet even though each returns None for some input.
        def conditional_return(x):
            if x > 0:
                return 1

        def return_then_trailing_statement(x):
            if x > 0:
                return 1
            y = x  # noqa: F841

        for label, func in [
            ("conditional return", conditional_return),
            ("trailing statement after a return", return_then_trailing_statement),
        ]:
            with self.subTest(case=label):
                self.assertIsNone(func(-1), f"{label}: bad test expectation")
                with self.sql_conf(_TRANSPILE_ON):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        UserDefinedFunction(func, LongType())
                    self.assertFalse(
                        [w for w in caught if "no return statement" in str(w.message)],
                        f"{label}: has a `return`, so the no-return warning is noise",
                    )

        # A `return` in a nested scope is not this function's return, so the
        # outer function still falls off its end and still warns.
        def returns_only_from_a_nested_def(x):
            def inner():
                return x

            inner()

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                UserDefinedFunction(returns_only_from_a_nested_def, LongType())
            self.assertIsNone(returns_only_from_a_nested_def(1), "bad test expectation")
            self.assertTrue(
                [w for w in caught if "no return statement" in str(w.message)],
                "a nested def's `return` is not the outer function's",
            )

        # Statements the old last-statement heuristic did not recognize (a loop,
        # a `with`, ...) return None just the same when nothing returns.
        def loops_without_returning(x):
            for _ in range(x):
                pass

        with self.sql_conf(_TRANSPILE_ON):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                UserDefinedFunction(loops_without_returning, LongType())
            self.assertIsNone(loops_without_returning(1), "bad test expectation")
            self.assertTrue(
                [w for w in caught if "no return statement" in str(w.message)],
                "a body that never returns should warn whatever its last statement is",
            )

        # A lambda has no `return` statement in its source, so a naive "does the
        # body contain an ast.Return" check would warn about every lambda UDF.
        # `_get_function_from_ast` synthesizes `body=[Return(<lambda body>)]`, so
        # the check sees the return the source never spelled out.
        identity = lambda x: x + 1  # noqa: E731

        def make_adder(n):
            # `inspect.getsource` yields just this `return` line, so the lambda
            # arrives through a different unwrapping path than the one above.
            return lambda x: x + n

        for label, func in [("bound lambda", identity), ("factory lambda", make_adder(3))]:
            with self.subTest(case=label):
                self.assertEqual(func(1), 4 if label == "factory lambda" else 2)
                with self.sql_conf(_TRANSPILE_ON):
                    with _warnings.catch_warnings(record=True) as caught:
                        _warnings.simplefilter("always")
                        UserDefinedFunction(func, LongType())
                    self.assertFalse(
                        [w for w in caught if "no return statement" in str(w.message)],
                        f"{label}: a lambda returns its body; the warning would lie",
                    )

    def test_udf_transpile_skips_docstring(self):
        # A docstring binds nothing and cannot raise, so it is dropped and the
        # rest of the body normalized as usual. Docstrings are common enough in
        # real UDFs that refusing them would exclude a lot of ordinary code.
        L = LongType()

        def documented(x):
            """Add one to x."""
            return x + 1

        def documented_with_assignment(x):
            """Add one to x, via a local."""
            b = x + 1
            return b * 2

        def documented_multiline(x):
            """Add one to x.

            A longer docstring, spanning lines, with a blank line in it.
            """
            return x + 1

        cases = [
            ("plain", documented, [(10,)], [11]),
            ("with assignment", documented_with_assignment, [(10,)], [22]),
            ("multiline", documented_multiline, [(10,)], [11]),
        ]
        for label, func, rows, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    [func(*row) for row in rows], expected, f"{label}: bad test expectation"
                )
                self.assertEqual(self._vals(func, L, "a long", rows), expected, label)

        # A string expression that is NOT in the docstring position is an
        # ordinary discarded expression, not a docstring, and only the last
        # statement may discard a value.
        def string_in_the_middle(x):
            b = x + 1
            "not a docstring"
            return b

        pudf, warned = self._fallback_warnings(string_in_the_middle, LongType())
        self.assertEqual(
            [], pudf.transpiled, "a mid-body string must not be treated as a docstring"
        )
        self.assertTrue(warned)

    def _timing_probe(self, mutate_at, transpile):
        """Rebind a captured global at one of three points; return both reads.

        ``mutate_at`` is "P1" (after the UDF is constructed, before it is
        called), "P2" (after the call builds the plan, before the action) or
        "P3" (between two actions).
        """
        global _TIMING_VALUE
        conf = {
            "spark.sql.experimental.optimizer.transpilePyUDFs": transpile,
            "spark.sql.ansi.enabled": True,
        }
        with self.sql_conf(conf):
            _TIMING_VALUE = 3
            try:
                df = self.spark.createDataFrame([(1,)], "a long")
                pudf = UserDefinedFunction(_timing_reads_global, LongType())
                if mutate_at == "P1":
                    _TIMING_VALUE = 100
                column = pudf("a")
                if mutate_at == "P2":
                    _TIMING_VALUE = 100
                first = df.select(column).collect()[0][0]
                if mutate_at == "P3":
                    _TIMING_VALUE = 100
                second = df.select(column).collect()[0][0]
                return first, second
            finally:
                _TIMING_VALUE = 3

    def test_udf_transpile_capture_timing_matches_interpreted(self):
        # The interpreted path snapshots captured values when the UDF is first
        # called (cloudpickle captures by value, and the judf is then cached).
        # The transpiled path bakes its literals at the same moment, so the two
        # must agree no matter when a captured global is rebound. Without that
        # alignment, rebinding at P1 gives 4 transpiled and 101 interpreted.
        for mutate_at in ("P1", "P2", "P3", "none"):
            with self.subTest(mutate_at=mutate_at):
                transpiled = self._timing_probe(mutate_at, True)
                interpreted = self._timing_probe(mutate_at, False)
                self.assertEqual(
                    transpiled,
                    interpreted,
                    f"transpiled and interpreted disagree when the captured "
                    f"global is rebound at {mutate_at}",
                )
        # Pin the actual values so the alignment is visible, not just self
        # consistent: rebinding before the first call is picked up, and
        # rebinding afterwards is not.
        self.assertEqual(self._timing_probe("P1", True), (101, 101))
        self.assertEqual(self._timing_probe("P2", True), (4, 4))
        self.assertEqual(self._timing_probe("P3", True), (4, 4))
        self.assertEqual(self._timing_probe("none", True), (4, 4))

    def test_udf_transpile_capture_timing_for_cells_and_attributes(self):
        # Same alignment for a closure cell and an instance attribute, the other
        # two things cloudpickle snapshots by value.
        def run(kind, mutate_before_call, transpile):
            conf = {
                "spark.sql.experimental.optimizer.transpilePyUDFs": transpile,
                "spark.sql.ansi.enabled": True,
            }
            with self.sql_conf(conf):
                df = self.spark.createDataFrame([(1,)], "a long")
                if kind == "cell":
                    func = _make_adder(3)

                    def mutate():
                        func.__closure__[0].cell_contents = 100

                else:
                    func = _AddInstanceAttr(3)

                    def mutate():
                        func.k = 100

                pudf = UserDefinedFunction(func, LongType())
                if mutate_before_call:
                    mutate()
                column = pudf("a")
                if not mutate_before_call:
                    mutate()
                return df.select(column).collect()[0][0]

        for kind in ("cell", "attribute"):
            for before in (True, False):
                with self.subTest(kind=kind, mutate_before_call=before):
                    self.assertEqual(
                        run(kind, before, True),
                        run(kind, before, False),
                        f"{kind}: transpiled and interpreted disagree",
                    )


if __name__ == "__main__":
    from pyspark.testing import main

    main()
