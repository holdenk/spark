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
Experimental tools for transpiling UDFS.
"""
import ast
from typing import Any, Callable, List, Optional, Tuple
import inspect
import textwrap


class AbstractTranspiler(object):
    """Base class for transpilers. All experimental."""
    varieties = {}
    # Specify the "friendly" name a user can add to spark.sql.experimental.optimizer.transpilers
    # to enable this transpiler.
    variety = None

    @classmethod
    def register(cls):
        AbstractTranspiler.varieties[cls.variety] = cls

    def _transpile_from_ast(
            self,
            src: str,
            ast_info: ast.AST,
            function_ast: ast.FunctionDef,
            params: List[str],
            returnType: "DataTypeOrString"
    ) -> Optional["Column"]:
        pass

class CatalystTranspiler(AbstractTranspiler):
    """Transpiler that attempts to convert a Python UDF into native Spark SQL expressions."""
    variety = "catalyst"


    def _convert_chunk(self, params: List[str], body: ast.AST) -> Optional["Column"]:
        from pyspark.sql.functions import lit, when
        match body:
            case None:
                # Special case literal None
                return lit(None)
            case ast.If(test, success, orelse):
                test_col = self._convert_chunk(params, test)
                inverse_test_col = _convert_chunk(
                    params,
                    ast.UnaryOp(op=ast.Not(), operand=test))
                if len(success) != 1:
                    raise Exception("Currently only support if statements with a single expression in the body")
                body_col = self._convert_chunk(params, success[0])
                if len(orelse) != 1:
                    raise Exception("Currently only support if statements with a single expression in the else body")
                else_col = self._convert_chunk(params, orelse)
                # Since otherwise acts on all nulls we double check.
                return when(test_condition, body_col).otherwise(
                    when(inverse_test_col, else_col))
            case ast.Compare(left, ops, comps):
                if len(ops) != 1 or len(comps) != 1:
                    raise Exception("Currently only support simple comparisons with a single comparator")
                left_col = self._convert_chunk(params, left)
                match ops[0]:
                    case ast.IsNot():
                        return left_col.isNotNull()
                    case ast.Is():
                        return left_col.isNull()
                    case _:
                        raise Exception(f"Found unhandled comparison operator {ops[0]}")
            case ast.BinOp(left=left, op=op, right=right):
                left_col = _convert_chunk(params, left)
                if left_col is None:
                    raise Exception("Could not find left param for addition")
                right_col = _convert_chunk(params, right)
                if right_col is None:
                    raise Exception("Could not find right column for addition")
                match op:
                    # TODO: Maybe use one of the try functions so we can control errors and map topython exceptional cases better.
                    case ast.Add():
                        return left_col.__add__(right_col)
                    case ast.Sub():
                        return left_col.__subtract__(right_col)
                    case ast.Mult():
                        return left_col.__mul__(right_col)
                    case ast.Mod():
                        return left_col.__mod__(right_col)
                    case ast.Pow():
                        return left_col.__mod__(right_col)
                    case _:
                        raise Exception(f"Found unhandled binary operator {op}")
            case ast.Constant(value=value):
                # Avoid circular import issue.
                return lit(value)
            case ast.Name(id=name, ctx=ast.Load()):
                # Insert columns referencing the param indexes for children
                if name in params:
                    param_index = params.index(name)
                    return Column(f"_udf_param_{param_index}")
                else:
                    # TODO: Handle assignments, class vars, etc.
                    raise Exception("Variable referenced not found in inputs")
            case _:
                raise Exception(f"Found unhandled Python component {body}")

    def _transpile_from_ast(
            self,
            src: str,
            ast_info: ast.AST,
            function_ast: ast.FunctionDef,
            params: List[str],
            returnType: "DataTypeOrString"
    ) -> Optional["Column"]:
        # Short circuit on nothing to transpile.
        if src == "" or ast_info is None:
            return None
        function_body = function_ast.body
        if len(function_body) != 1:
            raise Exception("Currently only support functions with a single expression in the body")
        return [self._convert_chunk(params, function_body[0])]

CatalystTranspiler.register()

def _get_transpilers(session: "SparkSession") -> List[AbstractTranspiler]:
    """Get the transpilers we should try."""
    transpiler_names = session.conf.get("spark.sql.experimental.optimizer.pyTranspilers").split(",")
    return [AbstractTranspiler.varieties[name]() for name in transpiler_names if name in AbstractTranspiler.varieties]


def _get_src_ast_from_func(func) -> Tuple[Optional[str], Optional[ast.AST]]:
    """Try and get the AST from a given callable"""
    # Note: consider maybe dill? (see the JYTHON PR)
    # inspect getsource does not work for functions defined in vanilla
    # repl, but does for those in files or in ipython.
    # It also fails when we give it an instance of a callable class.
    try:
        src = inspect.getsource(func)
        src = textwrap.dedent(src).strip()
        ast_info = ast.parse(src)
    except Exception:
        src = inspect.getsource(func.__call__)
        src = textwrap.dedent(src).strip()
        ast_info = ast.parse(src)
    return src, ast_info

def _get_parameter_list(node: ast.FunctionDef) -> list[str]:
    """Return the positional argument names of in order."""
    return [arg.arg for arg in node.args.args]
 
 
def _get_function_from_ast(body: ast.AST) -> ast.FunctionDef | None:
    """
    Extract a :class:`ast.FunctionDef` node from an AST produced by
    ``ast.parse(inspect.getsource(udf_func))``.
 
    Handles the following source patterns (in order):
 
    * ``f = lambda x: x + 1``  — direct assignment
    * ``f = some_wrapper(lambda x: x + 1, ...)``  — lambda as first positional
      arg of a call (e.g. ``functools.partial``)
    * ``lambda x: x + 1``  — bare expression (getsource on a raw lambda)
    * ``def f(x): ... return x + 1``
    * class with callable
 
    Returns ``None`` when no single unambiguous function can be identified.
    Note yet handled: local class variables.
    """
    print("Extracting lambda from AST:")
    print(ast.dump(body))
    if not body.body:
        return None
 
    stmt = body.body[0]
 
    # Grab the value side of a top level assign (e.g. x = lambda ...)
    if isinstance(stmt, ast.Assign):
        stmt = stmt.value

    # --- bare lambda expression ---
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Lambda):
        return ast.FunctionDef(
            name="<lambda>",
            args=stmt.value.args,
            body=[ast.Return(value=stmt.value.body)]
        )
                               
 
    if isinstance(stmt, ast.FunctionDef):
        return stmt
    print(f"Could not match on AST node type {type(stmt)}?")
    return None

def _transpile_func(
        session: "SparkSession",
        func: Callable[..., Any],        
        returnType: "DataTypeOrString",
    ) -> Tuple[List["Column"], List[str]]:
    """
    An experimental internal function that attempts to transpile a callable function.

    Returns
    -------
    list of transpiled functions
    list fo errors as strings
    """
    try:
        src, ast = _get_src_ast_from_func(func)
        if ast is None:
            return ([], ["Error getting ast for function, can not transpile"])
        # Get the lambda body and parameters
        function_ast = _get_function_from_ast(ast)
        params = _get_parameter_list(function_ast)
        transpiled = []
        errors = []
        # Maybe multiple transpilers (think CUDA, etc.).
        transpilers = _get_transpilers(session)
        for transpiler in transpilers:
            try:
                transpiled_result = transpiler._transpile_from_ast(
                    src, ast, function_ast, params, returnType)
                transpiled.append(transpiled_result)
            except Exception as e:
                errors.append(str(e))
                # temporarily raise
                raise
        return (transpiled, errors)
    except Exception as e:
        # temporarily raise
        raise
        return ([], [str(e)])
