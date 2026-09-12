"""Architecture test: no logging call may receive a raw cache key or raw exception text (CWE-532, LAB-304).

The redaction sweep on PR #264 hand-edited ~30 log lines. Nothing stopped the
next ``logger.debug(f"... {key}")`` from landing with CI green — this does.

For every logging call under ``src/cachekit`` — receiver a logger name
(``logger``, ``_logger``, ``self._logger``, ``logger_instance``, ``logging``,
``warnings``), a logger factory call (``get_logger()``, ``logger()``,
``logging.getLogger(...)``), a function imported directly from ``logging`` /
``warnings`` (``from logging import warning``, aliases included), an aliased
module (``import logging as lg``), or ``getattr(logger, level)(...)``:

* **Keys.** Any Name, Attribute, or ``d["..."]`` subscript whose identifier is
  key-shaped (``key``, ``cache_key``, ``lock_key``, ``e.key``, ``kwargs["key"]``)
  must be wrapped in ``redact_cache_key`` / ``redact_key_for_log`` somewhere
  between it and the call: in the message f-string, ``%s`` arguments, or ``extra=``.
* **Exceptions.** An exception's ``str()`` has unknown provenance (a redis
  ResponseError naming the key, a ``BackendError`` whose free-form message was
  built with it). Any exception-shaped identifier — every name bound by an
  ``except ... as <name>`` in the same file, plus the conventional names
  ``e``/``ex``/``exc``/``err``/``error``/``exception`` and any ``*_err``-style
  suffix, for parameters such as ``error: Exception`` — or an attribute of one,
  must be wrapped in ``redact_error_for_log``. ``type(e).__name__`` is allowed.
* **Tracebacks.** ``logger.exception(...)`` and ``exc_info=`` are flagged
  outright: the traceback carries the raw exception text whatever the message says.

Known blind spots (flow-insensitive): a message pre-built into a variable
(``msg = f"miss {key}"; logger.debug(msg)``) is not traced, and an exception
held in a parameter with an unconventional name (``failure: Exception``) is not
recognised. Build log lines inline, and bind exceptions with ``except ... as``
or a conventional name, so the guard can see them. Sink-central redaction is not
exempted: the sinks' own stdlib calls satisfy the rule; callers passing raw keys
*into* ``handle_cache_error`` / ``log_cache_operation`` / ``SimpleLogger.cache_*``
are covered by those sinks' contract tests, not here.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cachekit"

LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "critical", "exception", "log"})
# logger, _logger, logger_instance, logging, warnings — plus bare log / _log receivers.
# The (?:^|_)log(?:ger|ging)?(?:_|$) arm anchors on a word boundary so key-shaped names
# that merely contain "log" (catalog, dialog, backlog) are not treated as loggers.
LOGGER_NAME_RE = re.compile(r"(?:^|_)log(?:ger|ging)?(?:_|$)|^warnings$")
LOGGER_FACTORIES = frozenset({"get_logger", "logger", "getLogger", "get_structured_logger"})
KEY_REDACTORS = frozenset({"redact_cache_key", "redact_key_for_log"})
ERROR_REDACTORS = frozenset({"redact_error_for_log", "type"})  # type(e).__name__ is key-free
KEY_NAME_RE = re.compile(r"(?:^|_)key$")
EXC_NAME_RE = re.compile(r"(?:^|_)(?:e|ex|exc|err|error|exception)$")


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _call_args(node: ast.Call) -> list[ast.expr]:
    return [*node.args, *(kw.value for kw in node.keywords)]


def _is_logger_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return bool(LOGGER_NAME_RE.search(node.id))
    if isinstance(node, ast.Attribute):  # self.logger / self._logger
        return bool(LOGGER_NAME_RE.search(node.attr))
    if isinstance(node, ast.Call):  # get_logger().warning(...) / logging.getLogger(__name__).info(...)
        return _call_name(node) in LOGGER_FACTORIES
    return False


LOG_MODULES = frozenset({"logging", "warnings"})


def _direct_log_names(tree: ast.AST) -> tuple[dict[str, str], frozenset[str]]:
    """Names bound by importing from the logging modules directly.

    Returns (functions, module_aliases): ``from logging import warning as w`` binds the
    function ``w`` (mapped back to ``warning``); ``import logging as lg`` binds the module
    alias ``lg`` — a receiver the name regex would otherwise miss.
    """
    funcs: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in LOG_MODULES:
            funcs.update({a.asname or a.name: a.name for a in node.names if a.name in LOG_METHODS | {"getLogger"}})
        elif isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name in LOG_MODULES)
    return funcs, frozenset(modules)


def _is_logger_call(node: ast.Call, direct: dict[str, str] | None = None, aliases: frozenset[str] = frozenset()) -> bool:
    func = node.func
    if isinstance(func, ast.Name):  # from logging import warning; warning("%s", key)
        return func.id in (direct or {})
    if isinstance(func, ast.Attribute):
        receiver = func.value
        aliased = isinstance(receiver, ast.Name) and receiver.id in aliases  # import logging as lg; lg.warning(...)
        return func.attr in LOG_METHODS and (aliased or _is_logger_receiver(receiver))
    # getattr(logger, level.lower())(message, ...)
    return isinstance(func, ast.Call) and _call_name(func) == "getattr" and bool(func.args) and _is_logger_receiver(func.args[0])


def _key_identifier(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and KEY_NAME_RE.search(node.id):
        return node.id
    if isinstance(node, ast.Attribute) and KEY_NAME_RE.search(node.attr):
        return ast.unparse(node)
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and KEY_NAME_RE.search(str(node.slice.value)):
        return ast.unparse(node)
    return None


def _exception_identifier(bound: frozenset[str]) -> Callable[[ast.AST], str | None]:
    """Predicate for exception-shaped roots: names bound by ``except ... as`` in this file, or conventional names."""

    def ident(node: ast.AST) -> str | None:
        root = node
        while isinstance(root, (ast.Attribute, ast.Subscript)):  # e.message, e.args[0]
            root = root.value
        if isinstance(root, ast.Name) and (root.id in bound or EXC_NAME_RE.search(root.id)):
            return root.id
        return None

    return ident


def _unredacted(node: ast.AST, ident: Callable[[ast.AST], str | None], redactors: frozenset[str]) -> list[str]:
    """Identifiers matching ``ident`` under ``node`` that are not enclosed by a call to one of ``redactors``."""
    found_here = ident(node)
    if found_here is not None:
        return [found_here]
    found: list[str] = []
    if isinstance(node, ast.Call):
        # The callee's own name is never a key (``redact_cache_key`` ends in ``_key``);
        # only its receiver chain (``obj.key.method()``) can carry one.
        if isinstance(node.func, ast.Attribute):
            found.extend(_unredacted(node.func.value, ident, redactors))
        if _call_name(node) not in redactors:
            for child in _call_args(node):
                found.extend(_unredacted(child, ident, redactors))
        return found
    if isinstance(node, ast.IfExp):
        # ``redact(key) if key else "unknown"`` — the test is a truthiness check, it never renders.
        return _unredacted(node.body, ident, redactors) + _unredacted(node.orelse, ident, redactors)
    for child in ast.iter_child_nodes(node):
        found.extend(_unredacted(child, ident, redactors))
    return found


def _emits_traceback(node: ast.Call, direct: dict[str, str] | None = None) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "exception":
        return True
    if isinstance(func, ast.Name) and (direct or {}).get(func.id) == "exception":  # from logging import exception as x
        return True
    return any(kw.arg == "exc_info" for kw in node.keywords)


def _except_names(tree: ast.AST) -> frozenset[str]:
    return frozenset(h.name for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and h.name)


def _violations_in(tree: ast.AST, where: str) -> list[str]:
    out: list[str] = []
    exc_ident = _exception_identifier(_except_names(tree))
    direct, aliases = _direct_log_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_logger_call(node, direct, aliases):
            continue
        loc = f"{where}:{node.lineno}"
        keys = [k for arg in _call_args(node) for k in _unredacted(arg, _key_identifier, KEY_REDACTORS)]
        if keys:
            out.append(f"{loc} logs raw {', '.join(sorted(set(keys)))}")
        excs = [k for arg in _call_args(node) for k in _unredacted(arg, exc_ident, ERROR_REDACTORS)]
        if excs:
            out.append(f"{loc} logs raw exception text {', '.join(sorted(set(excs)))} (wrap in redact_error_for_log)")
        if _emits_traceback(node, direct):
            out.append(f"{loc} emits a traceback (logger.exception / exc_info) — raw exception text")
    return out


def _violations(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        out.extend(_violations_in(tree, str(path.relative_to(root.parents[1]))))
    return out


def test_no_raw_key_or_exception_text_reaches_a_logger_call() -> None:
    violations = _violations(SRC)
    assert not violations, "Raw cache keys or exception text reach a logger call:\n  " + "\n  ".join(violations)


def test_detector_catches_the_shapes_it_claims_to() -> None:
    """The guard is only as good as its detector — pin the shapes it must flag and must allow."""
    cases = [
        # keys
        ("logger.debug(f'hit {key}')", True),  # f-string
        ("logger.debug('miss %s', cache_key)", True),  # %-args
        ("self._logger.warning('x', extra={'k': e.key})", True),  # attribute in extra=
        ("get_logger().error(f'set failed for {cache_key}')", True),  # factory-call receiver (cache_handler.py style)
        ("logger().warning(f'{lock_key}')", True),  # module-level factory (wrapper.py style)
        ("logging.getLogger(__name__).info('%s', kwargs['key'])", True),  # getLogger + subscript
        ("getattr(logger, level.lower())(f'{cache_key}')", True),  # orchestrator.log_structured style
        ("logger_instance.warning(f'{cache_key}')", True),  # any *logger-suffixed receiver
        ("_log.warning('cache failure: %s', cache_key)", True),  # bare _log receiver
        ("log.warning(f'{cache_key}')", True),  # bare log receiver
        ("catalog.get(key)", False),  # 'log' substring is not a logger
        ("logger.info('ok %s', redact_key_for_log(key))", False),  # redacted %-arg
        ("logger.info(f'{redact_cache_key(lock_key)}')", False),  # redacted f-string
        ("logger.debug('%d keys', len(expired_keys))", False),  # plural: not a key
        ("get_logger().warning(f\"{redact_cache_key(cache_key) if cache_key else 'unknown'}\")", False),  # truthiness test
        ("client.get(key)", False),  # not a logger
        ("from logging import warning\nwarning('%s', cache_key)", True),  # directly imported function
        ("from logging import error as log_err\nlog_err(f'{cache_key}')", True),  # aliased direct import
        ("from warnings import warn\nwarn(f'{cache_key}')", True),  # warnings.warn imported directly
        ("import logging as lg\nlg.warning('%s', cache_key)", True),  # aliased module receiver
        ("from logging import getLogger\ngetLogger(__name__).info('%s', cache_key)", True),  # direct getLogger factory
        ("from logging import exception\nexception('boom')", True),  # directly imported traceback emitter
        ("def warning(msg): pass\nwarning(f'{cache_key}')", False),  # same name, not imported from logging
        # exception text
        ("logger.warning(f'set failed for {redact_cache_key(cache_key)}: {e}')", True),  # f-string {e}
        ("_logger.debug('TTL refresh failed for %s: %s', redact_cache_key(cache_key), exc)", True),  # %-arg exc
        ("logger.error(f'decrypt failed: {error!s}')", True),  # !s conversion
        ("logger.error(f'failed: {e.message}')", True),  # attribute of an exception
        ("logger.warning(f'evict failed: {del_err}')", True),  # *_err suffix
        ("logger.debug('x: %s', import_err)", True),  # *_err suffix, %-arg
        (
            "try:\n    pass\nexcept ValueError as failure:\n    logger.error(f'{failure}')",
            True,
        ),  # except-bound, unconventional name
        ("logger.error('failed', exc_info=True)", True),  # traceback
        ("logger.exception('failed')", True),  # traceback
        ("logger.warning(f'failed: {redact_error_for_log(e)}')", False),  # redacted
        ("logger.warning('failed: %s', redact_error_for_log(exc))", False),  # redacted %-arg
        ("logger.warning(f'failed: {type(e).__name__}')", False),  # type name is key-free
        ("def f(failure):\n    logger.error(f'{failure}')", False),  # unconventional parameter: documented blind spot
    ]
    for src, expected in cases:
        flagged = bool(_violations_in(ast.parse(src), "<case>"))
        assert flagged is expected, f"{src!r}: expected flagged={expected}, got {flagged}"
