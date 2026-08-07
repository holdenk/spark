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

Transpilation is only attempted when both
``spark.sql.experimental.optimizer.transpilePyUDFs=true`` and
``spark.sql.ansi.enabled=true``. The generated Catalyst expressions
target ANSI-mode SQL semantics (overflow raises, divide-by-zero raises,
etc.); running them under non-ANSI mode would silently diverge from the
Python interpretation in ways we don't currently track. If you flip
transpilation on with ANSI off the UDF will fall back to interpreted
Python execution and a warning is logged at UDF construction time.

Python's ``+`` and ``*`` are overloaded for text (concat / repeat), so an
untyped parameter is transpiled into one option per input-type category
(numeric and string) and the JVM picks the one matching the bound column
types -- falling back to interpreted Python when none fit. Annotating the
UDF's parameters (e.g. ``def f(a: int, b: str)``) pins each category and
keeps the option matrix small; prefer doing so. To bound plan growth,
functions with more than three untyped parameters only emit the
all-numeric and all-string variants.

Free variables, local assignments and captured values
-----------------------------------------------------
A name that is not a parameter (``lambda a: a + b``) is resolved from the
function's scope and baked into the plan as a literal, and a local binding
(``def f(a): b = a + 1; return b * 2``) is inlined at its use sites. Both are
handled by :func:`_normalize_function` before any lowering happens, so the
result reaching the lowering code is indistinguishable from a UDF the user
wrote with the literal spelled out.

Baking a value is only sound when the interpreted path would have seen that
same value, which means matching ``cloudpickle`` exactly: a captured name is
resolved only when ``cloudpickle`` would snapshot it BY VALUE.

:func:`_capture_scope` therefore reads ``cloudpickle``'s own helpers rather than
re-deriving the rule -- private, but vendored, so they move only on a deliberate
upgrade. ``dumps``/``loads`` will not do: ``loads`` returns a by-reference
function unchanged, hiding the divergence that must fall back.

Capture timing: captured values are read when the UDF's ``judf`` is created, the
same moment ``_wrap_function`` cloudpickles it, so the baked literals and the
snapshot cannot disagree even if a captured global is rebound in between.
"""

import ast
import copy
import copyreg
import types
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, TYPE_CHECKING
import inspect
import itertools
import textwrap
import warnings
from pyspark.cloudpickle.cloudpickle import (
    _empty_cell_value,
    _extract_code_globals,
    _get_cell_contents,
    _should_pickle_by_reference,
)
from pyspark.errors import UnsupportedOperationException
from pyspark.sql.column import Column
from pyspark.sql.types import (
    BinaryType,
    BooleanType,
    DataType,
    DecimalType,
    NumericType,
    StringType,
)
from pyspark.sql.functions import (
    abs as _abs,
    coalesce,
    col,
    concat,
    lit,
    pmod,
    raise_error,
    repeat,
    when,
)


if TYPE_CHECKING:
    from pyspark.sql import SparkSession
    from pyspark.sql._typing import DataTypeOrString


class AbstractTranspiler(object):
    """Base class for transpilers. All experimental."""

    varieties: dict[str, type["AbstractTranspiler"]] = {}
    # Specify the "friendly" name a user can add to spark.sql.experimental.optimizer.pyTranspilers
    # to enable this transpiler.
    variety: str = ""

    @classmethod
    def register(cls) -> None:
        AbstractTranspiler.varieties[cls.variety] = cls

    def _transpile_from_ast(
        self,
        src: Optional[str],
        ast_info: ast.AST,
        function_ast: ast.FunctionDef,
        params: List[str],
        returnType: "DataTypeOrString",
        param_categories: Optional[dict] = None,
    ) -> Optional[Column]:
        pass


def _is_definitely_basic_type(node: ast.AST) -> bool:
    """
    Return True when ``node`` is statically guaranteed to produce a Python
    basic/builtin type (int, float, str, bool, None, lists, etc.).
    All ast.Name's are treated as basic types. That is sound because
    ``_normalize_function`` runs first and rewrites every non-parameter name
    into an ``ast.Constant`` (or refuses), so the only ``ast.Name`` nodes the
    lowering code can still see are UDF parameters, which are bound to columns
    of basic types.
    """
    match node:
        case ast.Constant():
            return True
        case ast.BinOp(left=left, right=right):
            return _is_definitely_basic_type(left) and _is_definitely_basic_type(right)
        case ast.UnaryOp(operand=operand):
            return _is_definitely_basic_type(operand)
        case ast.Name():
            return True
        case _:
            return False


def _is_definitely_boolean(node: ast.AST) -> bool:
    """Return True when ``node`` is statically guaranteed to produce a Python
    ``bool`` (or ``None``, which round-trips through ``coalesce``).

    Used to gate ``if``/ternary lowering: we only allow the test expression
    into Catalyst's ``when(coalesce(test, false), ...)`` form when it provably
    produces a boolean. Everything else (bare Name, arithmetic, function calls,
    subscript, ...) must force a fallback to interpreted Python instead of
    silently diverging.
    """
    match node:
        case ast.Constant(value=v):
            return v is None or isinstance(v, bool)
        case ast.Compare(left=left, comparators=comparators):
            # All comparison operators of simple types bool
            return all(_is_definitely_basic_type(v) for v in comparators + [left])
        case ast.BoolOp(values=values):
            return all(_is_definitely_boolean(v) for v in values)
        case ast.UnaryOp(op=ast.Not()):
            # `not x` always produces bool.
            return True
        case ast.IfExp(body=body, orelse=orelse):
            # Ternary is boolean only if both branches are.
            return _is_definitely_boolean(body) and _is_definitely_boolean(orelse)
        case _:
            return False


# Only these can be baked into the plan as literals: they are immutable, they
# map onto a Spark column category, and ``cloudpickle`` snapshots them by value.
# Matched by EXACT type, not ``isinstance``: a subclass may override an operator
# (``int.__radd__``, ``str.__radd__``, ...) that Catalyst would then apply with
# base-type semantics, and ``bool`` is only safe here because it is listed.
_BAKEABLE_TYPES = (int, float, str, bool, bytes)

# A captured int must fit LongType; see the guard in ``_ScopeNormalizer._bake``.
_LONG_MIN = -(2**63)
_LONG_MAX = 2**63 - 1

# Inlining a local binding duplicates its expression at every use site, so a
# chain like ``b = a + a; c = b + b; ...`` doubles in size per link (a depth-10
# chain reaches ~8k nodes). Cap the normalized body and fall back above it.
_MAX_NORMALIZED_NODES = 1000

# Discarded statements (a function with no ``return``) are lowered away, so any
# error their evaluation would have raised in Python disappears with them. Only
# allow node types whose evaluation cannot raise, which keeps the existing
# documented "arithmetic on NULL yields NULL rather than TypeError" divergence
# as the only difference. `%` (ZeroDivisionError) and comparisons (TypeError on
# None) are deliberately absent.
_DISCARDABLE_NODES = (
    ast.Expr,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.BoolOp,
    ast.And,
    ast.Or,
)


def _check_node_budget(node: ast.AST, what: str) -> None:
    """Refuse a tree that has grown past ``_MAX_NORMALIZED_NODES``.

    Applied per local binding as it is inlined, not just to the finished body:
    inlining doubles a binding's size per link in ``z1 = z0 + z0; z2 = z1 + z1;
    ...``, so 20 such assignments reach ~8.4M nodes -- minutes and gigabytes
    spent at UDF construction before falling back anyway.
    """
    size = sum(1 for _ in ast.walk(node))
    if size > _MAX_NORMALIZED_NODES:
        raise UnsupportedOperationException(
            f"inlining local assignments produced {size} expression nodes in "
            f"{what}, over the {_MAX_NORMALIZED_NODES} limit; falling back to "
            "interpreted Python rather than emitting a plan this large"
        )


def _is_docstring(stmt: ast.stmt) -> bool:
    """Whether ``stmt`` is a bare string expression (a docstring in position 0)."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _own_scope_nodes(body: List[ast.stmt]) -> Iterator[ast.AST]:
    """Every node under ``body`` that belongs to the enclosing function itself.

    Nested ``def``/``lambda``/``class`` bodies are skipped, because a ``return``
    or ``yield`` inside one of them belongs to that scope, not to ours:
    ``ast.walk`` would descend and report ``def f(x):\\n def g(): return 1``
    as having a return.
    """
    stack: List[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _returns_only_none_implicitly(body: List[ast.stmt]) -> bool:
    """Whether the function has no ``return`` statement at all, so every* call
    falls off its end and hands back ``None``.

    * Technically could also throw.
    """
    nodes = list(_own_scope_nodes(body))
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in nodes):
        return False
    return not any(isinstance(n, ast.Return) for n in nodes)


def _underlying_function(func: Callable) -> Optional[types.FunctionType]:
    """The plain function whose code object describes ``func``'s body.

    ``func`` may be a function, a bound method, or an instance of a callable
    class; the latter two carry their body on ``__func__`` / the class's
    ``__call__``.
    """
    if isinstance(func, types.FunctionType):
        return func
    if inspect.ismethod(func):
        inner = func.__func__
        return inner if isinstance(inner, types.FunctionType) else None
    call = getattr(type(func), "__call__", None)
    return call if isinstance(call, types.FunctionType) else None


def _pickled_by_value(obj: Any) -> bool:
    """Whether ``cloudpickle`` snapshots ``obj`` rather than re-importing it.

    A by-reference object is re-imported on the executor, so anything reached
    through it is whatever that process holds -- not what the driver saw.
    """
    return not _should_pickle_by_reference(obj)


# Only default pickling ships ``vars(instance)`` verbatim. ``reducer_override``
# declines ordinary instances, so they go through ``__reduce_ex__`` -- and any of
# these hooks can rewrite or drop the dict on the way.
_PICKLE_STATE_HOOKS = ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__")


def _instance_dict_is_shipped(receiver: Any) -> bool:
    """Whether ``vars(receiver)`` is what the executor will actually receive."""
    cls = type(receiver)
    if cls in copyreg.dispatch_table:
        return False
    return all(
        getattr(cls, hook, None) is getattr(object, hook, None) for hook in _PICKLE_STATE_HOOKS
    )


def _is_data_descriptor(value: Any) -> bool:
    """Whether ``value`` outranks an instance / class ``__dict__`` entry."""
    vtype = type(value)
    return hasattr(vtype, "__set__") or hasattr(vtype, "__delete__")


class _CapturedScope:
    """The values a UDF body may safely read from its enclosing scope.

    Resolution is deliberately narrower than Python's own name lookup: a name is
    only resolvable when ``cloudpickle`` would have snapshotted the same value
    for the interpreted path (see this module's docstring).
    """

    def __init__(
        self,
        local_names: Set[str],
        cells: Dict[str, Any],
        global_values: Optional[Dict[str, Any]],
        instance: Optional[Any],
    ) -> None:
        self._local_names = local_names
        self._cells = cells
        # ``None`` == pickled by reference, so no global is readable.
        self._global_values = global_values
        self._instance = instance

    def lookup_name(self, name: str) -> Any:
        if name in self._local_names:
            # The compiler put this name in ``co_varnames``, so it is assigned
            # somewhere in the body and is therefore local for the WHOLE body.
            # Reading it before that assignment raises UnboundLocalError in
            # Python, so resolving it from an enclosing scope here would return
            # a value where the UDF actually fails. This catches only
            # ``co_varnames``: a body-local that a nested scope closes over moves
            # to ``co_cellvars`` instead, and refuses below as unresolvable.
            raise UnsupportedOperationException(
                f"{name!r} is a local variable that is read before it is "
                "assigned; Python raises UnboundLocalError here, so the "
                "transpiler falls back to interpreted Python"
            )
        if name in self._cells:
            value = self._cells[name]
            if value is _empty_cell_value:
                raise UnsupportedOperationException(
                    f"closure cell for {name!r} has not been assigned yet; "
                    "falling back to interpreted Python"
                )
            return value
        if self._global_values is not None and name in self._global_values:
            return self._global_values[name]
        if self._global_values is None:
            raise UnsupportedOperationException(
                f"cannot capture the global {name!r}: this UDF is pickled by "
                "reference, so the executor re-imports its module and reads "
                "whatever value the global holds there. Baking the driver's "
                "value could silently disagree, so the transpiler falls back "
                "to interpreted Python"
            )
        raise UnsupportedOperationException(
            f"name {name!r} is not a parameter and could not be resolved from the UDF's scope"
        )

    def lookup_self_attr(self, attr: str) -> Any:
        """Resolve ``self.<attr>`` on a callable instance / bound method."""
        if self._instance is None:
            raise UnsupportedOperationException(
                "attribute access on `self` is only supported for callable "
                "instances and bound methods"
            )
        receiver = self._instance
        # Reading the dicts below only tracks real attribute access while the
        # default lookup is in force; a custom ``__getattribute__`` can return
        # anything. ``__getattr__`` needs no guard: it only fires when lookup
        # fails, and a lookup that finds nothing here already refuses.
        if type(receiver).__getattribute__ not in (
            object.__getattribute__,
            type.__getattribute__,
        ):
            raise UnsupportedOperationException(
                f"{type(receiver).__name__} overrides `__getattribute__`, so "
                f"`self.{attr}` need not be the value stored on the instance or "
                "class; falling back to interpreted Python"
            )
        if isinstance(receiver, type):
            # A bound classmethod's receiver IS the class, so there is no
            # instance ``__dict__`` to snapshot: every attribute comes from the
            # MRO and has to clear the per-class gate below.
            instance_dict: Dict[str, Any] = {}
            mro = receiver.__mro__
            # ``type.__getattribute__`` consults the METACLASS for a data
            # descriptor before the class's own ``__dict__``, so one found there
            # outranks everything ``mro`` holds.
            metaclass: Any = type(receiver)
            meta = next((m for m in metaclass.__mro__ if attr in m.__dict__), None)
            if meta is not None and _is_data_descriptor(meta.__dict__[attr]):
                raise UnsupportedOperationException(
                    f"`self.{attr}` resolves to a data descriptor on the metaclass "
                    f"{meta.__name__}, which outranks the class attribute and is "
                    "computed per process; falling back to interpreted Python"
                )
        else:
            try:
                instance_dict = vars(receiver)
            except TypeError:
                # ``__slots__`` classes have no instance ``__dict__``.
                instance_dict = {}
            mro = type(receiver).__mro__
        if attr in instance_dict and not _instance_dict_is_shipped(receiver):
            raise UnsupportedOperationException(
                f"`self.{attr}` comes from the instance `__dict__`, but "
                f"{type(receiver).__name__} customizes pickling, so the executor "
                "may reconstruct it with a different value; falling back to "
                "interpreted Python"
            )
        # Resolve against the MRO before the instance ``__dict__``: a data
        # descriptor outranks it in Python's real lookup, so an instance hit
        # cannot be trusted until we know what the MRO holds.
        owner = next((klass for klass in mro if attr in klass.__dict__), None)
        if owner is None:
            # Nothing in the MRO, so the instance dict is the whole story.
            if attr in instance_dict:
                return instance_dict[attr]
            raise UnsupportedOperationException(
                f"`self.{attr}` could not be resolved to a value cloudpickle "
                "would capture by value; falling back to interpreted Python"
            )
        value = owner.__dict__[attr]
        # Read the class ``__dict__`` rather than ``getattr`` so a descriptor
        # (property, method, classmethod) is never invoked: its result is computed
        # per process, not snapshotted. Refused even when the instance ``__dict__``
        # also holds the name -- only a data descriptor would actually win there,
        # and telling the two apart is not worth it for a collision this rare.
        if hasattr(type(value), "__get__"):
            raise UnsupportedOperationException(
                f"`self.{attr}` resolves to a descriptor "
                f"({type(value).__name__}), whose value is not "
                "captured by cloudpickle; falling back to "
                "interpreted Python"
            )
        if attr in instance_dict:
            return instance_dict[attr]
        # Gate the defining class on its OWN mode: cloudpickle ships only a
        # by-value class's own ``__dict__`` and pickles each base separately, so a
        # by-value subclass can inherit from a by-reference base.
        if not _pickled_by_value(owner):
            raise UnsupportedOperationException(
                f"`self.{attr}` is defined on {owner.__name__}, which "
                "cloudpickle pickles by reference, so the executor "
                "re-imports that class and reads whatever the attribute "
                "holds there; falling back to interpreted Python"
            )
        return value


def _capture_scope(func: Callable) -> _CapturedScope:
    """Build the capture table for ``func``, gated on cloudpickle's behaviour."""
    fn = _underlying_function(func)
    if fn is None:
        raise UnsupportedOperationException(
            "could not determine the UDF's code object, so its scope cannot be resolved"
        )
    code = fn.__code__
    # cloudpickle's own sentinel, so an unassigned cell is the object the
    # executor would receive.
    cells = dict(zip(code.co_freevars, map(_get_cell_contents, fn.__closure__ or ())))

    if inspect.ismethod(func):
        instance: Optional[Any] = func.__self__
    elif isinstance(func, types.FunctionType):
        instance = None
    else:
        instance = func

    # Globals follow the function that CARRIES the code: ``_method_reduce``
    # reduces a bound method to its ``__func__``, so gating on the instance's
    # class would bake globals for a method inherited from a by-reference base.
    globals_travel = _pickled_by_value(fn)
    if globals_travel and instance is not None and not inspect.ismethod(func):
        # A callable instance reaches its code through ``type(func).__call__``,
        # which cloudpickle ships only when the class OWNING it is also by value.
        # A by-reference class is re-imported with its original ``__call__``,
        # however the driver's was patched, so the carrying function alone is not
        # enough here.
        owner = next((k for k in type(func).__mro__ if "__call__" in k.__dict__), None)
        globals_travel = owner is not None and _pickled_by_value(owner)
    # Mirror ``_function_getstate``: only the globals the code references ship.
    global_values = (
        {
            name: fn.__globals__[name]
            for name in _extract_code_globals(code)
            if name in fn.__globals__
        }
        if globals_travel
        else None
    )
    return _CapturedScope(
        local_names=set(code.co_varnames),
        cells=cells,
        global_values=global_values,
        instance=instance,
    )


class _ScopeNormalizer(ast.NodeTransformer):
    """Rewrite captured names into constants and inline local bindings.

    After this runs, the only ``ast.Name`` nodes left are UDF parameters, so the
    lowering code in :class:`CatalystTranspiler` needs no knowledge of scopes.
    """

    def __init__(self, params: List[str], scope: _CapturedScope, self_param: Optional[str]) -> None:
        self._params = set(params)
        self._scope = scope
        self._self_param = self_param
        # name -> already-normalized expression it is bound to
        self._env: Dict[str, ast.expr] = {}

    @staticmethod
    def _bake(description: str, value: Any) -> ast.expr:
        if value is None or type(value) in _BAKEABLE_TYPES:
            # Python ints are unbounded, so a capture can exceed what LongType
            # holds. Refuse explicitly rather than letting ``lit`` throw a Java
            # stack trace -- and were ``lit`` ever to accept such a value (a
            # decimal, say), it would still classify as "numeric" and then fail
            # CheckAnalysis against a bigint column, killing the query instead of
            # falling back. ``bool`` is excluded: it subclasses int but is 0/1.
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and not _LONG_MIN <= value <= _LONG_MAX
            ):
                raise UnsupportedOperationException(
                    f"{description} holds {value}, which is outside the range of "
                    "a 64-bit integer; Python integers are unbounded but Spark's "
                    "LongType is not, so the transpiler falls back to interpreted "
                    "Python"
                )
            return ast.Constant(value=value)
        raise UnsupportedOperationException(
            f"{description} holds a {type(value).__name__}, which has no column "
            "equivalent; only None and basic scalars (int, float, str, bool, "
            "bytes) can be transpiled, so this falls back to interpreted Python"
        )

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if not isinstance(node.ctx, ast.Load):
            return node
        # The local binding table is consulted BEFORE the parameter list: a
        # parameter is an ordinary local and may be rebound, in which case every
        # later read must see the new expression. Checking parameters first would
        # turn `def f(a): a = a + 1; return a * 2` into `a * 2`.
        if node.id in self._env:
            # Each use gets its own copy so the tree stays free of shared nodes.
            return copy.deepcopy(self._env[node.id])
        if node.id == self._self_param:
            # A body that references the receiver itself (`return self`) has no
            # column equivalent: it is not bound at the call site. Refused here,
            # where the receiver's name is known, so the lowering only ever sees
            # names the caller actually supplies.
            raise UnsupportedOperationException(
                f"references to the receiver {node.id!r} in a callable's body "
                "are not supported by the transpiler; falling back to "
                "interpreted Python"
            )
        if node.id in self._params:
            return node
        return self._bake(f"captured name {node.id!r}", self._scope.lookup_name(node.id))

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if (
            isinstance(node.ctx, ast.Load)
            and self._self_param is not None
            and isinstance(node.value, ast.Name)
            and node.value.id == self._self_param
            # A rebound receiver is no longer the receiver.
            and node.value.id not in self._env
        ):
            return self._bake(
                f"`{self._self_param}.{node.attr}`",
                self._scope.lookup_self_attr(node.attr),
            )
        return self.generic_visit(node)

    def _desugar_assignment(self, stmt: ast.stmt) -> Tuple[List[str], ast.expr]:
        """Reduce the three assignment forms to (target names, value)."""
        if isinstance(stmt, ast.AugAssign):
            if not isinstance(stmt.target, ast.Name):
                raise UnsupportedOperationException(
                    f"augmented assignment to {type(stmt.target).__name__} is "
                    "not supported by the transpiler"
                )
            # `b += e` is `b = b + e`; the read of `b` resolves against the env
            # as it stands before this statement.
            value: ast.expr = ast.BinOp(
                left=ast.Name(id=stmt.target.id, ctx=ast.Load()),
                op=stmt.op,
                right=stmt.value,
            )
            return [stmt.target.id], value
        if isinstance(stmt, ast.AnnAssign):
            if stmt.value is None:
                raise UnsupportedOperationException(
                    "a bare annotation binds no value and is not supported"
                )
            if not isinstance(stmt.target, ast.Name):
                raise UnsupportedOperationException(
                    f"annotated assignment to {type(stmt.target).__name__} is "
                    "not supported by the transpiler"
                )
            return [stmt.target.id], stmt.value
        assert isinstance(stmt, ast.Assign)
        names: List[str] = []
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                raise UnsupportedOperationException(
                    f"assignment to {type(target).__name__} (tuple unpacking, "
                    "subscript, attribute, ...) is not supported by the "
                    "transpiler"
                )
            names.append(target.id)
        return names, stmt.value

    def normalize_body(self, body: List[ast.stmt]) -> ast.stmt:
        """Collapse ``body`` to one statement."""
        # Python treats a leading string expression as the docstring: it binds
        # nothing and cannot raise, so drop it and normalize what follows. Only
        # the first statement is a docstring, and only when something follows it
        # -- a body that is nothing but a docstring returns None, which the
        # trailing-expression case below already handles.
        if len(body) > 1 and _is_docstring(body[0]):
            body = body[1:]
        for index, stmt in enumerate(body):
            is_last = index == len(body) - 1
            if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                names, value = self._desugar_assignment(stmt)
                # Inline against the env as of this point, then bind. Binding
                # after visiting is what makes `b = b + 1` mean the old `b`.
                inlined = self.visit(ast.fix_missing_locations(value))
                # Bound here rather than only at the end: see _check_node_budget.
                _check_node_budget(inlined, f"the binding of {names[0]!r}")
                for name in names:
                    self._env[name] = inlined
                if is_last:
                    return ast.Return(value=None)
                continue
            if isinstance(stmt, ast.Pass) and is_last:
                # ``pass`` evaluates nothing, so returning NULL is exact.
                return ast.Return(value=None)
            if isinstance(stmt, ast.Expr) and is_last:
                # A trailing bare expression discards its value, so the function
                # returns None. Only lower it away when evaluating it could not
                # have raised: dropping e.g. `x % 0` would turn Python's
                # ZeroDivisionError into a NULL. The caller has already warned
                # that this function returns None either way.
                if not all(isinstance(inner, _DISCARDABLE_NODES) for inner in ast.walk(stmt)):
                    raise UnsupportedOperationException(
                        "this function returns None but its final expression "
                        "could raise when evaluated, so discarding it would "
                        "hide the error; falling back to interpreted Python"
                    )
                return ast.Return(value=None)
            if not is_last:
                raise UnsupportedOperationException(
                    f"{type(stmt).__name__} statements are only supported as the "
                    "function's final statement; assignments may precede it"
                )
            return self.visit(ast.fix_missing_locations(stmt))
        raise UnsupportedOperationException("the function body is empty")


def _normalize_function(
    func: Callable,
    function_ast: ast.FunctionDef,
    params: List[str],
    receiver: Optional[str],
) -> ast.FunctionDef:
    """Resolve scope and inline assignments, yielding a one-statement body.

    ``receiver`` is the name of the implicit first parameter, or ``None`` when the
    call site supplies every parameter. It is determined from the binding by
    :func:`_analyze_func`, not from the name.

    Raises :class:`UnsupportedOperationException` when anything cannot be
    resolved soundly, which the caller turns into a fallback.
    """
    # This walk covers nested scopes, not just the top level: ``_extract_code_globals``
    # recurses into nested code objects, so a nested ``global x`` puts a name that is
    # ALSO a body-local into the global capture table, where it would resolve to the
    # module value for a body that raises UnboundLocalError.
    for node in ast.walk(function_ast):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise UnsupportedOperationException(
                "`global` / `nonlocal` declarations rebind names outside the "
                "function and are not supported by the transpiler"
            )
    scope = _capture_scope(func)
    normalizer = _ScopeNormalizer(params, scope, receiver)
    # ``ast.NodeTransformer`` rewrites in place, and the caller holds onto
    # ``function_ast`` across builds (a UDF is lowered once to validate it at
    # construction and again when its judf is created). Normalizing the original
    # would compound: `a = a + 1; return a * 2` would become `(a + 1) * 2`, then
    # `((a + 1) + 1) * 2` on the next build. Work on a copy.
    source = copy.deepcopy(function_ast)
    statement = normalizer.normalize_body(source.body)
    _check_node_budget(statement, "the function body")
    # Replace the copy's body rather than constructing a fresh ``ast.FunctionDef``.
    # Every other field (notably ``type_params``, which exists only on 3.12+)
    # carries over untouched, so this needs no per-version handling -- and it
    # avoids omitting constructor fields, which 3.13 deprecates and 3.15 rejects.
    source.body = [ast.fix_missing_locations(statement)]
    return source


class CatalystTranspiler(AbstractTranspiler):
    """Transpiler that attempts to convert a Python UDF into native Spark SQL expressions."""

    variety = "catalyst"

    # TODO (SPARK-55218): handle implicit-None return bodies like
    # ``def f(x): x + x`` -- no return statement means return None;
    # we should lower to lit(None) and optionally warn since it's
    # likely a mistake.
    def _convert_branch(self, params: List[str], statements: List[ast.stmt], slot: str) -> Column:
        """Lower a single-statement if-body / if-else block.

        ``slot`` is just used to disambiguate the multi-statement error
        message between the body and the else arm.
        """
        if len(statements) > 1:
            raise UnsupportedOperationException(
                f"if statements with more than one expression in the {slot} "
                "are not currently supported by the transpiler"
            )
        if len(statements) == 0:
            return lit(None)
        return self._convert_chunk(params, statements[0])

    def _safe_category(self, params: List[str], node: Optional[ast.AST]) -> Optional[str]:
        """Best-effort input-type category for an if/else branch, or ``None`` when
        it can't be pinned down statically.

        Used only to compare the two branches of an if/ternary. A ``None`` result
        means "treat as compatible" (don't force a fallback): the node is absent,
        is a bare ``None`` literal (which unifies with any branch type via
        ``coalesce``/``Cast``), or its category can't be determined.
        """
        if node is None:
            return None
        # If-statement branches arrive as ``Return`` statements; classify the
        # returned value, not the statement wrapper (``_is_definitely_boolean``
        # has no ``Return`` case, so without this a boolean-returning branch
        # would fall through to ``_category``'s numeric catch-all).
        if isinstance(node, ast.Return):
            return self._safe_category(params, node.value)
        # An if-statement's category is its branches' common category (the
        # ``_category`` catch-all would mislabel every ``ast.If`` "numeric").
        # Mismatched branches return None ("can't be pinned down"); the
        # branch-compatibility check in ``_convert_if_like`` raises for them.
        if isinstance(node, ast.If):
            body_c = self._safe_category(params, node.body[0]) if node.body else None
            else_c = self._safe_category(params, node.orelse[0]) if node.orelse else None
            if body_c is not None and else_c is not None and body_c != else_c:
                return None
            return body_c if body_c is not None else else_c
        if isinstance(node, ast.Constant) and node.value is None:
            return None
        # Comparisons / ``not`` / boolean ops produce a boolean column; classify
        # them as "bool" (``_category``'s catch-all would mislabel them numeric).
        if _is_definitely_boolean(node):
            return "bool"
        try:
            return self._category(params, node)
        except UnsupportedOperationException:
            return None

    def _convert_if_like(
        self,
        params: List[str],
        test_col: Column,
        body_col: Column,
        else_col: Column,
        test_node: ast.AST,
        body_node: Optional[ast.AST],
        else_node: Optional[ast.AST],
    ) -> Column:
        # We cannot soundly lower a generic Python truthiness test here.
        # Python truthiness depends on the runtime input type and value:
        # for example, 0, 0.0, "", empty collections, and None are all
        # falsy, while most other values are truthy. The transpiler does
        # not have enough input type information at this point to decide
        # whether ``test_col`` is a boolean expression or a bare value
        # whose truthiness would need Python-specific handling. Emitting
        # ``when(coalesce(test_col, false), ...)`` is therefore unsound:
        # it can either fail Spark analysis for non-boolean columns or
        # silently diverge from Python semantics. Fail closed so the UDF
        # falls back to interpreted Python execution instead.
        if not _is_definitely_boolean(test_node):
            raise UnsupportedOperationException(
                f"bare truthiness tests ({ast.dump(test_node)}) in if-expressions are "
                " not currently supported by the transpiler"
            )
        # When the two branches resolve to concrete but different categories
        # (e.g. numeric vs string), the lowered ``when(...).otherwise(...)`` is a
        # CASE WHEN whose branch values share no common type under ANSI. That node
        # is carried as a child of the TranspiledPythonUDF and is type-checked by
        # CheckAnalysis *before* ConvertToCatalyst can drop it, so it would fail
        # the whole query rather than fall back. Refuse here so the UDF runs as
        # interpreted Python instead. Branches whose category we can't pin down
        # (e.g. a bare ``None``) are treated as compatible and don't force this.
        body_cat = self._safe_category(params, body_node)
        else_cat = self._safe_category(params, else_node)
        if body_cat is not None and else_cat is not None and body_cat != else_cat:
            raise UnsupportedOperationException(
                f"if/else branches have incompatible categories ({body_cat} vs "
                f"{else_cat}); the lowered CASE WHEN has no common type under ANSI, "
                "so the transpiler falls back to interpreted Python"
            )
        safe_test = coalesce(test_col, lit(False))
        return when(safe_test, body_col).otherwise(else_col)

    def _lower_eq(
        self,
        params: List[str],
        left_node: ast.AST,
        right_node: ast.AST,
        equal: bool,
    ) -> Column:
        """Lower ``==`` / ``!=`` with Python's None-equality semantics.

        Unlike ordering operators, Python doesn't raise on ``None == x`` /
        ``None != x``: ``None == None`` is True, ``None == 0`` is False,
        and ``!=`` is the negation. Spark's ``==`` returns NULL on NULL
        operands (three-valued logic), which would round-trip through
        the UDF as ``None`` rather than the bool Python would have
        produced. Hand-roll the four cases via ``when`` branches.

        When the two operands resolve to concrete but DIFFERENT categories
        (e.g. ``x == True`` on a numeric column, or ``x == "5"`` under the
        numeric variant), the lowered ``=`` either fails analysis under ANSI
        (bool vs bigint) -- which would break a working UDF since the option
        is type-checked before ConvertToCatalyst can drop it -- or coerces
        where Python's ``==`` is simply False. Refuse those so the UDF falls
        back to interpreted Python. A ``None`` literal operand stays allowed
        (the four-branch NULL handling above reproduces Python exactly).

        One value-level difference remains (needs runtime values, so it is
        documented, not guarded): Spark treats ``NaN = NaN`` as true, while
        Python's ``nan == nan`` is False.
        """
        lc = self._safe_category(params, left_node)
        rc = self._safe_category(params, right_node)
        if lc is not None and rc is not None and lc != rc:
            raise UnsupportedOperationException(
                f"`==`/`!=` operands have incompatible categories ({lc} vs {rc}); "
                "Python compares across types as unequal while Spark would coerce "
                "or fail analysis, so the transpiler falls back to interpreted Python"
            )
        left_col = self._convert_chunk(params, left_node)
        right_col = self._convert_chunk(params, right_node)
        left_null = left_col.isNull()
        right_null = right_col.isNull()
        if equal:
            both_null_val: Column = lit(True)
            one_null_val: Column = lit(False)
            value_cmp = left_col == right_col
        else:
            both_null_val = lit(False)
            one_null_val = lit(True)
            value_cmp = left_col != right_col
        return (
            when(left_null & right_null, both_null_val)
            .when(left_null | right_null, one_null_val)
            .otherwise(value_cmp)
        )

    def _lower_value_compare(
        self,
        params: List[str],
        left_node: ast.AST,
        right_node: ast.AST,
        op: Callable[[Column, Column], Column],
        op_repr: str,
    ) -> Column:
        """Lower a value comparison (``<``, ``<=``, ``>``, ``>=``).

        Python raises ``TypeError`` when an operand of these operators is
        ``None`` (e.g. ``None > 0``), whereas Spark's three-valued logic
        returns ``NULL``. To stay faithful to the source UDF we guard the
        comparison: if either operand is ``NULL`` we raise via
        ``raise_error``, otherwise we evaluate ``left op right`` as usual.
        Callers that have already proven the operand non-null (``if x is
        not None: x > 0``) take the otherwise branch, so they never trip
        the raise.

        Python also forbids ordering across types (``1 < "a"`` -> TypeError),
        whereas Spark would coerce the operands and return a (wrong) boolean.
        We therefore only lower when both operands share a category; a
        mismatch raises so this variant is dropped and the UDF falls back to
        interpreted Python rather than silently diverging.

        One value-level difference from Python remains (it needs runtime
        value info, so it is documented, not guarded): Spark orders ``NaN``
        as greater than every value, whereas Python's ``NaN`` comparisons
        are all ``False``.
        """
        lc = self._category(params, left_node)
        rc = self._category(params, right_node)
        if lc != rc:
            raise UnsupportedOperationException(
                f"`{op_repr}` compares operands of different categories "
                f"({lc} vs {rc}); Python would raise TypeError, so the "
                "transpiler falls back to interpreted Python"
            )
        left_col = self._convert_chunk(params, left_node)
        right_col = self._convert_chunk(params, right_node)
        null_guard = left_col.isNull() | right_col.isNull()
        err = lit(
            "Python UDF transpiler: cannot compare NULL with operator "
            f"`{op_repr}`; Python would raise TypeError here. Add an "
            "`is not None` guard or filter NULLs upstream."
        )
        return when(null_guard, raise_error(err)).otherwise(op(left_col, right_col))

    def _category(self, params: List[str], node: ast.AST) -> str:
        """Infer ``"numeric"`` or ``"string"`` for ``node`` under the current
        ``self._param_categories`` assumption (set per input-type variant).

        Drives operator selection (``+`` -> add vs concat, ``*`` -> multiply vs
        repeat) and raises ``UnsupportedOperationException`` when an operator's
        operands are type-incompatible, so the caller drops that variant and the
        JVM picks another option / falls back to the Python UDF.
        """
        match node:
            case ast.Constant(value=v):
                # bool subclasses int, so classify it first: int/float -> numeric,
                # str -> string, bool -> bool, bytes -> binary. None/complex/
                # Ellipsis have no usable Spark column type, so raise to drop this
                # variant and fall back rather than emit an option that fails
                # CheckAnalysis or silently diverges (e.g. `x + None` -> NULL where
                # Python raises TypeError).
                if isinstance(v, bool):
                    return "bool"
                if isinstance(v, bytes):
                    return "binary"
                if isinstance(v, (int, float)):
                    return "numeric"
                if isinstance(v, str):
                    return "string"
                raise UnsupportedOperationException(
                    f"constant {v!r} ({type(v).__name__}) has no usable column "
                    "category; falling back to interpreted Python"
                )
            case ast.Name(id=name) if name in params:
                # ``params`` is already receiver-free, so its index IS the
                # call-site position the category map is keyed by.
                return self._param_categories.get(params.index(name), "numeric")
            case ast.BinOp(left=left, op=op, right=right):
                lc = self._category(params, left)
                rc = self._category(params, right)
                if isinstance(op, ast.Add) and lc == rc:
                    return lc  # str + str -> str, num + num -> num
                if isinstance(op, ast.Mult):
                    if {lc, rc} == {"numeric", "numeric"}:
                        return "numeric"
                    if {lc, rc} == {"numeric", "string"}:
                        return "string"  # str * int / int * str -> repeat
                if isinstance(op, (ast.Sub, ast.Mod)) and lc == rc == "numeric":
                    return "numeric"
                raise UnsupportedOperationException(
                    f"operands of `{type(op).__name__}` are not type-compatible "
                    "for this input-type variant"
                )
            case ast.Return(value=value) if value is not None:
                return self._category(params, value)
            case ast.IfExp(body=if_body, orelse=if_orelse):
                # A ternary's category is its branches' common category. Without
                # this arm the catch-all labeled every IfExp "numeric", so e.g.
                # `("5" if c else "6") == 5` passed the equality guard as
                # numeric-vs-numeric and Spark's string-number coercion silently
                # diverged from Python's cross-type `==` (always False). A
                # None-literal branch adopts the other branch's category (NULL
                # unifies with any type in the lowered CASE WHEN); mismatched or
                # all-None branches raise so the variant is dropped.
                def branch_category(b: ast.AST) -> Optional[str]:
                    if isinstance(b, ast.Constant) and b.value is None:
                        return None
                    return self._category(params, b)

                body_cat = branch_category(if_body)
                else_cat = branch_category(if_orelse)
                if body_cat is not None and else_cat is not None and body_cat != else_cat:
                    raise UnsupportedOperationException(
                        f"ternary branches have mismatched categories ({body_cat} "
                        f"vs {else_cat}) and cannot drive operator selection"
                    )
                result_cat = body_cat if body_cat is not None else else_cat
                if result_cat is None:
                    raise UnsupportedOperationException(
                        "ternary with all-None branches has no usable column category"
                    )
                return result_cat
            case _ if _is_definitely_boolean(node):
                # Comparisons, `not`, and boolean ops produce a boolean column.
                # Labeling them "numeric" (the old catch-all) let booleans into
                # arithmetic/equality lowerings where ANSI analysis fails (e.g.
                # `(x > 0) + 1`, valid Python) instead of falling back.
                return "bool"
            case _:
                # Remaining nodes (unsupported calls, subscripts, ...) don't
                # drive concat/repeat selection and are rejected later by
                # `_convert_chunk`; treat as numeric for category purposes.
                return "numeric"

    def _convert_chunk(self, params: List[str], body: ast.AST | None) -> Column:
        match body:
            case None:
                # Special case literal None, the implicit return None
                return lit(None)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                # Python's `not None` is `True` (None is falsy), but Spark's
                # `~NULL` is `NULL`. Coalesce against `lit(True)` so a NULL
                # operand mirrors Python's "None is falsy" rule. We only
                # accept operands that are statically known to be boolean;
                # for non-boolean operands (e.g. `not 0`, `not x` where x is
                # a bare parameter name) Spark's `~` is bitwise, not Python
                # truthiness, so we bail and let the caller fall back to
                # interpreted Python rather than silently diverge.
                if not _is_definitely_boolean(operand):
                    raise UnsupportedOperationException(
                        "`not` operand type is not statically known to be "
                        "boolean; Spark's `~` is bitwise, not Python "
                        "truthiness, so the transpiler refuses to lower this "
                        "and the UDF falls back to interpreted Python"
                    )
                return coalesce(self._convert_chunk(params, operand).__invert__(), lit(True))
            case ast.UnaryOp(op=(ast.USub() | ast.UAdd()) as op, operand=operand):
                # `-x` / `+x` -- like the binary arithmetic operators, only
                # lower for numeric operands. Python raises TypeError for
                # unary +/- on strings, but Spark's ANSI string promotion
                # would silently coerce the string to double (`-'5'` ->
                # -5.0), and a boolean operand emits UnaryMinus(bool), which
                # fails CheckAnalysis outright -- breaking the query instead
                # of falling back, since the option is type-checked as a
                # child of TranspiledPythonUDF before ConvertToCatalyst can
                # drop it. Fail closed for every non-numeric category.
                if self._category(params, operand) != "numeric":
                    raise UnsupportedOperationException(
                        "unary `+`/`-` is only supported for numeric operands "
                        "(Python raises TypeError on strings, and Spark would "
                        "coerce or fail analysis); the transpiler falls back "
                        "to interpreted Python"
                    )
                if isinstance(op, ast.USub):
                    # Handles both literal negative ints (USub on a Constant)
                    # and runtime negation of a column.
                    return self._convert_chunk(params, operand).__neg__()
                # `+x` -- identity, kept for symmetry with USub.
                return self._convert_chunk(params, operand)
            case ast.BoolOp(op=op, values=values):
                # Python `and` / `or` short-circuit and return one of the
                # operands rather than a strict boolean. For the booleans
                # produced by Compare / UnaryOp(Not) / nested BoolOps this
                # maps cleanly onto Spark Column `&` / `|`. For
                # non-boolean operands (including bare parameter names whose
                # runtime type is unknown) the right semantics would require
                # Python's truthiness rules (0 / "" / None / [] all
                # falsy), which we can't faithfully reproduce without the
                # input column types -- Spark's `&` / `|` would silently
                # do bitwise instead. Require all operands to be statically
                # known boolean so the caller falls back to interpreted
                # Python rather than producing a plan whose results diverge.
                if not all(_is_definitely_boolean(v) for v in values):
                    raise UnsupportedOperationException(
                        "`and` / `or` operand type is not statically known "
                        "to be boolean; Spark's `&` / `|` are bitwise, not "
                        "Python truthiness, so the transpiler refuses to "
                        "lower this and the UDF falls back to interpreted "
                        "Python"
                    )
                # A literal None operand short-circuits differently: Python's
                # `None and (x > 0)` returns None regardless of x, but Spark's
                # three-valued `null AND false` is false (and `null OR true` is
                # true), so the lowered form diverges. `_is_definitely_boolean`
                # accepts None for `not`/if-test contexts where coalesce handles
                # it; here it must force a fallback instead.
                if any(isinstance(v, ast.Constant) and v.value is None for v in values):
                    raise UnsupportedOperationException(
                        "literal None operand in `and` / `or` cannot be lowered: "
                        "Spark's three-valued logic diverges from Python's "
                        "short-circuit-return-operand semantics, so the UDF "
                        "falls back to interpreted Python"
                    )
                cols = [self._convert_chunk(params, v) for v in values]
                if isinstance(op, ast.And):
                    result = cols[0]
                    for c in cols[1:]:
                        result = result & c
                    return result
                if isinstance(op, ast.Or):
                    result = cols[0]
                    for c in cols[1:]:
                        result = result | c
                    return result
                raise UnsupportedOperationException(f"BoolOp operator {op} is not supported")
            case ast.IfExp(test=test, body=body_expr, orelse=orelse_expr):
                # Ternary `body if test else orelse` -- shares the
                # NULL-as-falsy lowering with the if-statement case.
                return self._convert_if_like(
                    params,
                    self._convert_chunk(params, test),
                    self._convert_chunk(params, body_expr),
                    self._convert_chunk(params, orelse_expr),
                    test,
                    body_expr,
                    orelse_expr,
                )
            case ast.If(test, success, orelse):
                return self._convert_if_like(
                    params,
                    self._convert_chunk(params, test),
                    self._convert_branch(params, success, "body"),
                    self._convert_branch(params, orelse, "else body"),
                    test,
                    success[0] if success else None,
                    orelse[0] if orelse else None,
                )
            case ast.Compare(left, ops, comps):
                if len(ops) != 1 or len(comps) != 1:
                    raise UnsupportedOperationException(
                        "chained comparisons (e.g. `a < b < c`) are not supported by the transpiler"
                    )
                comp = comps[0]
                match ops[0]:
                    case ast.Is() | ast.IsNot():
                        # Only lower `x is None` / `None is x` (and their
                        # `is not` variants) to isNull/isNotNull. For any
                        # other comparator (e.g. `x is 0`, `x is y`) Python
                        # performs an object-identity check that has no SQL
                        # equivalent, so we must fall back to interpreted
                        # Python rather than silently emitting a null check.
                        is_none_left = isinstance(left, ast.Constant) and left.value is None
                        is_none_right = isinstance(comp, ast.Constant) and comp.value is None
                        if not (is_none_left or is_none_right):
                            raise UnsupportedOperationException(
                                "`is`/`is not` is only supported when one "
                                "operand is the literal None; other identity "
                                "checks (e.g. `x is 0`, `x is y`) cannot be "
                                "lowered to SQL and the UDF falls back to "
                                "interpreted Python"
                            )
                        subject_node = comp if is_none_left else left
                        subject_col = self._convert_chunk(params, subject_node)
                        if isinstance(ops[0], ast.Is):
                            return subject_col.isNull()
                        else:
                            return subject_col.isNotNull()
                    case ast.Eq():
                        return self._lower_eq(params, left, comp, equal=True)
                    case ast.NotEq():
                        return self._lower_eq(params, left, comp, equal=False)
                    case ast.Lt():
                        return self._lower_value_compare(
                            params, left, comp, lambda l, r: l < r, "<"
                        )
                    case ast.LtE():
                        return self._lower_value_compare(
                            params, left, comp, lambda l, r: l <= r, "<="
                        )
                    case ast.Gt():
                        return self._lower_value_compare(
                            params, left, comp, lambda l, r: l > r, ">"
                        )
                    case ast.GtE():
                        return self._lower_value_compare(
                            params, left, comp, lambda l, r: l >= r, ">="
                        )
                    case _:
                        raise UnsupportedOperationException(
                            f"comparison operator {type(ops[0]).__name__} "
                            "is not supported by the transpiler"
                        )
            case ast.BinOp(left=left, op=op, right=right):
                # Operator selection is driven by the operand *categories* under
                # the current input-type variant (see ``_category``): Python's
                # `+` / `*` are overloaded for text. `+` -> add (num,num) or
                # concat (str,str); `*` -> multiply (num,num) or repeat (str,int
                # / int,str); `-` / `%` are numeric-only. Combos that don't fit
                # (str+int, str-str, ...) raise so this variant is dropped and
                # the JVM picks another option or falls back to the Python UDF.
                #
                # `**` is intentionally NOT lowered: Spark's `pow` is DOUBLE and
                # loses precision for large integers, so it would silently return
                # wrong results. TODO (SPARK-55210): add an exact integer-power
                # lowering and re-enable it.
                #
                # Value-level divergences remain documented (need runtime value
                # info, not type): overflow raises ARITHMETIC_OVERFLOW under ANSI
                # where Python promotes to a big int; arithmetic is not
                # NULL-guarded (`x + 1` on NULL -> NULL vs Python TypeError).
                # TODO (SPARK-55210): map overflow / divide-by-zero precisely.
                lc = self._category(params, left)
                rc = self._category(params, right)
                left_col = self._convert_chunk(params, left)
                right_col = self._convert_chunk(params, right)
                match op:
                    case ast.Add():
                        if lc == rc == "string":
                            return concat(left_col, right_col)
                        if lc == rc == "numeric":
                            return left_col.__add__(right_col)
                    case ast.Sub():
                        if lc == rc == "numeric":
                            return left_col.__sub__(right_col)
                    case ast.Mult():
                        if lc == "numeric" and rc == "numeric":
                            return left_col.__mul__(right_col)
                        if lc == "string" and rc == "numeric":
                            return repeat(left_col, right_col.cast("int"))
                        if lc == "numeric" and rc == "string":
                            return repeat(right_col, left_col.cast("int"))
                    case ast.Mod():
                        if lc == rc == "numeric":
                            # Python's `%` takes the sign of the divisor; Spark's
                            # takes the dividend's. `sign(b) * pmod(sign(b) * a,
                            # abs(b))` reproduces Python for every non-zero divisor
                            # except at the LongType overflow boundaries -- `a =
                            # Long.MinValue` with `b < 0` (the `sign(b) * a` negate
                            # overflows) and `b = Long.MinValue` (the `abs(b)`
                            # overflows) -- where this raises ARITHMETIC_OVERFLOW
                            # under ANSI while Python returns a value. That matches
                            # the documented overflow caveat for `+`/`-`/`*` above.
                            # Use a CASE-based integer sign rather than sign() to
                            # avoid promoting operands to DoubleType, which loses
                            # precision near LongType boundaries.
                            sb = (
                                when(right_col > 0, lit(1))
                                .when(right_col < 0, lit(-1))
                                .otherwise(lit(0))
                            )
                            return sb * pmod(sb * left_col, _abs(right_col))
                    case _:
                        raise UnsupportedOperationException(
                            f"binary operator {type(op).__name__} is not "
                            "supported by the transpiler"
                        )
                raise UnsupportedOperationException(
                    f"`{type(op).__name__}` operands are not type-compatible for "
                    "this input-type variant"
                )
            case ast.Return(value=value):
                return self._convert_chunk(params, value)
            case ast.Constant(value=value):
                # Avoid circular import issue.
                return lit(value)
            case ast.Name(id=name, ctx=ast.Load()):
                # Insert columns referencing the param indexes for children
                if name in params:
                    # ``params`` is receiver-free, so the index maps straight onto
                    # the positional placeholder the JVM binds. A reference to the
                    # receiver never reaches here -- ``_normalize_function``
                    # refuses it, where the receiver's real name is known.
                    return col(f"_udf_param_{params.index(name)}")
                else:
                    # ``_normalize_function`` rewrites every resolvable
                    # non-parameter name into a constant before lowering runs,
                    # so reaching here means it could not be resolved soundly
                    # (or a transpiler variety skipped normalization).
                    raise UnsupportedOperationException(
                        f"name {name!r} is not in the UDF's parameter list and "
                        "was not resolved from the UDF's scope"
                    )
            case _:
                raise UnsupportedOperationException(
                    f"AST node {type(body).__name__} is not supported by the "
                    f"transpiler ({ast.dump(body)[:120]})"
                )

    def _transpile_from_ast(
        self,
        src: Optional[str],
        ast_info: ast.AST,
        function_ast: ast.FunctionDef,
        params: List[str],
        returnType: "DataTypeOrString",
        param_categories: Optional[dict] = None,
    ) -> Optional[Column]:
        # Short circuit on nothing to transpile.
        if src == "" or ast_info is None:
            return None
        # Per-variant input-type assumption ({public_param_index -> category}),
        # read by ``_category`` to choose str vs numeric operators.
        self._param_categories = param_categories or {}
        function_body = function_ast.body
        if len(function_body) != 1:
            raise UnsupportedOperationException(
                "functions with more than one top-level statement are not "
                "supported by the transpiler"
            )
        # Refuse variants whose body category does not MATCH the declared
        # return type's category. Two distinct failure modes hide here:
        #
        # * A cast that can never resolve (binary -> numeric, bool -> binary):
        #   the options are type-checked by CheckAnalysis as children of
        #   TranspiledPythonUDF before ConvertToCatalyst could drop them, so
        #   the whole query fails instead of falling back.
        # * A cast that IS analysis-valid but that the interpreted
        #   SQL_BATCHED_UDF path never performs: EvaluatePython.makeFromJava
        #   accepts only the expected JVM types for the declared return type
        #   and nulls everything else. E.g. `def f(s: str): return s` declared
        #   LongType() returns NULL interpreted, but a lowered
        #   cast(string as bigint) would return 123 for '123' (or raise
        #   CAST_INVALID_INPUT for 'abc') -- a silent divergence.
        #
        # So require the strict match: numeric -> non-decimal NumericType
        # (DecimalType is excluded like it is for inputs: the interpreted
        # converter accepts only decimal.Decimal results there and nulls the
        # ints/floats these lowerings produce), string -> StringType, bool ->
        # BooleanType, binary -> BinaryType. An unknown category (e.g. a bare
        # None body) lowers to NULL, which every return type accepts as NULL
        # on both paths. Within-numeric conversions (e.g. a bigint body cast
        # to a double return type) are intentionally still allowed and
        # documented as the transpiled-cast behavior pinned by
        # test_udf_transpile_casts_to_return_type.
        if isinstance(returnType, DataType):
            body_cat = self._safe_category(params, function_body[0])
            cast_ok = (
                body_cat is None
                or (
                    body_cat == "numeric"
                    and isinstance(returnType, NumericType)
                    and not isinstance(returnType, DecimalType)
                )
                or (body_cat == "string" and isinstance(returnType, StringType))
                or (body_cat == "bool" and isinstance(returnType, BooleanType))
                or (body_cat == "binary" and isinstance(returnType, BinaryType))
            )
            if not cast_ok:
                raise UnsupportedOperationException(
                    f"a {body_cat}-typed lowering does not match the declared "
                    f"return type {returnType.simpleString()}; the interpreted "
                    "path would return NULL where the lowered cast would "
                    "convert (or fail), so the transpiler falls back to "
                    "interpreted Python"
                )
        converted = self._convert_chunk(params, function_body[0])
        # Cast to the declared return type so the rewritten plan reports a
        # known data type to the optimizer's plan validator (otherwise it
        # sees an UnresolvedFunction tree and reports VOID, which fails
        # the schema-stability check on this rule).
        return converted.cast(returnType)


CatalystTranspiler.register()


def _get_transpilers(session: "SparkSession") -> List[AbstractTranspiler]:
    """Get the transpilers we should try."""
    configured_transpilers = session.conf.get("spark.sql.experimental.optimizer.pyTranspilers")
    if not configured_transpilers:
        return []
    transpiler_names = configured_transpilers.split(",")
    return [
        AbstractTranspiler.varieties[name]()
        for name in transpiler_names
        if name in AbstractTranspiler.varieties
    ]


def _annotation_category(annotation: Optional[ast.AST]) -> Optional[str]:
    """Map a parameter's type annotation to a category
    (``"numeric"``/``"string"``/``"bool"``/``"binary"``), or ``None`` when it's
    absent or unrecognised (the caller then tries both numeric and string)."""
    name: Optional[str] = None
    if isinstance(annotation, ast.Name):
        name = annotation.id
    elif isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        name = annotation.value  # stringized annotation, e.g. def f(a: "int")
    # str -> "string", int/float -> "numeric", bool -> "bool", bytes -> "binary"
    # (matching the constant handling in ``_category``). complex and anything
    # unrecognised return None so the caller tries both numeric and string.
    if name == "str":
        return "string"
    if name in ("int", "float"):
        return "numeric"
    if name == "bool":
        return "bool"
    if name == "bytes":
        return "binary"
    return None


def _param_category_combos(function_ast: ast.FunctionDef, public_params: List[str]) -> List[dict]:
    """Per-variant maps ``{public_param_index -> category}`` where category is
    one of ``"numeric"``/``"string"``/``"bool"``/``"binary"``.

    A typed param (``def f(a: str, b: int)``) is pinned to its category; an
    untyped param is tried as both numeric and string. To cap plan growth, when
    more than three params are untyped we collapse the untyped ones to the
    all-numeric and all-string variants (encourage typing inputs to keep the
    matrix small) while keeping every typed param pinned.
    """
    n = len(public_params)
    public_args = function_ast.args.args[len(function_ast.args.args) - n :]
    candidates: List[List[str]] = []
    untyped = 0
    for arg in public_args:
        cat = _annotation_category(arg.annotation)
        if cat is None:
            candidates.append(["numeric", "string"])
            untyped += 1
        else:
            candidates.append([cat])
    if untyped > 3:
        # Cap the 2**untyped blow-up, but keep each typed param pinned to its
        # category (a single-element ``candidates`` entry); only the untyped
        # params collapse to the all-numeric / all-string pair.
        return [
            {i: c[0] if len(c) == 1 else fill for i, c in enumerate(candidates)}
            for fill in ("numeric", "string")
        ]
    return [{i: choice[i] for i in range(n)} for choice in itertools.product(*candidates)] or [{}]


def _get_src_ast_from_func(func: Callable) -> Tuple[Optional[str], Optional[ast.AST]]:
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
        try:
            # getattr keeps mypy happy: `__call__` on a bare Callable is
            # not attribute-accessible in the type system.
            src = inspect.getsource(getattr(func, "__call__"))
            src = textwrap.dedent(src).strip()
            ast_info = ast.parse(src)
        except Exception:
            # No usable source (REPL/stdin definition, builtin, ...) --
            # return cleanly so the caller reports "cannot transpile"
            # instead of surfacing an UnboundLocalError as the reason.
            return None, None
    return src, ast_info


def _get_parameter_list(node: ast.FunctionDef) -> list[str]:
    """Return the positional argument names in order."""
    return [arg.arg for arg in node.args.args]


def _get_function_from_ast(body: ast.AST) -> ast.FunctionDef | None:
    """
    Extract a :class:`ast.FunctionDef` node from an AST produced by
    ``ast.parse(inspect.getsource(udf_func))``.

    Handles the following source patterns (in order):

    * ``f = lambda x: x + 1`` -- lambda bound directly to a name
    * ``return lambda x: x + 1`` -- lambda returned from a factory function,
      which is what ``inspect.getsource`` yields for ``make_adder(3)``
    * ``lambda x: x + 1`` -- bare expression (getsource on a raw lambda)
    * ``def f(x): ... return x + 1``
    * a class with a ``__call__`` method

    Returns ``None`` when no single unambiguous function can be identified --
    notably, a lambda wrapped in a call such as
    ``f = some_wrapper(lambda x: x + 1)`` parses as ``Assign(value=Call(...))``,
    which is not unwrapped here and so falls back to interpreted Python. Local
    class variables are likewise unsupported.
    """
    if not hasattr(body, "body") or not body.body:
        return None

    stmt = body.body[0]

    # Grab the value side of a top level assign (e.g. x = lambda ...)
    if isinstance(stmt, ast.Assign):
        stmt = stmt.value

    # ``inspect.getsource`` on a lambda built by a factory function returns just
    # the ``return`` line, so unwrap that too -- otherwise the single most
    # natural way to write a closure-capturing UDF cannot be transpiled.
    if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Lambda):
        stmt = stmt.value

    # Bare ``lambda x: ...`` (when ``inspect.getsource`` returns a raw
    # lambda expression at module top level) parses as ``Expr(Lambda)``.
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Lambda):
        stmt = stmt.value

    if isinstance(stmt, ast.Lambda):
        # Synthesize a one-statement FunctionDef wrapping the lambda body so
        # the rest of the transpiler can treat lambdas and ``def`` uniformly.
        # ``ast.FunctionDef``'s overloads in mypy's typeshed require
        # keyword-only ``type_params`` on 3.12+, which doesn't exist at
        # runtime on every Python we support (the field was added in
        # 3.12 -- before that, passing it raises). Drop to ``Any`` so we
        # avoid the overload resolution entirely; constructing the node
        # via keyword args is well-defined at runtime even when the typed
        # overloads disagree.
        fn_ctor: Any = ast.FunctionDef
        return fn_ctor(
            name="<lambda>",
            args=stmt.args,
            body=[ast.Return(value=stmt.body)],
            decorator_list=[],
        )

    if isinstance(stmt, ast.FunctionDef):
        return stmt
    return None


class _TranspileAnalysis:
    """Everything about a UDF the transpiler can determine without reading the
    values it captures from its enclosing scope.

    Split out from the lowering so the captured values can be read later, at
    ``judf`` creation time, together with the ``cloudpickle`` snapshot the
    interpreted path uses -- see this module's docstring on capture timing.
    """

    def __init__(
        self,
        src: Optional[str],
        ast_info: ast.AST,
        function_ast: ast.FunctionDef,
        params: List[str],
        public_params: List[str],
        receiver: Optional[str],
        returnType: "DataTypeOrString",
        combos: List[dict],
    ) -> None:
        self.src = src
        self.ast_info = ast_info
        self.function_ast = function_ast
        self.params = params
        self.public_params = public_params
        # Name of the implicit first parameter (``self`` / ``cls`` / whatever the
        # author called it), or ``None`` when the call site supplies every one.
        self.receiver = receiver
        self.returnType = returnType
        self.combos = combos


def _analyze_func(
    func: Callable[..., Any], returnType: "DataTypeOrString"
) -> Tuple[Optional[_TranspileAnalysis], List[str]]:
    """Decide whether ``func`` is a transpilation candidate at all.

    Performs every check that does not depend on captured values, so an
    unsupported UDF can be reported when it is defined rather than when it is
    first used. Returns ``(None, errors)`` when transpilation cannot proceed.
    """
    # The transpiler lowers to atomic (numeric/string/boolean/binary)
    # expressions and casts the result to the declared return type. For a
    # return type no lowering can even category-match (arrays, maps,
    # structs, datetimes, ...), that Cast either never resolves -- and
    # because the options ride along as children of TranspiledPythonUDF,
    # an unresolvable Cast fails the WHOLE query at CheckAnalysis instead
    # of falling back -- or diverges from the interpreted converter, which
    # nulls type-mismatched results. Restrict transpilation to return
    # types some lowering can match (the strict per-variant body-category
    # check lives in ``_transpile_from_ast``); everything else falls back
    # to interpreted Python.
    if isinstance(returnType, str):
        from pyspark.sql.types import _parse_datatype_string

        returnType = _parse_datatype_string(returnType)
    if not isinstance(returnType, (NumericType, StringType, BooleanType, BinaryType)):
        return None, [
            f"return type {returnType.simpleString()} is not supported by "
            "the transpiler (no lowered expression can be cast to it "
            "under ANSI rules); falling back to interpreted Python"
        ]
    # A functools.wraps-style decorator makes ``inspect.getsource`` return
    # the WRAPPED function's source (getsource follows ``__wrapped__``),
    # while the UDF actually executes the wrapper. Transpiling would
    # silently reproduce the wrong behavior, so refuse and fall back.
    if (
        getattr(func, "__wrapped__", None) is not None
        or getattr(getattr(func, "__call__", None), "__wrapped__", None) is not None
    ):
        return None, [
            "decorated callables (functools.wraps) are not supported: "
            "the visible source is the wrapped function's, not the "
            "wrapper's, so transpilation would change behavior"
        ]
    src, ast_info = _get_src_ast_from_func(func)
    if ast_info is None:
        return None, ["Error getting ast for function, cannot transpile"]
    # Get the lambda body and parameters
    function_ast = _get_function_from_ast(ast_info)
    if function_ast is None:
        return None, ["Error extracting function body from ast, cannot transpile"]
    # Default, variadic (``*args`` / ``**kwargs``), keyword-only, and
    # positional-only parameters can't be represented by the positional
    # ``_udf_param_N`` placeholder scheme: a call site may omit a
    # defaulted argument, leaving the placeholder referencing a position
    # the call never bound, and ``_get_parameter_list`` only reads
    # ``args``. Fall back to interpreted Python rather than emit an
    # invalid plan.
    fn_args = function_ast.args
    if (
        fn_args.defaults
        or any(d is not None for d in fn_args.kw_defaults)
        or fn_args.kwonlyargs
        or fn_args.vararg is not None
        or fn_args.kwarg is not None
        or getattr(fn_args, "posonlyargs", [])
    ):
        return None, [
            "functions with default, variadic, keyword-only, or "
            "positional-only arguments are not supported by the transpiler"
        ]
    params = _get_parameter_list(function_ast)
    # Which leading declared parameters are the implicit receiver? Derive it from
    # the BINDING rather than the parameter's NAME: ``self`` and ``cls`` are only
    # conventions, so a name test both misses ``def __call__(this, x)`` and a
    # ``cls``-named classmethod, and misfires on a plain function that happens to
    # call its first argument ``self``. ``inspect.signature`` already reports the
    # call-site view, so the difference IS the receiver: a bound method, a bound
    # classmethod and a callable instance each hide one declared parameter, while
    # a plain function, a lambda and a ``staticmethod`` hide none.
    try:
        visible = len(inspect.signature(func).parameters)
    except (TypeError, ValueError) as e:
        return None, [
            f"could not determine the UDF's call signature ({e}), so the "
            "transpiler cannot tell which parameters the call site supplies"
        ]
    hidden = len(params) - visible
    if hidden not in (0, 1):
        # A ``__signature__`` override or an unwrappable callable: refuse rather
        # than guess how declared parameters line up with bound columns.
        return None, [
            f"the UDF declares {len(params)} parameter(s) but accepts {visible} "
            "at the call site, which the transpiler cannot map onto columns; "
            "falling back to interpreted Python"
        ]
    # The caller matches user-supplied kwargs against these, and the user does
    # not name the receiver at the call site.
    public_params = params[hidden:]
    receiver = params[0] if hidden else None
    return (
        _TranspileAnalysis(
            src=src,
            ast_info=ast_info,
            function_ast=function_ast,
            params=params,
            public_params=public_params,
            receiver=receiver,
            returnType=returnType,
            combos=_param_category_combos(function_ast, public_params),
        ),
        [],
    )


def _build_transpiled(
    session: "SparkSession", func: Callable[..., Any], analysis: _TranspileAnalysis
) -> Tuple[List[Column], List[str], List[List[str]]]:
    """Resolve captured values and lower ``func`` to Catalyst expressions.

    Call this at ``judf`` creation time: the captured values it reads are baked
    into the returned expressions, and they must be the same values
    ``_wrap_function``'s ``cloudpickle`` snapshot sees.
    """
    errors: List[str] = []
    # Warn before normalizing, so a function that always returns None is
    # reported whether or not it turns out to be transpilable. A trailing
    # expression that could raise (`def f(x): x % 0`) falls back below, but the
    # user still wants to know the function never returns a value.
    if _returns_only_none_implicitly(analysis.function_ast.body):
        warnings.warn(
            f"UDF {func} has no return statement, so it always returns None "
            "(NULL) or raises; add a `return` if that was not intended.",
            RuntimeWarning,
        )
    try:
        # Resolve free variables and inline local assignments once, up front:
        # the result is value-dependent but category-independent, so it is
        # shared by every input-type variant below.
        normalized = _normalize_function(
            func, analysis.function_ast, analysis.params, analysis.receiver
        )
    except Exception as e:
        return [], [str(e)], []
    transpiled: List[Column] = []
    input_categories: List[List[str]] = []
    # One transpiled option per (backend x input-type variant). Untyped
    # params are tried as both numeric and string so the JVM can pick the
    # option matching the actual column types (or fall back if none match).
    # Maybe multiple transpilers (think CUDA, etc.).
    for transpiler in _get_transpilers(session):
        for combo in analysis.combos:
            try:
                transpiled_column = transpiler._transpile_from_ast(
                    analysis.src,
                    analysis.ast_info,
                    normalized,
                    # The receiver is already stripped: ``_normalize_function``
                    # refuses any reference to it, so every name the lowering can
                    # still see is a column the call site binds.
                    analysis.public_params,
                    analysis.returnType,
                    combo,
                )
                if transpiled_column is not None:
                    transpiled.append(transpiled_column)
                    input_categories.append(
                        [combo.get(i, "numeric") for i in range(len(analysis.public_params))]
                    )
            except Exception as e:
                errors.append(str(e))
    return transpiled, errors, input_categories
