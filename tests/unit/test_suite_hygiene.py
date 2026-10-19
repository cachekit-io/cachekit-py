"""Architecture test: no test in ``tests/`` may be dead on arrival or assert nothing.

A test that never runs, or runs and checks nothing, still counts as coverage. The
suite once carried two ``skipif(True)`` polars round trips from the first release,
ten ``@pytest.mark.skip`` tests, a module that skipped 35 functions at import, and
77 test functions with no assertion. Nothing stopped the next one landing with CI
green; this does. It flags, statically:

* **Literal skips.** ``skipif(<truthy constant>)`` and ``@pytest.mark.skip`` anywhere,
  including ``pytestmark`` and ``pytest.param(marks=...)``.
* **Unconditional imperative skips.** A ``pytest.skip(...)`` statement directly in a
  function or module body, outside any ``if``/``try``/loop. A skip that depends on the
  environment (an optional import, a platform) sits inside one of those and passes.
* **Assertion-free tests.** A ``test_*`` function whose body, the helpers it calls and
  the fixtures it requests contain no ``assert``, no ``pytest.raises``/``warns``/``fail``,
  no ``raise AssertionError`` and no call to an ``assert*`` function (``mock.assert_called_once``,
  ``np.testing.assert_array_equal``, ``self.assertEqual``). Helpers resolve within the
  module (functions, ``self.`` methods, classes in the module) and through ``from tests...``
  imports; fixtures resolve through the module, its class and the conftest chain.

A test that is meant to show "this does not raise" still asserts the state it leaves
behind. A test that cannot run yet is ``xfail(strict=True)`` with a ticket, so it fails
the day it starts passing.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
RAISING_CHECKS = frozenset({"raises", "warns", "fail", "deprecated_call"})
CONDITIONAL = (ast.If, ast.IfExp, ast.Try, ast.ExceptHandler, ast.For, ast.AsyncFor, ast.While, ast.Match)


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
        return True
    if isinstance(node, ast.Raise) and node.exc is not None:
        return _dotted(node.exc).endswith("AssertionError")
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        last = name.rsplit(".", 1)[-1]
        return last.lstrip("_").startswith("assert") or name in {f"pytest.{c}" for c in RAISING_CHECKS} or name == "self.fail"
    return False


def _is_fixture(fn: ast.AST) -> bool:
    return any(_dotted(d).endswith("fixture") for d in getattr(fn, "decorator_list", ()))


def _fixture_name(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for d in fn.decorator_list:
        for kw in getattr(d, "keywords", ()):
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    return fn.name


Func = ast.FunctionDef | ast.AsyncFunctionDef


class _Module:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.name = ".".join(path.relative_to(root.parent).with_suffix("").parts)
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.funcs: dict[str, Func] = {}
        self.classes: dict[str, dict[str, Func]] = {}
        self.bases: dict[str, list[str]] = {}
        self.imports: dict[str, tuple[str, str]] = {}
        self.fixtures: dict[str, Func] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = {
                    n.name: n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.bases[node.name] = [_dotted(b) for b in node.bases]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture(node):
                self.fixtures[_fixture_name(node)] = node


class _Suite:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules = {
            m.name: m for m in (_Module(p, root) for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts)
        }
        self._memo: dict[tuple[str, int], bool] = {}

    def _callees(self, mod: _Module, cls: str | None, call: ast.Call) -> list[tuple[_Module, Func, str | None]]:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in mod.funcs:
                return [(mod, mod.funcs[func.id], None)]
            target, name = mod, func.id
            if func.id in mod.imports:
                module, name = mod.imports[func.id]
                target = self.modules.get(module)
                if target is None:
                    return []
                if name in target.funcs:
                    return [(target, target.funcs[name], None)]
            methods = target.classes.get(name, {})
            return [(target, methods[m], name) for m in ("__init__", "__call__", "__enter__") if m in methods]
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return []
        base = func.value.id
        if base in ("self", "cls") and cls:
            return [(mod, *found) for found in self._method(mod, cls, func.attr)]
        if base in mod.classes:
            return [(mod, *found) for found in self._method(mod, base, func.attr)]
        if base in mod.imports:
            module, name = mod.imports[base]
            target = self.modules.get(f"{module}.{name}")
            if target and func.attr in target.funcs:
                return [(target, target.funcs[func.attr], None)]
        return []

    def _method(self, mod: _Module, cls: str, attr: str) -> list[tuple[Func, str]]:
        seen: set[str] = set()
        stack = [cls]
        while stack:
            current = stack.pop()
            if current in seen or current not in mod.classes:
                continue
            seen.add(current)
            if attr in mod.classes[current]:
                return [(mod.classes[current][attr], current)]
            stack.extend(b.rsplit(".", 1)[-1] for b in mod.bases.get(current, []))
        return []

    def reaches_signal(self, mod: _Module, fn: Func, cls: str | None, stack: frozenset = frozenset()) -> bool:
        key = (mod.name, fn.lineno)
        if key in self._memo:
            return self._memo[key]
        if key in stack:
            return False
        found = any(_is_signal(n) for n in ast.walk(fn)) or any(
            self.reaches_signal(tm, tf, tc, stack | {key})
            for call in ast.walk(fn)
            if isinstance(call, ast.Call)
            for tm, tf, tc in self._callees(mod, cls, call)
        )
        self._memo[key] = found
        return found

    def _conftests(self, mod: _Module) -> list[_Module]:
        out = []
        directory = mod.path.parent
        while True:
            conftest = self.modules.get(".".join((directory / "conftest").relative_to(self.root.parent).parts))
            if conftest:
                out.append(conftest)
            if directory == self.root:
                return out
            directory = directory.parent

    def fixture_signal(self, mod: _Module, cls: str | None, params: list[str], seen: set[str] | None = None) -> bool:
        seen = set() if seen is None else seen
        for param in params:
            if param in seen:
                continue
            seen.add(param)
            candidates: list[tuple[_Module, Func, str | None]] = []
            class_methods = mod.classes.get(cls, {}) if cls else {}
            if param in class_methods and _is_fixture(class_methods[param]):
                candidates.append((mod, class_methods[param], cls))
            elif param in mod.fixtures:
                candidates.append((mod, mod.fixtures[param], None))
            else:
                candidates.extend((c, c.fixtures[param], None) for c in self._conftests(mod) if param in c.fixtures)
            for fm, ff, fc in candidates[:1]:  # the nearest definition is the one pytest uses
                if self.reaches_signal(fm, ff, fc) or self.fixture_signal(fm, fc, [a.arg for a in ff.args.args], seen):
                    return True
        return False


def _tests(mod: _Module):
    for name, fn in mod.funcs.items():
        if name.startswith("test_"):
            yield None, fn
    for cls, methods in mod.classes.items():
        if cls.startswith("Test"):
            for name, fn in methods.items():
                if name.startswith("test_"):
                    yield cls, fn


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _is_unconditional_skip_call(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    if _dotted(node.func) not in ("pytest.skip", "skip"):
        return False
    current = parents.get(node)
    while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module)):
        if isinstance(current, CONDITIONAL) or isinstance(current, ast.BoolOp):
            return False
        current = parents.get(current)
    return True


def _skip_violations(mod: _Module, where: str) -> list[str]:
    out = []
    parents = _parents(mod.tree)
    for node in ast.walk(mod.tree):
        line = f"{where}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute) and _dotted(node).endswith("mark.skip"):
            out.append(f"{line}: unconditional @pytest.mark.skip")
        elif isinstance(node, ast.Call) and _dotted(node.func).endswith("mark.skipif") and node.args:
            condition = node.args[0]
            if isinstance(condition, ast.Constant) and not isinstance(condition.value, str) and condition.value:
                out.append(f"{line}: skipif({condition.value!r}) never runs")
        elif isinstance(node, ast.Call) and _is_unconditional_skip_call(node, parents):
            out.append(f"{line}: unconditional pytest.skip()")
    return out


def scan(root: Path) -> list[str]:
    """Every hygiene violation under ``root`` (a ``tests``-shaped directory), as ``path:line: why``."""
    suite = _Suite(root)
    out: list[str] = []
    for mod in suite.modules.values():
        where = str(mod.path.relative_to(root.parent))
        out.extend(_skip_violations(mod, where))
        if not mod.path.name.startswith("test_"):
            continue
        for cls, fn in _tests(mod):
            if suite.reaches_signal(mod, fn, cls):
                continue
            if suite.fixture_signal(mod, cls, [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]):
                continue
            out.append(f"{where}:{fn.lineno}: {cls + '.' if cls else ''}{fn.name} asserts nothing")
    return sorted(out)


def test_no_dead_or_assertion_free_tests() -> None:
    violations = scan(TESTS)
    assert not violations, "Dead or assertion-free tests (fix, delete, or xfail(strict=True) with a ticket):\n" + "\n".join(
        violations
    )


PLANTED = """
import pytest
from tests.helpers import check_it

pytestmark = pytest.mark.skip(reason="module")


@pytest.mark.skipif(True, reason="never")
def test_literal_skipif():
    assert 1


@pytest.mark.skip
def test_marker_skip():
    assert 1


def test_imperative_skip():
    pytest.skip("not today")
    assert 1


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
        assert True


def test_conditional_skip_is_fine():
    try:
        import nothing_here  # noqa: F401
    except ImportError:
        pytest.skip("optional")
    assert 1


@pytest.mark.skipif("sys.platform == 'win32'", reason="string conditions are evaluated")
def test_via_imported_helper(fixture_that_asserts):
    check_it()


@pytest.fixture
def fixture_that_asserts():
    yield
    assert True


def test_raises_counts():
    with pytest.raises(ValueError):
        int("x")


def test_mock_assert_counts(mocker):
    mocker.assert_called_once()
"""

HELPERS = """
def check_it():
    assert True
"""


def test_scan_flags_each_planted_violation_and_nothing_else(tmp_path: Path) -> None:
    root = tmp_path / "tests"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "helpers.py").write_text(HELPERS)
    (root / "test_planted.py").write_text(PLANTED)

    found = {line.split(": ", 1)[1] for line in scan(root)}

    assert found == {
        "unconditional @pytest.mark.skip",
        "skipif(True) never runs",
        "unconditional pytest.skip()",
        "test_assert_free asserts nothing",
        "TestThings.test_method_assert_free asserts nothing",
    }
    assert sum("mark.skip" in line for line in scan(root)) == 2  # pytestmark and the decorator
