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
from typing import Optional, List, Any, Tuple
import inspect
from pyspark.sql import SparkSession
from pyspark.sql.functions import lit
from pyspark.sql.functions.builtin import _invoke_function

def _get_transpilers(session: SparkSession) -> List[AbstractTranspiler]:
    """Get the transpilers we should try."""
    transpiler_names = session.conf.get("spark.sql.experimental.optimizer.pyTranspilers").split(",")
    return [AbstractTranspiler.varieties[name]() for name in transpiler_names if name in AbstractTranspiler.varieties]

def _transpile_func(
        session: SparkSession,
        func: Callable[..., Any],
        
        returnType: "DataTypeOrString" = StringType()
    ) -> Tuple[List[Column], List[str]]:
    """
    An experimental internal function that attempts to transpile a callable function.

    Returns
    -------
    list of transpiled functions
    list fo errors as strings
    """
    try:
        ast = _get_ast_from_func(func)
        if ast is None:
            return ([], ["Error getting ast for function, can not transpile"])
        transpiled = []
        errors = []
        # Maybe multiple transpilers (think CUDA, etc.).
        transpilers = _get_transpilers()
        for transpiler in transpilers:
            try:
                transpiled_result = transpiler._transpile_from_ast(src, ast_info)
                transpiled.append(transpiled_result)
            except Exception as e:
                errors.append(str(e))
        return (transpiled, errors)
    except Exception as e:
        return ([], [str(e)])


class AbstractTranspiler(object):
    """Base class for transpilers. All experimental."""
    varieties = {}
    # Specify the "friendly" name a user can add to spark.sql.experimental.optimizer.transpilers
    # to enable this transpiler.
    variety = None

    @classmethod
    def register(cls):
        AbstractTranspiler.varieties[cls.variety] = cls

    @staticmethod
    def _transpile_from_ast(src: str, ast_info: ast.AST) -> Optional[Column]:
        pass

class CatalystTranspiler(AbstractTranspiler):
    """Transpiler that attempts to convert a Python UDF into native Spark SQL expressions."""
    variety = "catalyst"

    @staticmethod
    def _transpile_from_ast(src: str, ast_info: ast.AST) -> Optional[Column]:
        # Short circuit on nothing to transpile.
        if src == "" or ast_info is None:
            return None
        lambda_ast = _get_lambda_from_ast(ast_info)
        if lambda_ast is None:
            return None
        lambda_body = lambda_ast.body
        params = _get_parameter_list(lambda_ast)
        return [_convert_function(params, lambda_body)]

    @staticmethod
    def _convert_function(params: List[str], body: ast.AST) -> Optional[Column]:
        match body:
            case ast.BinOp(left=left, op=op, right=right):
                left_col = _convert_function(params, left)
                if left_col is None:
                    raise Exception("Could not find left param for addition")
                right_col = _convert_function(params, right)
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
                return lit(value)
            case ast.Name(id=name, ctx=ast.Load()):
                # Note: the Python UDF parameter name might not match the column
                # And at this point we don't know who are children are going to be.
                if name in params:
                    param_index = params.index(name)
                    # TODO: Add a special node here that indicates we want child number param_index
                    return _invoke_function("childPlaceholder", param_index)
            case _:
                raise Exception(f"Found unhandled Python component {body}")

    @staticmethod
    def _get_ast_from_func(func) -> Option[ast.AST]:
        """Try and get the AST from a given callable"""
        # Note: consider maybe dill? (see the JYTHON PR)
        # inspect getsource does not work for functions defined in vanilla
        # repl, but does for those in files or in ipython.
        # It also fails when we give it an instance of a callable class.
        try:
            src = inspect.getsource(func)
        except Exception:
            # Open q: local var in class?
            src = inspect.getsource(func.__call__)
        ast_info = ast.parse(src)
        return ast_info

    @staticmethod
    def _transpile_ast(ast: ast.AST):
        lambda_ast = _get_lambda_from_ast(ast_info)
        if lambda_ast is None:
            return None

CatalystTranspiler.register()
