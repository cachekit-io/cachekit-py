"""The latency-terrain harness (tests/performance/terrain_canary.py) keeps its schedule and its labels honest.

CI runs no doctests under tests/performance, so its doctests and its argument checks run here.
"""

from __future__ import annotations

import doctest
from pathlib import Path

import pytest

from tests.performance import terrain_canary

HARNESS = "https://github.com/cachekit-io/cachekit-py/blob/0123abc/tests/performance/terrain_canary.py"


def test_doctests_pass() -> None:
    # Covers next_arrival's schedule (a revert to `due += gap` or `max(due, now) + gap` fails it),
    # the loopback rule for --l2-where, the value sizes the recording states, and the binning.
    result = doctest.testmod(terrain_canary, optionflags=doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS)
    assert result.attempted > 0
    assert result.failed == 0


def test_refuses_a_redis_elsewhere_without_saying_where(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Refused before any connection: the label it would have guessed is copy on a public page.
    argv = ["--redis", "redis://db.example:6379/0", "--harness", HARNESS, "--out", str(tmp_path / "terrain.json")]
    with pytest.raises(SystemExit) as exc:
        terrain_canary.main(argv)
    assert exc.value.code == 2
    assert "--l2-where is required" in capsys.readouterr().err
