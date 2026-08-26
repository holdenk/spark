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

Text repeat carries a known divergence. Python's ``s * n`` needs a whole ``n``
(``"ab" * 2.5`` and ``"ab" * 2.0`` are both a TypeError) while Spark's ``repeat``
truncates. The rule: a count is refused when a non-integral number is VISIBLE in
it -- a literal, or a captured value -- and lowered otherwise, so any count whose
fractional part only arrives at runtime, in a column, still diverges. On a
``double`` column holding 2.5, ``s * n`` gives ``'abab'``, ``s * (n + 1)``
``'ababab'`` and ``s * -n`` ``''``, where Python raises every time. Casting the
count to an integral type makes the two paths agree -- on ``'abab'``, not on the
TypeError; the only way to get Python's error is to leave the UDF interpreted.

A lambda is lowered only when its source names it directly and alone: bind it to
a name (``f = lambda x: x + 1``, annotated if you like) or return it from a
``def`` (``return lambda x: x + n``), on a line of its own. Passed straight to
``udf(...)``, wrapped in another call, returned by another lambda, or sharing a
line with a second lambda, nothing in the source read back says which lambda is
the UDF, so it falls back to interpreted Python rather than risk the wrong body.

Free variables and literal assignments
--------------------------------------
A name that is not a parameter (``lambda a: a + b``) is resolved from the
function's scope and baked into the plan as a literal. A local assignment is
supported only when it **binds a literal** -- one written in the body
(``def f(a): b = 5; return a + b``) or captured from the enclosing scope
(``b = k``). Anything the assignment would have to compute (``b = a + 1``,
``b += 1``, or even aliasing a column with ``b = a``) falls back to interpreted
Python.

That restriction is what keeps this simple. Substituting a literal at a name's
read sites cannot duplicate work, cannot grow the tree, and cannot move an error:
a literal has no evaluation to move. Were an arbitrary expression allowed, an
assignment read only inside an ``if`` branch -- or never read at all -- would
discard or defer an error Python raises eagerly, and inlining a chain like
``b = a + a; c = b + b`` would double the plan per link.

Both kinds of rewrite are done by :func:`_normalize_function` before any lowering
happens, so what reaches the lowering code is indistinguishable from a UDF the
user wrote with the literal spelled out.

Baking a value is only sound when the interpreted path would have seen that
same value, which means matching ``cloudpickle`` exactly: a captured name is
resolved only when ``cloudpickle`` would snapshot it BY VALUE.
:func:`_capture_scope` therefore reads ``cloudpickle``'s own helpers rather than
re-deriving the rule -- private, but vendored, so they move only on a deliberate
upgrade. ``dumps``/``loads`` will not do: ``loads`` returns a by-reference
function unchanged, hiding the divergence that must fall back.

Consequences worth knowing as a user:

* A UDF written as a top-level ``def`` in an importable module is pickled by
  reference, so the executor re-imports the module and re-reads its globals;
  such a UDF falls back rather than baking the driver's values. Writing it as
  a lambda, or registering the module with
  ``cloudpickle.register_pickle_by_value``, makes it eligible.
* Only ``None`` and the basic scalars (``int``, ``float``, ``str``, ``bool``,
  ``bytes``) are bakeable, and an ``int`` has to fit a 64-bit integer since
  Python's are unbounded. Anything else falls back. Some values of a bakeable
  type are refused as well: NaN, whose ordering and equality differ from
  Python's; the infinities, which no integral cast accepts; and a ``str`` that is
  not UTF-8 encodable (a lone surrogate), which py4j cannot carry.
* Only closure cells and module globals are read. ``self.<attr>`` on a callable
  instance is not captured -- resolving it faithfully means reproducing Python's
  descriptor and MRO lookup and cloudpickle's instance-state rules, which is left
  to a follow-up.

Capture timing: captured values are read when the UDF's ``judf`` is created, the
same moment ``_wrap_function`` cloudpickles it, so the baked literals and the
snapshot agree even if a captured global is rebound in between. The two reads are
adjacent rather than atomic; see ``_create_judf`` in ``udf.py``.
"""

import ast
import contextlib
import copy
import inspect
import itertools
import math
import sys
import textwrap
import threading
import types
import warnings
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

from pyspark.cloudpickle.cloudpickle import (
    _empty_cell_value,
    _extract_code_globals,
    _get_cell_contents,
    _should_pickle_by_reference,
)
from pyspark.errors import UnsupportedOperationException
from pyspark.sql.column import Column
from pyspark.sql.functions import (
    abs as _abs,
)
from pyspark.sql.functions import (
    coalesce,
    col,
    concat,
    lit,
    pmod,
    raise_error,
    repeat,
    when,
)
from pyspark.sql.types import (
    BinaryType,
    BooleanType,
    DataType,
    DecimalType,
    NumericType,
    StringType,
)
from pyspark.util import JVM_LONG_MAX, JVM_LONG_MIN

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
        function_ast: ast.FunctionDef,
        params: List[str],
        returnType: "DataTypeOrString",
        param_categories: Optional[dict] = None,
    ) -> Optional[Column]:
        """Lower ``function_ast`` to a :class:`Column`, or return ``None`` to decline.

        The override point for ``spark.sql.experimental.optimizer.pyTranspilers``.

        ``params`` is the CALLER-FACING parameter list: a receiver already bound, as
        on a method or callable instance, has been removed, so ``params[i]`` is the
        name bound to placeholder ``_udf_param_i`` with no offsetting needed. It is
        also the list ``param_categories`` is keyed by.
        """
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


def _refuse_fractional_repeat_count(node: ast.AST) -> None:
    """Refuse a string repeat (``s * n``) whose count is not a whole number.

    Python's ``str * n`` needs a whole ``n`` -- both ``"ab" * 2.5`` and ``"ab" *
    2.0`` are a TypeError -- while Spark's ``repeat`` takes any numeric and would
    truncate, returning a value where Python raises.

    Refuses when a non-integral number appears ANYWHERE in the count expression,
    not just when the count is itself a fractional constant. Matching one node
    shape is the wrong altitude here: ``s * -2.5`` is ``UnaryOp`` and
    ``s * (2.5 + 0)`` is ``BinOp``, and both lowered to
    ``repeat(s, cast(... as int))`` -- ``''`` and ``'abab'`` where Python raises
    TypeError. Since this transpiler deliberately does not evaluate expressions, it
    cannot know what a computed count comes to, so it fails closed on the presence
    of the value that could only truncate.

    Two consequences, both deliberate:

    * A count with no non-integral number VISIBLE in it still lowers, and that is
      wider than a bare ``s * n``: ``Add``/``Sub``/``Mult``/``Mod`` are all lowered,
      so ``s * (n + 1)`` and ``s * -n`` pass too and diverge on a fractional column
      (``'ababab'`` and ``''`` for ``('ab', 2.5)``). Pre-existing behavior;
      constraining it needs a narrower input category than "numeric" on the JVM
      side and is left to a follow-up.
    * A fractional literal somewhere in the expression that cannot reach the count
      (``s * (a if a > 2.5 else 3)``) is refused too. Over-refusing costs a
      lowering; under-refusing returns a wrong answer.
    """
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Constant):
            continue
        value = inner.value
        # ``bool`` subclasses ``int`` and `"ab" * True` is legal Python, but a bool
        # is categorised "bool" and never reaches the repeat arm, so refuse it here
        # rather than imply support the lowering does not have.
        if isinstance(value, int) and not isinstance(value, bool):
            continue
        if not isinstance(value, (int, float, complex)):
            # A non-number cannot be a count; whatever it is doing in this
            # expression, the lowering refuses it on its own terms.
            continue
        raise UnsupportedOperationException(
            f"the count in a string repeat (`s * n`) involves {value!r}, which is "
            "not a whole number; Python raises TypeError for a fractional count "
            "while Spark's `repeat` would truncate it, so this falls back to "
            "interpreted Python"
        )


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


def _refuse_unrepresentable_value(description: str, value: Any) -> None:
    """Refuse a bakeable-TYPE value whose VALUE has no faithful column equivalent.

    Called wherever a Python value becomes a ``lit()`` -- both for a captured name
    (:meth:`_LiteralNormalizer._bake`) and for a literal written in the body
    (``_convert_chunk``'s ``ast.Constant`` arm). Keeping one copy matters because
    the two paths converge: a literal assignment (``b = 1e400``) is substituted by
    the normalizer and then lowered as a body constant, so a check on only one side
    is a check that can be walked around.
    """
    # Python ints are unbounded, so a value can exceed what LongType holds. Refuse
    # explicitly rather than letting ``lit`` throw a Java stack trace -- and were
    # ``lit`` ever to accept such a value (a decimal, say), it would still classify
    # as "numeric" and then fail CheckAnalysis against a bigint column, killing the
    # query instead of falling back. ``bool`` is excluded: it subclasses int but is
    # 0/1.
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and not JVM_LONG_MIN <= value <= JVM_LONG_MAX
    ):
        raise UnsupportedOperationException(
            f"{description} is {value}, which is outside the range of a 64-bit "
            "integer; Python integers are unbounded but Spark's LongType is not, "
            "so the transpiler falls back to interpreted Python"
        )
    # NaN compares false against everything in Python, but Spark orders it above
    # every other double and treats it as equal to itself, so a baked NaN silently
    # flips `a < nan` and `a == nan`. A NaN COLUMN value can only be documented
    # (the type says nothing about it), but one that is known here can be refused.
    #
    # The infinities go with it: the trailing cast to an integral return type
    # raises CAST_OVERFLOW where the interpreted path returns NULL -- breaking a
    # query rather than falling back, which is what every guard here exists to
    # avoid. ``1e400`` is an ordinary-looking literal that evaluates to one.
    if isinstance(value, float) and not math.isfinite(value):
        raise UnsupportedOperationException(
            f"{description} is the non-finite float {value}, which has no faithful "
            "column equivalent: Python compares NaN as false against everything "
            "while Spark orders it above all other doubles, and an infinity cannot "
            "be cast to an integral type. The transpiler falls back to interpreted "
            "Python"
        )
    # A lone surrogate is a legal Python str but not encodable, and py4j encodes
    # every command as UTF-8. This one must be refused BEFORE reaching ``lit``:
    # the UnicodeEncodeError happens inside the socket write, which drops the
    # gateway connection rather than falling back, damaging the whole session.
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise UnsupportedOperationException(
                f"{description} is a string that is not UTF-8 encodable, which "
                "cannot be sent to the JVM, so the transpiler falls back to "
                "interpreted Python"
            )


def _as_literal(node: ast.AST) -> Optional[ast.Constant]:
    """The constant ``node`` already is, or ``None`` when it is not one.

    This is the single place the "assignments bind literals" rule is enforced. It
    runs on an ALREADY-VISITED expression, so a captured name has become an
    ``ast.Constant`` by the time it arrives; what reaches here as something else is
    genuinely computed (``a + 1``, a call, a comparison) or is a column reference,
    and either way this transpiler will not evaluate it.

    The one concession is a unary ``-``/``+`` on a numeric constant: Python parses
    ``-5`` as ``UnaryOp(USub, Constant(5))``, never ``Constant(-5)``, so without
    folding it a negative literal would be refused as "computed" -- a confusing
    answer for something spelled exactly like a literal. Folding is exact for
    ``int`` and ``float`` and is not applied to anything else (``-"ab"`` is a
    Python TypeError, and ``-True`` would silently become ``-1``).
    """
    if isinstance(node, ast.Constant):
        return node
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = node.operand
        if (
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, (int, float))
            and not isinstance(operand.value, bool)
        ):
            value = -operand.value if isinstance(node.op, ast.USub) else +operand.value
            return ast.Constant(value=value)
    return None


def _is_docstring(stmt: ast.stmt) -> bool:
    """Whether ``stmt`` is a bare string expression (a docstring in position 0)."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


# Node types that open a new binding scope. Split so each user takes exactly the
# set it needs, rather than two hand-maintained lists drifting apart: a
# ``return``/``yield`` search only has to stop at function-ish bodies, while a
# name-substituting rewrite has to stop at comprehensions too.
_FUNCTION_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _own_scope_nodes(body: List[ast.stmt]) -> Iterator[ast.AST]:
    """Every node under ``body`` that belongs to the enclosing function itself.

    Nested ``def``/``lambda``/``class`` bodies are skipped, because a ``return``
    or ``yield`` inside one of them belongs to that scope, not to ours:
    ``ast.walk`` would descend and report ``def f(x):\\n def g(): return 1``
    as having a return.

    Comprehensions are NOT skipped even though they open a scope: a ``return``
    cannot appear in one and a ``yield`` in one is a syntax error, so descending
    is harmless here. ``_LiteralNormalizer`` does have to skip them -- it cares
    about name bindings, not returns -- which is why it uses the wider
    ``_COMPREHENSION_NODES`` on top of these.
    """
    stack: List[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _FUNCTION_SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))


def _returns_only_none_implicitly(body: List[ast.stmt]) -> bool:
    """Whether the function has no ``return`` statement at all, so every* call
    falls off its end and hands back ``None``.

    * Technically could also throw.
    """
    # Any one of the three forces False, so stop at the first.
    return not any(
        isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)) for n in _own_scope_nodes(body)
    )


def _underlying_function(func: Callable) -> Optional[types.FunctionType]:
    """The plain function whose code object describes ``func``'s body.

    ``func`` may be a function, a bound method, or an instance of a callable
    class; the latter two carry their body on ``__func__`` / the class's
    ``__call__``.

    The ``__call__`` lookup goes through :func:`_call_dunder` / :func:`_call_impl`
    so this reads the same function :func:`_held_code` does -- resolving the two
    differently would let the scope be captured from one body while the source was
    validated against another.
    """
    if isinstance(func, types.FunctionType):
        return func
    if inspect.ismethod(func):
        inner = func.__func__
        return inner if isinstance(inner, types.FunctionType) else None
    call = _call_impl(_call_dunder(func))
    if inspect.ismethod(call):
        call = call.__func__
    return call if isinstance(call, types.FunctionType) else None


def _pickled_by_value(obj: Any) -> bool:
    """Whether ``cloudpickle`` snapshots ``obj`` rather than re-importing it.

    A by-reference object is re-imported on the executor, so anything reached
    through it is whatever that process holds -- not what the driver saw.
    """
    return not _should_pickle_by_reference(obj)


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
    ) -> None:
        self._local_names = local_names
        self._cells = cells
        # ``None`` == pickled by reference, so no global is readable.
        self._global_values = global_values

    def lookup_name(self, name: str) -> Any:
        if name in self._local_names:
            # The compiler put this name in ``co_varnames``, so it is assigned
            # somewhere in the body and is therefore local for the WHOLE body:
            # reading it before that assignment raises UnboundLocalError, and
            # resolving it from an enclosing scope would return a value where the
            # UDF actually fails. A body-local that a nested scope closes over
            # lands in ``co_cellvars`` instead and refuses below as unresolvable --
            # but only because ``_analyze_func`` refuses ``global``/``nonlocal``: a
            # nested ``global x`` would put a cellvar in the globals table below.
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

    # Globals follow the function that CARRIES the code: ``_method_reduce``
    # reduces a bound method to its ``__func__``, so gating on the instance's
    # class would bake globals for a method inherited from a by-reference base.
    globals_travel = _pickled_by_value(fn)
    is_callable_instance = not isinstance(func, types.FunctionType) and not inspect.ismethod(func)
    if globals_travel and is_callable_instance:
        # A callable instance reaches its code through ``type(func).__call__``,
        # which cloudpickle ships only when BOTH the class owning ``__call__`` and
        # the receiver's own class are by value. A by-reference class anywhere on
        # that path is re-imported with its original ``__call__``, however the
        # driver's was patched, so the carrying function alone is not enough.
        owner = next((k for k in type(func).__mro__ if "__call__" in k.__dict__), None)
        globals_travel = (
            owner is not None and _pickled_by_value(owner) and _pickled_by_value(type(func))
        )
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
    )


class _LiteralNormalizer(ast.NodeTransformer):
    """Rewrite captured names into constants and substitute literal-valued locals.

    After this runs, the only ``ast.Name`` nodes left are UDF parameters, so the
    lowering code in :class:`CatalystTranspiler` needs no knowledge of scopes.

    Every binding in ``_env`` is an ``ast.Constant``, which is what keeps this a
    plain substitution: a literal has no evaluation to duplicate, defer, or
    discard, so unlike inlining an arbitrary expression there is nothing to track
    about where -- or whether -- the name is read.
    """

    def __init__(self, params: List[str], scope: _CapturedScope, self_param: Optional[str]) -> None:
        self._params = set(params)
        self._scope = scope
        self._self_param = self_param
        # name -> the literal it is bound to, as of the statement being visited
        self._env: Dict[str, ast.Constant] = {}

    @staticmethod
    def _bake(description: str, value: Any) -> ast.expr:
        if value is None or type(value) in _BAKEABLE_TYPES:
            _refuse_unrepresentable_value(description, value)
            return ast.Constant(value=value)
        raise UnsupportedOperationException(
            f"{description} holds a {type(value).__name__}, which has no column "
            "equivalent; only None and basic scalars (int, float, str, bool, "
            "bytes) can be transpiled, so this falls back to interpreted Python"
        )

    def _visit_nested_scope(self, node: ast.AST) -> ast.AST:
        """Leave a nested scope untouched: its own bindings shadow ours.

        A lambda parameter or comprehension target rebinds a name, so
        substituting our literal at those read sites would be a wrong rewrite
        (``b = 5; return [b for b in [a]]`` must not become ``[5 for b in [a]]``).
        Nothing here is lowerable today -- ``_convert_chunk`` has no arm for any
        of these nodes -- so leaving the names in place refuses cleanly instead.
        """
        return node

    # One alias per node type in ``_FUNCTION_SCOPE_NODES + _COMPREHENSION_NODES``.
    # Spelled out rather than registered in a loop, because ``ast.NodeVisitor``
    # dispatches on attribute name and a generated one is invisible to readers and
    # to grep; ``test_udf_transpile_normalizer_skips_every_scope_node`` fails if
    # this list and those tuples drift apart.
    visit_Lambda = _visit_nested_scope
    visit_FunctionDef = _visit_nested_scope
    visit_AsyncFunctionDef = _visit_nested_scope
    visit_ClassDef = _visit_nested_scope
    visit_ListComp = _visit_nested_scope
    visit_SetComp = _visit_nested_scope
    visit_DictComp = _visit_nested_scope
    visit_GeneratorExp = _visit_nested_scope

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if not isinstance(node.ctx, ast.Load):
            return node
        # The local binding table is consulted BEFORE the parameter list: a
        # parameter is an ordinary local and may be rebound to a literal, in which
        # case every later read must see that literal rather than the column.
        # Checking parameters first would turn `def f(a): a = 5; return a * 2`
        # into `a * 2`.
        if node.id in self._env:
            # Fresh node per use so the tree stays free of shared nodes. The
            # wrapped value is always an immutable scalar (see ``_bake``), so
            # rewrapping is enough -- no deep copy needed.
            return ast.Constant(value=self._env[node.id].value)
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

    def _assignment_targets(self, stmt: ast.stmt) -> Tuple[List[str], ast.expr]:
        """The names ``stmt`` binds and the expression it binds them to.

        ``ast.AugAssign`` (``b += 1``) is refused outright rather than desugared to
        ``b = b + 1``: the result is an arithmetic expression, which this
        transpiler does not evaluate. See ``_as_literal``.
        """
        if isinstance(stmt, ast.AugAssign):
            raise UnsupportedOperationException(
                "augmented assignment (`b += ...`) computes a value rather than "
                "binding a literal, which the transpiler does not support; "
                "falling back to interpreted Python"
            )
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
        """Collapse ``body`` to a single statement, refusing what cannot be bound.

        Assignments are consumed here: each binds its name(s) to a literal in
        ``_env``, and later reads of those names substitute it (see
        ``visit_Name``). Only the final statement survives as the returned one.
        """
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
                names, value = self._assignment_targets(stmt)
                # Resolve against the env as it stands BEFORE this statement, then
                # bind. Visiting first is what makes `b = 1` then `b = b` legal
                # while keeping each binding's value the one in scope at the time.
                resolved = self.visit(value)
                literal = _as_literal(resolved)
                if literal is None:
                    raise UnsupportedOperationException(
                        f"the assignment to {', '.join(map(repr, names))} does not "
                        "bind a literal; the transpiler only supports assignments "
                        "whose value is a constant or a captured scalar, not one it "
                        "would have to compute, so this falls back to interpreted "
                        "Python"
                    )
                for name in names:
                    self._env[name] = literal
                if is_last:
                    # An assignment evaluates nothing beyond a literal and yields
                    # no value, so a body ending in one returns None.
                    return ast.Return(value=None)
                continue
            if isinstance(stmt, ast.Pass) and is_last:
                # ``pass`` evaluates nothing, so returning NULL is exact.
                return ast.Return(value=None)
            if isinstance(stmt, ast.Expr) and is_last:
                # A trailing bare expression discards its value, so the function
                # returns None. Only drop it when evaluating it could not have
                # raised: discarding `x % 0` would turn Python's ZeroDivisionError
                # into a NULL, and `x + "abc"` a TypeError. After visiting, a
                # literal or a bare column read is all that is safe to discard.
                discarded = self.visit(stmt.value)
                safe = _as_literal(discarded) is not None or (
                    isinstance(discarded, ast.Name) and discarded.id in self._params
                )
                if not safe:
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
            return self.visit(stmt)
        raise UnsupportedOperationException("the function body is empty")


def _normalize_function(
    func: Callable,
    function_ast: ast.FunctionDef,
    params: List[str],
    receiver: Optional[str],
) -> ast.FunctionDef:
    """Resolve scope and literal assignments, yielding a one-statement body.

    ``receiver`` is the name of the implicit first parameter, or ``None`` when the
    call site supplies every parameter.

    Raises :class:`UnsupportedOperationException` when anything cannot be
    resolved soundly, which the caller turns into a fallback.
    """
    scope = _capture_scope(func)
    normalizer = _LiteralNormalizer(params, scope, receiver)
    # ``ast.NodeTransformer`` rewrites in place, and the caller holds onto
    # ``function_ast`` across builds (a UDF is lowered once to validate it at
    # construction and again when its judf is created). Normalizing the original
    # would let a rebound capture leak between builds, so work on a copy.
    #
    # No depth or size guard is needed. Substituting literals cannot grow the tree,
    # and a body nested deeply enough to exhaust the stack in ``deepcopy`` raises
    # RecursionError, which ``_build_transpiled``'s ``except Exception`` turns into
    # an ordinary fallback -- the same way an over-deep body behaves without any of
    # this rewriting.
    source = copy.deepcopy(function_ast)
    statement = normalizer.normalize_body(source.body)
    # Replace the copy's body rather than constructing a fresh ``ast.FunctionDef``.
    # Every other field (notably ``type_params``, which exists only on 3.12+)
    # carries over untouched, so this needs no per-version handling -- and it
    # avoids omitting constructor fields, which 3.13 deprecates and 3.15 rejects.
    source.body = [ast.fix_missing_locations(statement)]
    return source


class CatalystTranspiler(AbstractTranspiler):
    """Transpiler that attempts to convert a Python UDF into native Spark SQL expressions."""

    variety = "catalyst"

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
                # ``params`` is the caller-facing list, so its indexes are already
                # the ``_udf_param_N`` / category indexes -- see ``_analyze_func``.
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
        """Lower one expression / statement to a Column.

        Adding an arm for a node that OPENS A SCOPE (``Lambda``, ``FunctionDef``,
        a comprehension) requires teaching ``_LiteralNormalizer`` to rename or
        refuse first: it deliberately does not descend into those, so their inner
        names still refer to their own bindings, and lowering one today would
        turn ``b = 5; return [b for b in [a]]`` into ``[5 for b in [a]]``. See
        ``_LiteralNormalizer._visit_nested_scope``.
        """
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
                        # Repeat needs a whole count: Python raises TypeError for
                        # a fractional one, so lowering it would return a value
                        # where Python raises. See
                        # ``_refuse_fractional_repeat_count``.
                        if lc == "string" and rc == "numeric":
                            _refuse_fractional_repeat_count(right)
                            return repeat(left_col, right_col.cast("int"))
                        if lc == "numeric" and rc == "string":
                            _refuse_fractional_repeat_count(left)
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
                # Every literal that reaches a plan passes through here -- one the
                # user wrote, one ``_LiteralNormalizer`` substituted for a captured
                # name, and one it folded out of a literal assignment -- so this is
                # where the value domain has to be checked. Guarding only the
                # capture path let ``b = 1e400`` and ``t = "\ud800"`` straight
                # through, and the surrogate takes the gateway down with it.
                _refuse_unrepresentable_value(f"the literal {value!r}", value)
                return lit(value)
            case ast.Name(id=name, ctx=ast.Load()):
                # Insert columns referencing the param indexes for children
                if name in params:
                    # ``params`` excludes any bound receiver (see ``_analyze_func``),
                    # so its indexes ARE the placeholder indexes. A reference to the
                    # receiver never reaches here: ``_normalize_function`` refuses it
                    # earlier, where the receiver's real name is known.
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
        function_ast: ast.FunctionDef,
        params: List[str],
        returnType: "DataTypeOrString",
        param_categories: Optional[dict] = None,
    ) -> Optional[Column]:
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
    # Deliberately no client-side default: ``RuntimeConfig.get`` returns the
    # default INSTEAD of the driver's registered one, so passing "catalyst" here
    # would shadow ``SQLConf.PYTHON_UDF_TRANSPILERS`` and keep transpiling even
    # for a driver that set it to "". A driver too old to know the conf raises,
    # which ``_build_transpiled`` turns into an ordinary fallback. Same reasoning
    # as ``_transpile_conf_is_true`` in ``udf.py``.
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
    all_args = _positional_args(function_ast)
    public_args = all_args[len(all_args) - n :]
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


def _call_dunder(func: Callable) -> Any:
    """The ``__call__`` entry from ``func``'s type.

    Not ``getattr(func, "__call__")``, which is wrong in two ways that both end with
    lowering a body that never runs: an instance attribute ``obj.__call__ = f``
    shadows the type's for ``getattr`` but is ignored when ``obj`` is called, and on
    a CLASS object it finds the ``__call__`` its instances use while calling the
    class runs ``__init__``.

    ``getattr_static`` looks the name up without firing the descriptor protocol, so
    deciding what to transpile never runs user code -- a custom descriptor used as
    ``__call__`` would otherwise have its ``__get__`` called here.

    Everything comes back undisturbed, so a ``staticmethod`` or ``classmethod``
    arrives as the descriptor rather than the function inside it -- see
    ``_call_impl``. There is always something to return: ``getattr_static`` on a type
    falls through to the metatype, so the floor is ``type.__call__``.
    """
    return inspect.getattr_static(type(func), "__call__")


def _call_impl(entry: Any) -> Any:
    """The function inside a ``staticmethod`` / ``classmethod``, else ``entry`` itself.

    Both get in the way, in opposite directions: they do not forward the wrapped
    function's ``__code__``, and they synthesize a ``__wrapped__`` pointing at it even
    when no decorator is involved. So asking the descriptor directly finds no code
    object and a wraps decorator that is not there -- unwrap before either question.

    Narrow on purpose: unwrapping any ``__func__`` would follow the attribute on
    unrelated callables that expose one, and read the wrong code object.
    """
    return entry.__func__ if isinstance(entry, (staticmethod, classmethod)) else entry


def _held_code(func: Callable) -> Any:
    """The code object that runs when ``func`` is called, or ``None``.

    A function or method runs its own ``__code__``; anything else runs its type's
    ``__call__``. Used only to ask whether we are holding a lambda.
    """
    target = func if (inspect.isfunction(func) or inspect.ismethod(func)) else _call_dunder(func)
    return getattr(_call_impl(target), "__code__", None)


_WARNINGS_LOCK = threading.Lock()


@contextlib.contextmanager
def _syntax_warnings_suppressed() -> Iterator[None]:
    """Parse without re-emitting, or tripping over, the source's own SyntaxWarnings.

    The import already reported them. Without this, ``udf()`` repeats the warning,
    and under warnings-as-errors the parse raises and lowering silently turns off.
    Before 3.12 an invalid escape sequence was a DeprecationWarning, so ignore that
    too rather than lose lowering on the oldest Python we support.

    The lock serializes our own use of ``warnings``, whose state is process-global.
    It cannot serialize anyone else's: a thread entering ``catch_warnings`` while
    this is open has its filters restored from our older snapshot on exit, and
    entering at all bumps the filter version, so a "once"-filtered warning
    elsewhere in the process can fire again. Both are inherent to the stdlib API,
    and are why the parse is the only thing inside here.
    """
    with _WARNINGS_LOCK:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=SyntaxWarning)
            if sys.version_info < (3, 12):
                warnings.filterwarnings(
                    "ignore", message="invalid escape sequence", category=DeprecationWarning
                )
            yield


def _get_ast_from_func(func: Callable) -> Optional[ast.AST]:
    """Try and get the AST from a given callable

    KNOWN LIMITATION: this is the source on disk NOW, not necessarily the source
    ``func`` was compiled from. ``inspect.getsource`` reads through ``linecache``,
    which re-reads an edited file while the code object stays as it was at import,
    so editing a module in a long-lived driver and then building a UDF from a
    function imported earlier lowers the NEW body while Python runs the old one --
    verified: rewriting ``lambda x: x + 1`` to ``x * 9`` gives Python 6, Spark 45.
    Closing it needs the parsed node checked against the held code object; it is
    not tracked separately, being part of the experimental transpiler
    (SPARK-54783). Until then, transpilation assumes source files are not edited
    underneath a running session.
    """
    # Note: consider maybe dill? (see the JYTHON PR)
    # inspect getsource does not work for functions defined in vanilla
    # repl, but does for those in files or in ipython.
    # It also fails when we give it an instance of a callable class.
    try:
        with _syntax_warnings_suppressed():
            return ast.parse(textwrap.dedent(inspect.getsource(func)).strip())
    except Exception:
        try:
            with _syntax_warnings_suppressed():
                return ast.parse(textwrap.dedent(inspect.getsource(_call_dunder(func))).strip())
        except Exception:
            # No usable source (REPL/stdin definition, builtin, ...) --
            # return cleanly so the caller reports "cannot transpile".
            return None


def _positional_args(node: Union[ast.FunctionDef, ast.Lambda]) -> List[ast.arg]:
    """Return the positional argument nodes in order, positional-only first."""
    return node.args.posonlyargs + node.args.args


def _get_parameter_list(node: Union[ast.FunctionDef, ast.Lambda]) -> list[str]:
    """Return the positional argument names in order, positional-only first."""
    return [arg.arg for arg in _positional_args(node)]


def _get_function_from_ast(body: ast.AST, held_code: Any) -> Tuple[Optional[ast.FunctionDef], str]:
    """
    Extract a :class:`ast.FunctionDef` node from an AST produced by
    ``ast.parse(inspect.getsource(udf_func))``.

    Handles the following source patterns (in order):

    * ``f = lambda x: x + 1`` -- lambda bound to a name, annotated or not
    * ``return lambda x: x + 1`` -- lambda returned from a factory function,
      which is what ``inspect.getsource`` yields for ``make_adder(3)``
    * ``lambda x: x + 1`` -- bare expression (getsource on a raw lambda)
    * ``def f(x): ... return x + 1``
    * a class with a ``__call__`` method

    ``held_code`` is the code object that runs when the callable is called; a
    ``co_name`` of ``<lambda>`` is what makes the ambiguity checks below apply, and
    its parameter names are what tell a located lambda apart from a rival.

    Returns the node and an empty reason, or ``None`` and why -- paired so no refusal
    reaches the caller unexplained.
    """
    if not hasattr(body, "body") or not body.body:
        return None, "no statement was found in the source read for this callable"

    stmt = body.body[0]

    # Grab the value side of a top level assign (e.g. x = lambda ...). An annotated
    # binding is the same shape, and the form a typed codebase writes.
    if isinstance(stmt, ast.Assign):
        stmt = stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
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

    # ``inspect.getsource`` works in whole lines, so refuse unless the lambda located
    # here IS the one we hold: anything else lowers a body that never runs
    # (SPARK-58650).
    if getattr(held_code, "co_name", None) == "<lambda>":
        if not isinstance(stmt, ast.Lambda):
            return None, (
                "the source read for this lambda does not define it as a statement of "
                "its own -- it is wrapped in a call or a tuple assignment, or a "
                "surrounding definition, or the file has changed since import and no "
                "longer holds it -- so which lambda to lower cannot be determined"
            )
        # The located lambda must take the parameters the held one does, or it is a
        # different lambda that merely sits where ours was read from. This is what
        # separates a lambda nested in the body of the one we hold (fine: it can never
        # be the UDF) from one that RETURNED the lambda we hold, as in the one-line
        # ``make_adder = lambda n: lambda x: x + n`` -- there the outer lambda is
        # located and the inner is held, and lowering the outer would be wrong.
        located_args = _get_parameter_list(stmt)
        if located_args != list(held_code.co_varnames[: held_code.co_argcount]):
            return None, (
                "the lambda defined in the source read for this one takes different "
                f"parameters ({', '.join(located_args) or 'none'}), so it is not the "
                "lambda being transpiled -- a lambda returning another lambda on one "
                "line, or a file changed since import; put each lambda on its own line"
            )
        # Only lambdas OUTSIDE ``stmt`` are rivals; one in its body cannot be the UDF,
        # and the user could not split it onto another line.
        own = set(map(id, ast.walk(stmt)))
        if any(id(node) not in own for node in ast.walk(body) if isinstance(node, ast.Lambda)):
            return None, (
                "more than one lambda is visible in the source line(s) this one was "
                "read from, and nothing there says which is the UDF, so it is not "
                "safe to lower; put each lambda on its own line to transpile it"
            )

    if isinstance(stmt, ast.Lambda):
        # Synthesize a one-statement FunctionDef wrapping the lambda body so
        # the rest of the transpiler can treat lambdas and ``def`` uniformly.
        fn_ctor: Any = ast.FunctionDef
        synthesized = fn_ctor(
            name="<lambda>",
            args=stmt.args,
            body=[ast.Return(value=stmt.body)],
            decorator_list=[],
        )
        # A node without ``lineno`` cannot be unparsed or compiled; seed from the
        # lambda so positions point at real source rather than line 1.
        return ast.fix_missing_locations(ast.copy_location(synthesized, stmt)), ""

    if isinstance(stmt, ast.FunctionDef):
        return stmt, ""
    return None, (
        f"the source read for this callable is a {type(stmt).__name__}, which the "
        "transpiler cannot reduce to a single function definition"
    )


class _TranspileAnalysis:
    """Everything about a UDF the transpiler can determine without reading the
    values it captures from its enclosing scope.

    Split out from the lowering so the captured values can be read later, at
    ``judf`` creation time, together with the ``cloudpickle`` snapshot the
    interpreted path uses -- see this module's docstring on capture timing.
    """

    def __init__(
        self,
        function_ast: ast.FunctionDef,
        params: List[str],
        public_params: List[str],
        positional_only_public_params: List[str],
        receiver: Optional[str],
        returnType: "DataTypeOrString",
        combos: List[dict],
    ) -> None:
        self.function_ast = function_ast
        self.params = params
        self.public_params = public_params
        # Subset of ``public_params`` Python forbids calling by keyword -- the
        # call-site kwargs-to-positional rewrite in ``udf.py`` must not "fix" a
        # keyword call to one of these, since Python itself would reject it.
        self.positional_only_public_params = positional_only_public_params
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
    # ``_call_impl`` first: a ``staticmethod`` / ``classmethod`` exposes a
    # ``__wrapped__`` of its own, so asking the descriptor refuses every one of
    # them for a wraps decorator that is not there.
    if (
        getattr(func, "__wrapped__", None) is not None
        or getattr(_call_impl(_call_dunder(func)), "__wrapped__", None) is not None
    ):
        return None, [
            "decorated callables (functools.wraps) are not supported: "
            "the visible source is the wrapped function's, not the "
            "wrapper's, so transpilation would change behavior"
        ]
    ast_info = _get_ast_from_func(func)
    if ast_info is None:
        return None, ["Error getting ast for function, cannot transpile"]
    # Get the lambda body and parameters
    function_ast, extraction_error = _get_function_from_ast(ast_info, _held_code(func))
    if function_ast is None:
        return None, [extraction_error]
    # This walk covers nested scopes, not just the top level: ``_extract_code_globals``
    # recurses into nested code objects, so a nested ``global x`` puts a name that is
    # ALSO a body-local into the global capture table, where it would resolve to the
    # module value for a body that raises UnboundLocalError.
    if any(isinstance(n, (ast.Global, ast.Nonlocal)) for n in ast.walk(function_ast)):
        return None, [
            "`global` / `nonlocal` declarations rebind names outside the "
            "function and are not supported by the transpiler"
        ]
    # Default/variadic/keyword-only params can't map to positional
    # ``_udf_param_N`` placeholders -- the call site can skip them. A bare
    # positional-only param has no such gap, so it's not rejected here; a
    # defaulted one still hits ``fn_args.defaults`` below.
    fn_args = function_ast.args
    if (
        fn_args.defaults
        or any(d is not None for d in fn_args.kw_defaults)
        or fn_args.kwonlyargs
        or fn_args.vararg is not None
        or fn_args.kwarg is not None
    ):
        return None, [
            "functions with default, variadic, or keyword-only "
            "arguments are not supported by the transpiler"
        ]
    params = _get_parameter_list(function_ast)
    # The FunctionDef above was recovered from TEXT, which can describe a
    # different function than the one that runs: for ``def make(): return lambda
    # x: x + N``, ``inspect.getsource`` on the returned lambda yields the whole
    # ``def make()`` line, so the outer def is what gets picked. Its parameters
    # must match the code object whose locals ``_capture_scope`` reads, or names
    # resolve against an unrelated scope.
    fn = _underlying_function(func)
    if fn is not None and list(fn.__code__.co_varnames[: fn.__code__.co_argcount]) != params:
        return None, [
            "the source recovered for this UDF describes a different function "
            "than the one it will run (its parameters do not match); falling "
            "back to interpreted Python"
        ]
    # Drop a receiver that is already bound, so what is left is what the call
    # site supplies. Decided by HOW ``func`` dispatches, not by the parameter's
    # name: a bound ``__call__(this, x)`` or ``@classmethod f(cls, x)`` has a
    # receiver not named ``self``, while a plain ``def f(self, x)`` supplies its
    # ``self`` at the call site. Going by the name misnumbered every
    # ``_udf_param_N`` -- a two-column call on ``__call__(this, x)`` read column b
    # for ``x`` and returned a value where Python raises TypeError. Asking
    # ``inspect.signature`` is both weaker (a ``__signature__`` off by exactly one
    # is undetectable) and worse behaved (it runs user code).
    #
    # For a callable instance, two things can consume a leading parameter, and
    # they compose: what the descriptor prepends when Python looks ``__call__``
    # up -- the instance for a plain function, the class for a ``classmethod``,
    # nothing for a ``staticmethod`` or for an already-bound method, whose
    # ``__get__`` returns itself -- and whatever the callable already has bound.
    # Each count below is checked against what Python returns for that shape.
    if inspect.isfunction(func):
        spoken_for = 0
    elif inspect.ismethod(func):
        spoken_for = 1
    else:
        call_entry = _call_dunder(func)
        call_target = _call_impl(call_entry)
        if not (inspect.isfunction(call_target) or inspect.ismethod(call_target)):
            # A slot wrapper, property, partial, or other descriptor: what it
            # prepends is not knowable from here.
            return None, [
                f"a {type(call_entry).__name__} as __call__ does not say which "
                "parameters the call site supplies, so the placeholder "
                "positions cannot be assigned"
            ]
        spoken_for = int(
            inspect.isfunction(call_entry) or isinstance(call_entry, classmethod)
        ) + int(inspect.ismethod(call_target))
    if spoken_for > 1 or spoken_for > len(params):
        # Two receivers at once -- a ``classmethod`` over an already-bound method
        # prepends the class ON TOP of the method's own ``__self__`` -- or one with
        # no parameter to hold it. Python raises for whatever the call site passes,
        # so there is nothing correct to lower.
        return None, ["callable leaves no parameter for the call site to bind"]
    # Caller-facing params: callers match user-supplied kwargs against these, and
    # the receiver is not named at the call site. Everything downstream indexes
    # off THIS list, so the placeholder numbering needs no offset.
    public_params = params[spoken_for:]
    posonly_names = {arg.arg for arg in function_ast.args.posonlyargs}
    positional_only_public_params = [p for p in public_params if p in posonly_names]
    receiver = params[0] if spoken_for else None
    # Warned here rather than while lowering: this depends only on the AST, and
    # ``_build_transpiled`` runs again for every ``judf`` and every read of the
    # ``transpiled`` property, so warning there would repeat for the UDF's life.
    # Warned even when the LOWERING goes on to fail -- the user still wants to know
    # the function never returns a value.
    if _returns_only_none_implicitly(function_ast.body):
        warnings.warn(
            f"UDF {func} has no return statement, so it always returns None "
            "(NULL) or raises; add a `return` if that was not intended.",
            RuntimeWarning,
        )
    return (
        _TranspileAnalysis(
            function_ast=function_ast,
            params=params,
            public_params=public_params,
            positional_only_public_params=positional_only_public_params,
            receiver=receiver,
            returnType=returnType,
            combos=_param_category_combos(function_ast, public_params),
        ),
        [],
    )


def _can_transpile(
    session: "SparkSession", func: Callable[..., Any], analysis: _TranspileAnalysis
) -> Tuple[bool, List[str]]:
    """Whether ``func`` lowers at all right now, and the refusals if it does not.

    For validating a UDF where it is defined. Returns a bool rather than the
    expressions so a truncated option list cannot be mistaken for the full set
    and end up in a plan -- lowering every variant just to discard it costs a py4j
    roundtrip per node.
    """
    options, errors, _ = _build_transpiled(session, func, analysis, first_only=True)
    return bool(options), errors


def _build_transpiled(
    session: "SparkSession",
    func: Callable[..., Any],
    analysis: _TranspileAnalysis,
    first_only: bool = False,
) -> Tuple[List[Column], List[str], List[List[str]]]:
    """Resolve captured values and lower ``func`` to Catalyst expressions.

    Call this at ``judf`` creation time: the captured values it reads are baked
    into the returned expressions, and they must be the same values
    ``_wrap_function``'s ``cloudpickle`` snapshot sees.

    ``first_only`` stops at the first option produced and so returns a TRUNCATED
    option list, which must never reach a plan. Callers asking only "does this
    lower at all?" should go through :func:`_can_transpile`, which returns a bool
    and cannot be mistaken for the full set.
    """
    errors: List[str] = []
    try:
        # Resolve free variables and literal assignments once, up front: the
        # result is value-dependent but category-independent, so it is shared by
        # every input-type variant below.
        normalized = _normalize_function(
            func, analysis.function_ast, analysis.params, analysis.receiver
        )
        transpilers = _get_transpilers(session)
    except Exception as e:
        return [], [str(e)], []
    transpiled: List[Column] = []
    input_categories: List[List[str]] = []
    # One transpiled option per (backend x input-type variant). Untyped
    # params are tried as both numeric and string so the JVM can pick the
    # option matching the actual column types (or fall back if none match).
    # Maybe multiple transpilers (think CUDA, etc.).
    for transpiler in transpilers:
        for combo in analysis.combos:
            try:
                transpiled_column = transpiler._transpile_from_ast(
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
                    if first_only:
                        return transpiled, errors, input_categories
            except Exception as e:
                errors.append(str(e))
    return transpiled, errors, input_categories
