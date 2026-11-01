"""The latency-terrain harness (tests/performance/terrain_canary.py) keeps its schedule and its labels honest.

CI runs no doctests under tests/performance, so its doctests and its argument checks run here.
"""

from __future__ import annotations

import doctest
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import cachekit
from tests.performance import terrain_canary

HARNESS = "https://github.com/cachekit-io/cachekit-py/blob/0123abc/tests/performance/terrain_canary.py"
COMMIT = "1ef1d05eafa112d6cf5f73cce86fb7d2804ff119"  # pragma: allowlist secret (a commit id)
GIT = "/usr/bin/git"
SITE = "/site-packages"  # where an installed copy's files live
CHECKOUT = "/src/cachekit-py"
VCS = {"url": "https://github.com/cachekit-io/cachekit-py", "vcs_info": {"vcs": "git", "commit_id": COMMIT}}
EDITABLE = {"url": f"file://{CHECKOUT}", "dir_info": {"editable": True}}
FROM_SITE = f"{SITE}/cachekit/__init__.py"
FROM_CHECKOUT = f"{CHECKOUT}/src/cachekit/__init__.py"
SHADOW = "/work/other-checkout/src/cachekit/__init__.py"  # a checkout earlier on sys.path, PYTHONPATH say


class Installed:
    """An installed cachekit distribution: its PEP 610 direct_url.json holds `origin` (None: from an index),
    and its RECORD lists `files` under SITE (None: no RECORD)."""

    def __init__(self, origin: dict[str, Any] | None, files: tuple[str, ...] | None) -> None:
        self.text = None if origin is None else json.dumps(origin)
        self.files = None if files is None else [PurePosixPath(f) for f in files]

    def read_text(self, filename: str) -> str | None:
        return self.text if filename == "direct_url.json" else None

    def locate_file(self, path: PurePosixPath) -> Path:
        return Path(SITE) / path


def fake_git(status: str = "", fail: bool = False) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A subprocess.run that answers rev-parse with COMMIT[:8] and status with `status`, or fails."""

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv[1:3] == ["-C", CHECKOUT], argv
        if fail:
            raise subprocess.CalledProcessError(128, argv, stderr="fatal: not a git repository")
        if argv[3] == "rev-parse":
            return subprocess.CompletedProcess(argv, 0, stdout=COMMIT[:8] + "\n", stderr="")
        # Untracked files count, and only what the extension builds from: stray output elsewhere is not dirt.
        expected = ["status", "--porcelain", "--untracked-files=normal", "--", "src", "rust", "Cargo.toml", "Cargo.lock"]
        assert argv[3:] == expected, argv
        return subprocess.CompletedProcess(argv, 0, stdout=status, stderr="")

    return run


def install(
    monkeypatch: pytest.MonkeyPatch,
    origin: dict[str, Any] | None,
    imported: str,
    *,
    git: str | None = GIT,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    files: tuple[str, ...] | None = ("cachekit/__init__.py",),
) -> None:
    """Make cachekit look installed from `origin` and imported from the file `imported`."""
    monkeypatch.setattr(terrain_canary.importlib.metadata, "distribution", lambda name: Installed(origin, files))
    monkeypatch.setattr(cachekit, "__file__", imported)
    monkeypatch.setattr(terrain_canary.shutil, "which", lambda name: git)
    monkeypatch.setattr(terrain_canary.subprocess, "run", run or fake_git())


def test_doctests_pass() -> None:
    # next_arrival's examples guard the schedule: a revert to `due += gap` or `max(due, now) + gap`
    # fails them. The value sizes the recording states and the binning are checked the same way.
    with_examples = {t.name.rsplit(".", 1)[-1] for t in doctest.DocTestFinder().find(terrain_canary) if t.examples}
    assert {"next_arrival", "value_size_quantiles", "bin_of"} <= with_examples
    result = doctest.testmod(terrain_canary, optionflags=doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS)
    assert result.failed == 0


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "the following arguments are required: --l2-where"),
        (["--l2-where", "  "], "--l2-where must say where the Redis runs"),
    ],
)
def test_refuses_to_run_without_saying_where_the_redis_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    # Refused before any connection: the label is copy on a public page, and a localhost URL can
    # still reach a remote Redis through a tunnel, so the harness never guesses it.
    argv = ["--redis", "redis://127.0.0.1:6379/0", "--harness", HARNESS, "--out", str(tmp_path / "terrain.json"), *extra]
    with pytest.raises(SystemExit) as exc:
        terrain_canary.main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("origin", "imported", "status", "suffix"),
    [
        (None, FROM_SITE, "", ""),  # from a package index: the release its version names
        (VCS, FROM_SITE, "", "+1ef1d05e"),
        (EDITABLE, FROM_CHECKOUT, "", "+1ef1d05e"),
        (EDITABLE, FROM_CHECKOUT, " M src/cachekit/l1_cache.py\n", "+1ef1d05e.dirty"),
        # A new module the edited code imports before anyone adds it to git.
        (EDITABLE, FROM_CHECKOUT, "?? src/cachekit/hit_path.py\n", "+1ef1d05e.dirty"),
        (EDITABLE, FROM_CHECKOUT, " M Cargo.lock\n", "+1ef1d05e.dirty"),  # a local cachekit-core bump
    ],
)
def test_names_the_cachekit_it_measured(
    monkeypatch: pytest.MonkeyPatch, origin: dict[str, Any] | None, imported: str, status: str, suffix: str
) -> None:
    # A checkout reports the last release's version until the next release bumps it, so a recording
    # made from one names its commit; only an index install is the release its version names.
    install(monkeypatch, origin, imported, run=fake_git(status))
    assert terrain_canary.cachekit_label() == f"cachekit {cachekit.__version__}{suffix}"


@pytest.mark.parametrize(
    ("origin", "imported", "git", "fail"),
    [
        ({"url": "file:///tmp/cachekit-0.25.1-cp314-cp314-linux_x86_64.whl", "archive_info": {}}, FROM_SITE, GIT, False),
        # A plain directory install copied the tree when it was installed, so the checkout's HEAD
        # now may not be what runs.
        ({"url": f"file://{CHECKOUT}", "dir_info": {}}, FROM_CHECKOUT, GIT, False),
        (EDITABLE, FROM_CHECKOUT, GIT, True),  # not a git checkout
        (EDITABLE, FROM_CHECKOUT, None, False),  # no git on PATH
        # Something earlier on sys.path shadows the install the metadata describes.
        (None, SHADOW, GIT, False),
        (VCS, SHADOW, GIT, False),
        (EDITABLE, SHADOW, GIT, False),
    ],
)
def test_refuses_an_install_whose_commit_it_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, origin: dict[str, Any] | None, imported: str, git: str | None, fail: bool
) -> None:
    install(monkeypatch, origin, imported, git=git, run=fake_git(fail=fail))
    with pytest.raises(SystemExit, match="which commit it was built from is unknown"):
        terrain_canary.cachekit_label()


def test_refuses_an_installed_copy_without_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without its RECORD, an install cannot show that the imported package is one of its files.
    install(monkeypatch, None, FROM_SITE, files=None)
    with pytest.raises(SystemExit, match="which commit it was built from is unknown"):
        terrain_canary.cachekit_label()


def test_refuses_a_package_that_shadows_the_install(tmp_path: Path) -> None:
    # The real importer and metadata, no fakes: a cachekit package earlier on sys.path is what
    # imports, while importlib.metadata still finds the installed distribution.
    shadow = tmp_path / "cachekit"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(f'__version__ = "{cachekit.__version__}"\n')
    code = "import sys; sys.path.insert(0, sys.argv[1]); from tests.performance import terrain_canary; terrain_canary.cachekit_label()"
    proc = subprocess.run(  # noqa: S603 (trusted: sys.executable + literal code)
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "which commit it was built from is unknown" in proc.stderr
    assert f"imported from {shadow.resolve()}" in proc.stderr


def test_names_this_checkout_by_its_commit() -> None:
    # The real install, so a change in the direct_url.json an installer writes cannot go unnoticed.
    origin = terrain_canary.importlib.metadata.distribution("cachekit").read_text("direct_url.json")
    if origin is None or not json.loads(origin).get("dir_info", {}).get("editable"):
        pytest.skip("cachekit is not installed editable from a checkout here")
    assert re.fullmatch(rf"cachekit {re.escape(cachekit.__version__)}\+[0-9a-f]{{8}}(\.dirty)?", terrain_canary.cachekit_label())


@pytest.mark.parametrize(
    ("free_threaded", "gil_on", "label"),
    [(False, True, ""), (True, False, ", free-threaded, GIL off"), (True, True, ", free-threaded, GIL on")],
)
def test_names_a_free_threaded_build_and_whether_the_gil_was_on(
    monkeypatch: pytest.MonkeyPatch, free_threaded: bool, gil_on: bool, label: str
) -> None:
    # An import that does not declare free-threaded support turns the GIL back on, so a free-threaded
    # recording says which it measured.
    monkeypatch.setattr(
        terrain_canary.sysconfig, "get_config_var", lambda name: int(free_threaded) if name == "Py_GIL_DISABLED" else None
    )
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: gil_on, raising=False)
    assert terrain_canary.python_label() == f"Python {terrain_canary.platform.python_version()}{label}"


@pytest.mark.parametrize(("sysctl", "expected"), [("Apple M5 Pro\n", "Apple M5 Pro"), (None, "arm")])
def test_names_the_apple_chip_on_macos(monkeypatch: pytest.MonkeyPatch, sysctl: str | None, expected: str) -> None:
    # platform.processor() reports only "arm" on Apple silicon, so the host would read "arm, Darwin".
    class NoProc:
        def __init__(self, path: str) -> None:
            pass

        def read_text(self) -> str:
            raise FileNotFoundError("/proc/cpuinfo")

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]
        if sysctl is None:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, stdout=sysctl, stderr="")

    monkeypatch.setattr(terrain_canary, "Path", NoProc)
    monkeypatch.setattr(terrain_canary.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(terrain_canary.platform, "processor", lambda: "arm")
    monkeypatch.setattr(terrain_canary.subprocess, "run", run)
    assert terrain_canary.cpu_model() == expected
