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
Property-based tests for the UDF transpilation module (pyspark.sql.transpile).

Uses the Hypothesis library to generate random Python AST expression trees and
verify that the CatalystTranspiler handles them correctly.

Tests that exercise ``_convert_chunk`` require an active SparkContext because
the transpiler creates Column objects via ``lit()``, ``col()``, and ``when()``.

Tier 1 tests focus on structure/rejection and use a shared SparkSession but
do not execute Spark queries. Tier 2 (equivalence) tests run actual queries
and are gated behind ``SPARK_RUN_HYPOTHESIS_TESTS=1``.

To run::

    # Tier 1 only (fast, needs hypothesis + pyspark deps)
    pip install hypothesis
    python -m pytest python/pyspark/sql/tests/test_transpile.py -v -k "not Equivalence"

    # All tiers including equivalence integration tests
    SPARK_RUN_HYPOTHESIS_TESTS=1 \\
        python -m pytest python/pyspark/sql/tests/test_transpile.py -v
"""

import ast
import math
import os
import unittest

try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st

    have_hypothesis = True
except ImportError:
    have_hypothesis = False

from pyspark.sql.transpile import (
    CatalystTranspiler,
    _get_function_from_ast,
    _get_parameter_list,
)
from pyspark.sql.column import Column

hypothesis_requirement_message = (
    "hypothesis is required for property-based transpilation tests "
    "(pip install hypothesis)"
)

run_hypothesis_integration = (
    os.environ.get("SPARK_RUN_HYPOTHESIS_TESTS", "0") == "1"
)


def _try_create_spark_session(app_name="test_transpile"):
    """Try to create a SparkSession; return None if Spark/Java is unavailable."""
    try:
        from pyspark.sql import SparkSession

        return (
            SparkSession.builder.master("local[1]")
            .appName(app_name)
            .getOrCreate()
        )
    except Exception:
        return None


# Eagerly probe once at import time so we can skip entire classes.
_spark_available = False
try:
    _probe = _try_create_spark_session("test_transpile_probe")
    _spark_available = _probe is not None
    if _probe is not None:
        _probe.stop()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

if have_hypothesis:
    # Leaf expressions: constants and parameter references
    constant_exprs = st.one_of(
        st.integers(min_value=-1000, max_value=1000).map(
            lambda v: ast.Constant(value=v)
        ),
        st.floats(
            allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6
        ).map(lambda v: ast.Constant(value=v)),
        st.just(ast.Constant(value=None)),
    )

    param_name_strategy = st.sampled_from(["x", "y", "z"])

    leaf_exprs = st.one_of(
        constant_exprs,
        param_name_strategy.map(
            lambda n: ast.Name(id=n, ctx=ast.Load())
        ),
    )

    # Only operators that are correctly implemented today.
    # Sub and Pow are excluded because of known bugs:
    #   Sub -> calls __subtract__ (doesn't exist on Column)
    #   Pow -> calls __mod__ instead of __pow__
    working_binops = st.sampled_from([ast.Add(), ast.Mult(), ast.Mod()])

    def _extend_expr(base):
        return st.one_of(
            base,
            # BinOp with working operators
            st.tuples(base, working_binops, base).map(
                lambda t: ast.BinOp(left=t[0], op=t[1], right=t[2])
            ),
            # UnaryOp Not
            base.map(lambda e: ast.UnaryOp(op=ast.Not(), operand=e)),
            # Compare: is None / is not None
            st.tuples(
                base, st.sampled_from([ast.Is(), ast.IsNot()])
            ).map(
                lambda t: ast.Compare(
                    left=t[0],
                    ops=[t[1]],
                    comparators=[ast.Constant(value=None)],
                )
            ),
        )

    expr_strategy = st.recursive(leaf_exprs, _extend_expr, max_leaves=8)



# ---------------------------------------------------------------------------
# Tier 1: Structure/rejection tests (needs SparkContext for Column creation)
# ---------------------------------------------------------------------------


@unittest.skipIf(not have_hypothesis, hypothesis_requirement_message)
@unittest.skipUnless(_spark_available, "SparkSession required (lit/col/when need SparkContext)")
class TestConvertChunkUnit(unittest.TestCase):
    """Property-based unit tests for CatalystTranspiler._convert_chunk.

    Requires a SparkSession because _convert_chunk creates Column objects
    via lit()/col()/when() which need an active SparkContext.  No Spark
    *queries* are executed — only the Column expression tree is built.
    """

    @classmethod
    def setUpClass(cls):
        cls.spark = _try_create_spark_session("test_transpile_unit")

    @classmethod
    def tearDownClass(cls):
        cls.spark.stop()

    def setUp(self):
        self.transpiler = CatalystTranspiler()
        self.params = ["x", "y", "z"]

    # -- constants ----------------------------------------------------------

    @given(value=st.one_of(
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False),
        st.none(),
        st.booleans(),
        st.text(max_size=20),
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_constant_roundtrip(self, value):
        """Any Python literal should transpile to a Column via lit()."""
        node = ast.Constant(value=value)
        result = self.transpiler._convert_chunk(self.params, node)
        self.assertIsInstance(result, Column)

    # -- binary operators (working subset) ----------------------------------

    @given(expr=expr_strategy)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_random_expr_returns_column(self, expr):
        """Any randomly generated supported expression should return a Column."""
        body = ast.Return(value=expr)
        result = self.transpiler._convert_chunk(self.params, body)
        self.assertIsInstance(result, Column)

    # -- parameter references -----------------------------------------------

    @given(name=param_name_strategy)
    @settings(max_examples=10)
    def test_param_reference(self, name):
        """Parameter names in scope should produce col('_udf_param_N')."""
        node = ast.Name(id=name, ctx=ast.Load())
        result = self.transpiler._convert_chunk(self.params, node)
        self.assertIsInstance(result, Column)

    def test_unknown_variable_raises(self):
        """Referencing a name not in params should raise."""
        node = ast.Name(id="unknown_var", ctx=ast.Load())
        with self.assertRaises(Exception, msg="Variable referenced not found"):
            self.transpiler._convert_chunk(self.params, node)

    # -- unary not ----------------------------------------------------------

    @given(expr=leaf_exprs)
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_unary_not(self, expr):
        """UnaryOp(Not) wrapping any leaf should produce a Column."""
        node = ast.UnaryOp(op=ast.Not(), operand=expr)
        result = self.transpiler._convert_chunk(self.params, node)
        self.assertIsInstance(result, Column)

    # -- compare: is None / is not None -------------------------------------

    @given(
        expr=leaf_exprs,
        op=st.sampled_from([ast.Is(), ast.IsNot()]),
    )
    @settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
    def test_compare_is_none(self, expr, op):
        """is None / is not None comparisons should produce a Column."""
        node = ast.Compare(
            left=expr, ops=[op], comparators=[ast.Constant(value=None)]
        )
        result = self.transpiler._convert_chunk(self.params, node)
        self.assertIsInstance(result, Column)

    # -- if / else ----------------------------------------------------------

    @given(
        guard_name=param_name_strategy,
        guard_op=st.sampled_from([ast.Is(), ast.IsNot()]),
        then_expr=constant_exprs,
        else_expr=constant_exprs,
    )
    @settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
    def test_if_else_single_branch(self, guard_name, guard_op, then_expr, else_expr):
        """if/else with single-expression branches should transpile."""
        node = ast.If(
            test=ast.Compare(
                left=ast.Name(id=guard_name, ctx=ast.Load()),
                ops=[guard_op],
                comparators=[ast.Constant(value=None)],
            ),
            body=[ast.Return(value=then_expr)],
            orelse=[ast.Return(value=else_expr)],
        )
        result = self.transpiler._convert_chunk(self.params, node)
        self.assertIsInstance(result, Column)

    # -- rejection of unsupported AST nodes ---------------------------------

    def test_rejects_call(self):
        node = ast.Call(
            func=ast.Name(id="len", ctx=ast.Load()), args=[], keywords=[]
        )
        with self.assertRaises(Exception):
            self.transpiler._convert_chunk(self.params, node)

    def test_rejects_listcomp(self):
        node = ast.ListComp(
            elt=ast.Constant(value=1),
            generators=[
                ast.comprehension(
                    target=ast.Name(id="i", ctx=ast.Store()),
                    iter=ast.Name(id="x", ctx=ast.Load()),
                    ifs=[],
                    is_async=0,
                )
            ],
        )
        with self.assertRaises(Exception):
            self.transpiler._convert_chunk(self.params, node)

    def test_rejects_attribute(self):
        node = ast.Attribute(
            value=ast.Name(id="x", ctx=ast.Load()), attr="foo", ctx=ast.Load()
        )
        with self.assertRaises(Exception):
            self.transpiler._convert_chunk(self.params, node)

    def test_rejects_subscript(self):
        node = ast.Subscript(
            value=ast.Name(id="x", ctx=ast.Load()),
            slice=ast.Constant(value=0),
            ctx=ast.Load(),
        )
        with self.assertRaises(Exception):
            self.transpiler._convert_chunk(self.params, node)

    def test_rejects_unsupported_comparison(self):
        """Comparisons other than is/is not should raise."""
        node = ast.Compare(
            left=ast.Name(id="x", ctx=ast.Load()),
            ops=[ast.Eq()],
            comparators=[ast.Constant(value=1)],
        )
        with self.assertRaises(Exception):
            self.transpiler._convert_chunk(self.params, node)

    def test_rejects_multi_comparator(self):
        """Chained comparisons like 1 < x < 10 should raise."""
        node = ast.Compare(
            left=ast.Constant(value=1),
            ops=[ast.Lt(), ast.Lt()],
            comparators=[
                ast.Name(id="x", ctx=ast.Load()),
                ast.Constant(value=10),
            ],
        )
        with self.assertRaises(Exception):
            self.transpiler._convert_chunk(self.params, node)

    # -- multi-statement body rejected by _transpile_from_ast ---------------

    def test_multi_statement_body_rejected(self):
        """Functions with more than one statement should be rejected."""
        src = "def f(x):\n    y = x + 1\n    return y"
        ast_info = ast.parse(src)
        func_ast = _get_function_from_ast(ast_info)
        with self.assertRaises(Exception, msg="single expression"):
            self.transpiler._transpile_from_ast(
                src, ast_info, func_ast, ["x"], "long"
            )

    # -- empty / trivial sources --------------------------------------------

    def test_empty_src_returns_none(self):
        """Empty source string should short-circuit to None."""
        result = self.transpiler._transpile_from_ast(
            "", None, ast.FunctionDef(name="f", args=ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[],
                kw_defaults=[], defaults=[],
            ), body=[]), ["x"], "long"
        )
        self.assertIsNone(result)

    # -- known bugs (documented, expected to fail) --------------------------

    @unittest.expectedFailure
    def test_known_bug_pow_uses_mod(self):
        """BUG: ast.Pow() dispatches to __mod__ instead of __pow__.

        When this test starts passing, the bug has been fixed and
        the @expectedFailure decorator should be removed.
        """
        left = ast.Constant(value=2)
        right = ast.Constant(value=3)
        node = ast.BinOp(left=left, op=ast.Pow(), right=right)
        result = self.transpiler._convert_chunk([], node)
        # If Pow were correct, the Column expression tree would use __pow__.
        # Verify the Column's string representation differs from the Mod
        # version (2**3 != 2%3).
        mod_node = ast.BinOp(left=left, op=ast.Mod(), right=right)
        mod_result = self.transpiler._convert_chunk([], mod_node)
        # These should NOT be equal if Pow is implemented correctly
        self.assertNotEqual(str(result), str(mod_result))

    @unittest.expectedFailure
    def test_known_bug_sub_uses_wrong_method(self):
        """BUG: ast.Sub() calls Column.__subtract__ which doesn't exist.

        Column defines __sub__, not __subtract__. This causes an
        AttributeError at transpile time.

        When this test starts passing, the bug has been fixed and
        the @expectedFailure decorator should be removed.
        """
        left = ast.Constant(value=10)
        right = ast.Constant(value=3)
        node = ast.BinOp(left=left, op=ast.Sub(), right=right)
        # This should succeed if Sub is implemented correctly
        result = self.transpiler._convert_chunk([], node)
        self.assertIsInstance(result, Column)


# ---------------------------------------------------------------------------
# Tier 1: AST extraction unit tests
# ---------------------------------------------------------------------------


@unittest.skipIf(not have_hypothesis, hypothesis_requirement_message)
class TestASTExtractionUnit(unittest.TestCase):
    """Unit tests for _get_function_from_ast and _get_parameter_list."""

    def test_get_function_from_ast_def(self):
        """Standard function def should be extracted."""
        src = "def f(x, y):\n    return x + y"
        tree = ast.parse(src)
        func = _get_function_from_ast(tree)
        self.assertIsNotNone(func)
        self.assertIsInstance(func, ast.FunctionDef)
        self.assertEqual(func.name, "f")

    @unittest.expectedFailure
    def test_get_function_from_ast_lambda_assigned(self):
        """BUG: Lambda assigned to a variable is not extracted.

        _get_function_from_ast unwraps ast.Assign to get the value, but
        then only checks for ast.Expr(ast.Lambda), not a bare ast.Lambda.
        So ``f = lambda x: x + 1`` falls through to the default case.

        When this test starts passing, the bug has been fixed and
        the @expectedFailure decorator should be removed.
        """
        src = "f = lambda x: x + 1"
        tree = ast.parse(src)
        func = _get_function_from_ast(tree)
        self.assertIsNotNone(func)
        self.assertIsInstance(func, ast.FunctionDef)
        self.assertEqual(func.name, "<lambda>")

    def test_get_function_from_ast_bare_lambda(self):
        """Bare lambda expression should be extracted."""
        src = "lambda x: x + 1"
        tree = ast.parse(src)
        func = _get_function_from_ast(tree)
        self.assertIsNotNone(func)
        self.assertIsInstance(func, ast.FunctionDef)

    def test_get_function_from_ast_empty_body(self):
        """Module with empty body should return None."""
        tree = ast.parse("")
        result = _get_function_from_ast(tree)
        self.assertIsNone(result)

    @given(
        names=st.lists(
            st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True),
            min_size=0,
            max_size=5,
            unique=True,
        )
    )
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_get_parameter_list_various_counts(self, names):
        """_get_parameter_list should return param names in order."""
        args = ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=n) for n in names],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        )
        func = ast.FunctionDef(
            name="f", args=args, body=[ast.Return(value=ast.Constant(value=1))]
        )
        result = _get_parameter_list(func)
        self.assertEqual(result, names)

    def test_self_param_included(self):
        """_get_parameter_list does NOT filter 'self' — documents current behavior.

        For callable classes, the __call__ method has 'self' as the first
        parameter.  The transpiler currently includes it, which means the
        param index is off by one when used as a UDF.
        """
        src = "def __call__(self, x, y):\n    return x + y"
        tree = ast.parse(src)
        func = _get_function_from_ast(tree)
        params = _get_parameter_list(func)
        # Current behavior: self IS included
        self.assertIn("self", params)
        self.assertEqual(params, ["self", "x", "y"])


# ---------------------------------------------------------------------------
# Tier 2: Integration tests (with SparkSession) — skipped by default
# ---------------------------------------------------------------------------


@unittest.skipIf(not have_hypothesis, hypothesis_requirement_message)
@unittest.skipUnless(_spark_available, "SparkSession required")
@unittest.skipUnless(
    run_hypothesis_integration,
    "Set SPARK_RUN_HYPOTHESIS_TESTS=1 to run Hypothesis integration tests",
)
class TestTranspileEquivalence(unittest.TestCase):
    """Property-based equivalence tests: Python eval vs transpiled Catalyst.

    These tests create a SparkSession and compare the output of:
      1. Evaluating the Python expression directly
      2. Evaluating the transpiled Catalyst Column on a DataFrame

    Gated behind SPARK_RUN_HYPOTHESIS_TESTS=1 because they are slow.
    """

    @classmethod
    def setUpClass(cls):
        cls.spark = _try_create_spark_session("test_transpile_hypothesis")

    @classmethod
    def tearDownClass(cls):
        cls.spark.stop()

    def setUp(self):
        self.transpiler = CatalystTranspiler()
        self.params = ["x", "y", "z"]

    @staticmethod
    def _eval_python(expr_ast, param_names, input_values):
        """Evaluate an AST expression node with the given input values.

        Returns the Python result, or None if Python raises TypeError or
        ZeroDivisionError (matching Spark's null-propagation semantics).
        """
        ns = {name: val for name, val in zip(param_names, input_values)}
        try:
            code = compile(ast.Expression(body=expr_ast), "<test>", "eval")
            return eval(code, {"__builtins__": {}}, ns)
        except (TypeError, ZeroDivisionError):
            return None

    def _eval_catalyst(self, result_col, param_names, input_values):
        """Evaluate a transpiled Column on a single-row DataFrame."""
        from pyspark.sql import Row

        row_dict = {
            f"_udf_param_{i}": v for i, v in enumerate(input_values)
        }
        df = self.spark.createDataFrame([Row(**row_dict)])
        result_row = df.select(result_col).first()
        return result_row[0] if result_row is not None else None

    @staticmethod
    def _results_match(python_val, spark_val):
        """Compare Python and Spark results with tolerance for floats."""
        if python_val is None and spark_val is None:
            return True
        if python_val is None or spark_val is None:
            return False
        if isinstance(python_val, float) and isinstance(spark_val, float):
            if math.isnan(python_val) and math.isnan(spark_val):
                return True
            return math.isclose(python_val, spark_val, rel_tol=1e-6, abs_tol=1e-9)
        # Spark may return int results as long
        try:
            return python_val == spark_val or float(python_val) == float(spark_val)
        except (TypeError, ValueError):
            return False

    @given(value=st.integers(min_value=-100, max_value=100))
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_constant_equivalence(self, value):
        """Transpiled integer constants should match Python values."""
        node = ast.Constant(value=value)
        result_col = self.transpiler._convert_chunk([], node)
        spark_val = self._eval_catalyst(result_col, [], [])
        self.assertEqual(value, spark_val)

    @given(
        left=st.integers(min_value=-50, max_value=50),
        right=st.integers(min_value=-50, max_value=50),
        op_name=st.sampled_from(["Add", "Mult"]),
    )
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_arithmetic_equivalence(self, left, right, op_name):
        """Add and Mult should produce same results as Python."""
        op_map = {"Add": ast.Add(), "Mult": ast.Mult()}
        node = ast.BinOp(
            left=ast.Constant(value=left),
            op=op_map[op_name],
            right=ast.Constant(value=right),
        )
        py_val = self._eval_python(node, [], [])
        result_col = self.transpiler._convert_chunk([], node)
        spark_val = self._eval_catalyst(result_col, [], [])
        self.assertTrue(
            self._results_match(py_val, spark_val),
            f"{left} {op_name} {right}: Python={py_val}, Spark={spark_val}",
        )

    @given(
        value=st.one_of(st.integers(min_value=-50, max_value=50), st.none()),
        const=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_if_none_guard_equivalence(self, value, const):
        """``if x is not None: return x + c`` should match Python semantics."""
        # Build: if x is not None: return x + const; else: return None
        node = ast.If(
            test=ast.Compare(
                left=ast.Name(id="x", ctx=ast.Load()),
                ops=[ast.IsNot()],
                comparators=[ast.Constant(value=None)],
            ),
            body=[
                ast.Return(
                    value=ast.BinOp(
                        left=ast.Name(id="x", ctx=ast.Load()),
                        op=ast.Add(),
                        right=ast.Constant(value=const),
                    )
                )
            ],
            orelse=[ast.Return(value=ast.Constant(value=None))],
        )
        # Python evaluation
        if value is not None:
            py_val = value + const
        else:
            py_val = None

        result_col = self.transpiler._convert_chunk(["x"], node)
        spark_val = self._eval_catalyst(result_col, ["x"], [value])
        self.assertTrue(
            self._results_match(py_val, spark_val),
            f"value={value}, const={const}: Python={py_val}, Spark={spark_val}",
        )

    @given(value=st.one_of(st.integers(min_value=-50, max_value=50), st.none()))
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_null_propagation(self, value):
        """Arithmetic on a None param should propagate null, not crash."""
        # x + 1 where x may be None
        node = ast.BinOp(
            left=ast.Name(id="x", ctx=ast.Load()),
            op=ast.Add(),
            right=ast.Constant(value=1),
        )
        py_val = self._eval_python(node, ["x"], [value])
        result_col = self.transpiler._convert_chunk(["x"], node)
        spark_val = self._eval_catalyst(result_col, ["x"], [value])
        self.assertTrue(
            self._results_match(py_val, spark_val),
            f"value={value}: Python={py_val}, Spark={spark_val}",
        )


if __name__ == "__main__":
    try:
        import xmlrunner  # noqa: F401

        testRunner = xmlrunner.XMLTestRunner(output="target/test-reports", verbosity=2)
    except ImportError:
        testRunner = None
    unittest.main(testRunner=testRunner, verbosity=2)
