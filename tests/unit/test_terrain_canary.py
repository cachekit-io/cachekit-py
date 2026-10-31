"""The latency-terrain harness (tests/performance/terrain_canary.py) keeps its schedule and its labels honest.

CI runs no doctests under tests/performance, so its doctests and its argument checks run here.
"""

from __future__ import annotations

import argparse
import doctest
from pathlib import Path
from typing import NoReturn

import pytest

from tests.performance import terrain_canary

HARNESS = "https://github.com/cachekit-io/cachekit-py/blob/0123abc/tests/performance/terrain_canary.py"


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


def test_records_the_l2_label_without_its_padding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The label prints beside the figures as given, so padding from a shell quote must not reach it.
    seen: list[str] = []

    class StoppedBeforeRunError(Exception):
        pass

    def stop(args: argparse.Namespace) -> NoReturn:
        seen.append(args.l2_where)
        raise StoppedBeforeRunError

    monkeypatch.setattr(terrain_canary, "run", stop)
    argv = ["--redis", "redis://127.0.0.1:6379/0", "--harness", HARNESS, "--out", str(tmp_path / "terrain.json")]
    with pytest.raises(StoppedBeforeRunError):
        terrain_canary.main([*argv, "--l2-where", "  same host, loopback TCP "])
    assert seen == ["same host, loopback TCP"]
