"""Architecture test: no test in ``tests/`` may be dead on arrival or assert nothing.

A test that never runs, or runs and checks nothing, still counts as coverage. The
suite once carried two ``skipif(True)`` polars round trips from the first release,
ten ``@pytest.mark.skip`` tests, a module that skipped 35 functions at import, and
77 test functions with no assertion. Nothing stopped the next one landing with CI
green; this does. It flags, statically:

* **Literal skips.** ``skipif(<truthy constant>)`` and ``@pytest.mark.skip`` anywhere,
  including ``pytestmark`` and ``pytest.param(marks=...)``.
* **Lenient xfails.** ``xfail(strict=False)`` passes whether or not the test fails (pytest
  runs every other xfail strict, ``xfail_strict`` in pyproject.toml), and
  ``xfail(run=False)`` never runs it.
* **Unconditional imperative skips.** A ``pytest.skip(...)`` or ``pytest.xfail(...)``
  statement directly in a function or module body, outside any ``if``/``try``/loop. A
  skip that depends on the environment (an optional import, a platform) sits inside one
  of those and passes.
* **Assertion-free tests.** A ``test_*`` function whose body, and the functions in the
  same module it calls (directly or as ``self.``/``cls.`` methods of its class), contain no
  ``assert``, no ``pytest.raises``/``warns``/``fail``, no ``raise AssertionError`` and no
  call to an ``assert*`` function (``mock.assert_called_once``,
  ``np.testing.assert_array_equal``, ``self.assertEqual``). ``assert True`` does not count,
  nor does a function defined inside the test that it does not call by name (a thread
  target's failed assert is only printed). Helpers imported from other modules and
  assertions inside fixtures do not count either: assert in the test or in a helper
  defined beside it.

A test that is meant to show "this does not raise" still asserts the state it leaves
behind. A test that cannot pass yet is ``xfail(strict=True)`` naming the defect, so it
fails the day the defect is fixed.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
RAISING_CHECKS = frozenset({"pytest.raises", "pytest.warns", "pytest.fail", "pytest.deprecated_call", "self.fail"})
CONDITIONAL = (ast.If, ast.IfExp, ast.Try, ast.ExceptHandler, ast.BoolOp, ast.For, ast.AsyncFor, ast.While, ast.Match)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module)
Func = ast.FunctionDef | ast.AsyncFunctionDef


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _is_signal(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return not _is_true(node.test)
    if isinstance(node, ast.Raise) and node.exc is not None:
        return _dotted(node.exc).endswith("AssertionError")
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        return name.rsplit(".", 1)[-1].lstrip("_").startswith("assert") or name in RAISING_CHECKS
    return False


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and not isinstance(node.value, str) and bool(node.value)


class _Module:
    def __init__(self, path: Path) -> None:
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.funcs: dict[str, Func] = {}
        self.classes: dict[str, dict[str, Func]] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = {
                    n.name: n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                }

    def tests(self):
        yield from ((None, fn) for name, fn in self.funcs.items() if name.startswith("test_"))
        for cls, methods in self.classes.items():
            if cls.startswith("Test"):
                yield from ((cls, fn) for name, fn in methods.items() if name.startswith("test_"))

    def _callee(self, cls: str | None, call: ast.Call) -> Func | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.funcs.get(func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("self", "cls") and cls:
            return self.classes[cls].get(func.attr)
        return None

    def asserts(self, fn: Func, cls: str | None, seen: frozenset[int] = frozenset()) -> bool:
        if id(fn) in seen:
            return False
        nodes = list(_own_nodes(fn))
        if any(_is_signal(n) for n in nodes):
            return True
        nested = {n.name: n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        calls = [n for n in nodes if isinstance(n, ast.Call)]
        callees = (
            nested.get(c.func.id) if isinstance(c.func, ast.Name) and c.func.id in nested else self._callee(cls, c)
            for c in calls
        )
        return any(self.asserts(c, cls, seen | {id(fn)}) for c in callees if c is not None)


def _own_nodes(fn: Func):
    """``fn``'s nodes, minus the bodies of the functions and lambdas defined inside it.

    A nested function counts only where ``fn`` calls it by name: an assert in a thread target
    (``Thread(target=worker)``) only prints a warning when it fails, and never fails the test.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _is_unconditional(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None and not isinstance(current, SCOPES):
        if isinstance(current, CONDITIONAL):
            return False
        current = parents.get(current)
    return True


def _marker_violation(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    if isinstance(node, ast.Attribute) and _dotted(node).endswith("mark.skip"):
        return "unconditional @pytest.mark.skip"
    if not isinstance(node, ast.Call):
        return None
    name = _dotted(node.func)
    if name.endswith("mark.skipif"):
        condition = node.args[0] if node.args else _keyword(node, "condition")
        return f"skipif({condition.value!r}) never runs" if _is_true(condition) else None
    if name.endswith("mark.xfail"):
        strict, run = _keyword(node, "strict"), _keyword(node, "run")
        if isinstance(strict, ast.Constant) and not strict.value:
            return "xfail(strict=False) passes whether or not the test fails"
        return "xfail(run=False) never runs" if isinstance(run, ast.Constant) and not run.value else None
    if name in ("pytest.skip", "pytest.xfail") and _is_unconditional(node, parents):
        return f"unconditional {name}()"
    return None


def scan(root: Path) -> list[str]:
    """Every hygiene violation under ``root`` (a ``tests``-shaped directory), as ``path:line: why``."""
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        mod = _Module(path)
        where = str(path.relative_to(root.parent))
        parents = _parents(mod.tree)
        for node in ast.walk(mod.tree):
            why = _marker_violation(node, parents)
            if why:
                out.append(f"{where}:{node.lineno}: {why}")
        if path.name.startswith("test_"):
            for cls, fn in mod.tests():
                if not mod.asserts(fn, cls):
                    out.append(f"{where}:{fn.lineno}: {cls + '.' if cls else ''}{fn.name} asserts nothing")
    return sorted(out)


def test_no_dead_or_assertion_free_tests() -> None:
    violations = scan(TESTS)
    assert not violations, (
        "Dead or assertion-free tests. Fix the test, delete it, or mark it xfail(strict=True) naming the defect; "
        f"what counts is in the docstring of {Path(__file__).relative_to(TESTS.parent)}:\n" + "\n".join(violations)
    )


PLANTED = """
import threading

import pytest

pytestmark = pytest.mark.skip(reason="module")


@pytest.mark.skipif(True, reason="never")
def test_literal_skipif():
    assert len("ok") == 2


@pytest.mark.skipif(condition=1, reason="never")
def test_literal_skipif_keyword():
    assert len("ok") == 2


@pytest.mark.skip
def test_marker_skip():
    assert len("ok") == 2


@pytest.mark.xfail(strict=False, reason="passes either way")
def test_lenient_xfail():
    assert len("ok") == 2


@pytest.mark.xfail(reason="strict through xfail_strict")
def test_xfail_is_strict_by_default():
    assert len("ok") == 2


def test_assert_true_is_not_a_check():
    assert True


def test_thread_target_assert_is_not_a_check():
    def worker():
        assert len("ok") == 2

    threading.Thread(target=worker).start()


def test_nested_helper_called_by_name_counts():
    def check():
        assert len("ok") == 2

    check()


@pytest.mark.xfail(strict=True, run=False, reason="never runs")
def test_unrun_xfail():
    assert len("ok") == 2


def test_imperative_skip():
    pytest.skip("not today")
    assert len("ok") == 2


def test_assert_free():
    print("ran")


class TestThings:
    def test_method_assert_free(self):
        self._noop()

    def _noop(self):
        return 1

    def test_via_method_helper(self):
        self._checks()

    def _checks(self):
        assert len("ok") == 2


def test_conditional_skip_is_fine():
    try:
        import nothing_here  # noqa: F401
    except ImportError:
        pytest.skip("optional")
    assert len("ok") == 2


@pytest.mark.skipif("sys.platform == 'win32'", reason="string conditions are evaluated")
@pytest.mark.xfail(strict=True, raises=ValueError, reason="a known defect")
def test_via_module_helper():
    check_it()


def check_it():
    assert len("ok") == 2


def test_raises_counts():
    with pytest.raises(ValueError):
        int("x")


def test_mock_assert_counts(mocker):
    mocker.assert_called_once()
"""


def test_scan_flags_each_planted_violation_and_nothing_else(tmp_path: Path) -> None:
    root = tmp_path / "tests"
    root.mkdir()
    (root / "test_planted.py").write_text(PLANTED)

    assert sorted(line.split(": ", 1)[1] for line in scan(root)) == [
        "TestThings.test_method_assert_free asserts nothing",
        "skipif(1) never runs",
        "skipif(True) never runs",
        "test_assert_free asserts nothing",
        "test_assert_true_is_not_a_check asserts nothing",
        "test_thread_target_assert_is_not_a_check asserts nothing",
        "unconditional @pytest.mark.skip",
        "unconditional @pytest.mark.skip",
        "unconditional pytest.skip()",
        "xfail(run=False) never runs",
        "xfail(strict=False) passes whether or not the test fails",
    ]
