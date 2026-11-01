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
from pathlib import Path
from typing import Any

import pytest

import cachekit
from tests.performance import terrain_canary

HARNESS = "https://github.com/cachekit-io/cachekit-py/blob/0123abc/tests/performance/terrain_canary.py"
COMMIT = "1ef1d05eafa112d6cf5f73cce86fb7d2804ff119"  # pragma: allowlist secret (a commit id)


class Installed:
    """An installed cachekit distribution whose PEP 610 direct_url.json holds `origin` (None: from an index)."""

    def __init__(self, origin: dict[str, Any] | None) -> None:
        self.text = None if origin is None else json.dumps(origin)

    def read_text(self, filename: str) -> str | None:
        return self.text if filename == "direct_url.json" else None


def fake_git(status: str = "", fail: bool = False) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A subprocess.run that answers rev-parse with COMMIT[:8] and status with `status`, or fails."""

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv[1:3] == ["-C", "/src/cachekit-py"], argv
        if fail:
            raise subprocess.CalledProcessError(128, argv, stderr="fatal: not a git repository")
        if argv[3] == "rev-parse":
            return subprocess.CompletedProcess(argv, 0, stdout=COMMIT[:8] + "\n", stderr="")
        # Untracked files count, and only the package's own sources: stray output elsewhere is not dirt.
        assert argv[3:] == ["status", "--porcelain", "--untracked-files=normal", "--", "src", "rust"], argv
        return subprocess.CompletedProcess(argv, 0, stdout=status, stderr="")

    return run


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
    ("origin", "status", "suffix"),
    [
        (None, "", ""),  # from a package index: the release its version names
        (
            {"url": "https://github.com/cachekit-io/cachekit-py", "vcs_info": {"vcs": "git", "commit_id": COMMIT}},
            "",
            "+1ef1d05e",
        ),
        ({"url": "file:///src/cachekit-py", "dir_info": {"editable": True}}, "", "+1ef1d05e"),
        ({"url": "file:///src/cachekit-py", "dir_info": {"editable": True}}, " M src/cachekit/l1_cache.py\n", "+1ef1d05e.dirty"),
        # A new module the edited code imports before anyone adds it to git.
        ({"url": "file:///src/cachekit-py", "dir_info": {"editable": True}}, "?? src/cachekit/hit_path.py\n", "+1ef1d05e.dirty"),
    ],
)
def test_names_the_cachekit_it_measured(
    monkeypatch: pytest.MonkeyPatch, origin: dict[str, Any] | None, status: str, suffix: str
) -> None:
    # A checkout reports the last release's version until the next release bumps it, so a recording
    # made from one names its commit; only an index install is the release its version names.
    monkeypatch.setattr(terrain_canary.importlib.metadata, "distribution", lambda name: Installed(origin))
    monkeypatch.setattr(terrain_canary.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(terrain_canary.subprocess, "run", fake_git(status))
    assert terrain_canary.cachekit_label() == f"cachekit {cachekit.__version__}{suffix}"


@pytest.mark.parametrize(
    ("origin", "git", "fail"),
    [
        ({"url": "file:///tmp/cachekit-0.25.1-cp314-cp314-linux_x86_64.whl", "archive_info": {}}, "/usr/bin/git", False),
        # A plain directory install copied the tree when it was installed, so the checkout's HEAD
        # now may not be what runs.
        ({"url": "file:///src/cachekit-py", "dir_info": {}}, "/usr/bin/git", False),
        ({"url": "file:///src/cachekit-py", "dir_info": {"editable": True}}, "/usr/bin/git", True),  # not a git checkout
        ({"url": "file:///src/cachekit-py", "dir_info": {"editable": True}}, None, False),  # no git on PATH
    ],
)
def test_refuses_an_install_whose_commit_it_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, origin: dict[str, Any], git: str | None, fail: bool
) -> None:
    monkeypatch.setattr(terrain_canary.importlib.metadata, "distribution", lambda name: Installed(origin))
    monkeypatch.setattr(terrain_canary.shutil, "which", lambda name: git)
    monkeypatch.setattr(terrain_canary.subprocess, "run", fake_git(fail=fail))
    with pytest.raises(SystemExit, match="which commit it was built from is unknown"):
        terrain_canary.cachekit_label()


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
