"""Architecture test: no stdlib logger call may receive a raw cache key (CWE-532, LAB-304).

The redaction sweep on PR #264 hand-edited ~30 log lines. Nothing stopped the
next ``logger.debug(f"... {key}")`` from landing with CI green — this does.

For every logging call under ``src/cachekit`` — receiver a logger name
(``logger``, ``_logger``, ``self._logger``, ``logger_instance``, ``logging``,
``warnings``), a logger factory call (``get_logger()``, ``logger()``,
``logging.getLogger(...)``), or ``getattr(logger, level)(...)`` — any Name,
Attribute, or ``d["..."]`` subscript whose identifier is key-shaped (``key``,
``cache_key``, ``lock_key``, ``e.key``, ``kwargs["key"]`` ...) must be wrapped
in ``redact_cache_key`` / ``redact_key_for_log`` somewhere between it and the
call: in the message f-string, in ``%s`` arguments, or in ``extra=``.

Known blind spot (flow-insensitive): a message pre-built into a variable
(``msg = f"miss {key}"; logger.debug(msg)``) is not traced. Build log lines
inline so the guard can see them. Sink-central redaction is not exempted: the
sinks' own stdlib calls satisfy the rule; callers passing raw keys *into*
``handle_cache_error`` / ``log_cache_operation`` / ``SimpleLogger.cache_*`` are
covered by those sinks' contract tests, not here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cachekit"

LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "critical", "exception", "log"})
# logger, _logger, logger_instance, logging, warnings — plus bare log / _log receivers.
# The (?:^|_)log(?:ger|ging)?(?:_|$) arm anchors on a word boundary so key-shaped names
# that merely contain "log" (catalog, dialog, backlog) are not treated as loggers.
LOGGER_NAME_RE = re.compile(r"(?:^|_)log(?:ger|ging)?(?:_|$)|^warnings$")
LOGGER_FACTORIES = frozenset({"get_logger", "logger", "getLogger", "get_structured_logger"})
REDACTORS = frozenset({"redact_cache_key", "redact_key_for_log"})
KEY_NAME_RE = re.compile(r"(?:^|_)key$")


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


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in LOG_METHODS and _is_logger_receiver(func.value)
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


def _raw_keys(node: ast.AST) -> list[str]:
    """Key-shaped identifiers under ``node`` not enclosed by a redactor call."""
    ident = _key_identifier(node)
    if ident is not None:
        return [ident]
    found: list[str] = []
    if isinstance(node, ast.Call):
        # The callee's own name is never a key (``redact_cache_key`` ends in ``_key``);
        # only its receiver chain (``obj.key.method()``) can carry one.
        if isinstance(node.func, ast.Attribute):
            found.extend(_raw_keys(node.func.value))
        if _call_name(node) not in REDACTORS:
            for child in _call_args(node):
                found.extend(_raw_keys(child))
        return found
    if isinstance(node, ast.IfExp):
        # ``redact(key) if key else "unknown"`` — the test is a truthiness check, it never renders.
        return _raw_keys(node.body) + _raw_keys(node.orelse)
    for child in ast.iter_child_nodes(node):
        found.extend(_raw_keys(child))
    return found


def _violations(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_logger_call(node):
                continue
            leaks = [k for arg in _call_args(node) for k in _raw_keys(arg)]
            if leaks:
                out.append(f"{path.relative_to(root.parents[1])}:{node.lineno} logs raw {', '.join(sorted(set(leaks)))}")
    return out


def test_no_raw_cache_key_reaches_a_logger_call() -> None:
    violations = _violations(SRC)
    assert not violations, "Raw cache keys reach a logger call (wrap in redact_key_for_log):\n  " + "\n  ".join(violations)


def test_detector_catches_the_shapes_it_claims_to() -> None:
    """The guard is only as good as its detector — pin the shapes it must flag and must allow."""
    cases = [
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
    ]
    for src, expected in cases:
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logger_call(n)]
        flagged = any(_raw_keys(a) for c in calls for a in _call_args(c))
        assert flagged is expected, f"{src!r}: expected flagged={expected}, got {flagged}"
