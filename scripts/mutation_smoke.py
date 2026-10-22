"""Mutation smoke: re-apply hand-written mutants and check that their recorded killer tests still fail.

Each mutant is one exact string edit to one source file, plus the tests that must catch it. A mutant
is killed only when pytest exits 1 and at least one of its killers is among the failures, so a
collection error, a setup or teardown error, a timeout or an unrelated failure never counts as a kill. The ``baseline`` run
checks that every anchor matches exactly once and that every killer passes on the unmutated tree.

    uv run python scripts/mutation_smoke.py list
    uv run python scripts/mutation_smoke.py run baseline
    uv run python scripts/mutation_smoke.py run checksum-skipped

Exit status: 0 killed (or baseline green), 1 survived (or baseline red), 2 a mutant could not be applied.
"""

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTEST_TIMEOUT_S = 20 * 60
PYTEST_EXIT_TESTS_FAILED = 1


@dataclass(frozen=True)
class Mutant:
    name: str
    path: str
    anchor: str
    replacement: str
    killers: tuple[str, ...]  # pytest node ids; at least one must fail on the mutant


A = "src/cachekit/serializers/arrow_serializer.py"
H = "src/cachekit/cache_handler.py"
F = "src/cachekit/backends/file/backend.py"

MUTANTS = (
    Mutant(
        "checksum-skipped", A,
        "            if xxhash.xxh3_64_digest(body) != expected_checksum:\n                raise SerializationError",
        "            if False:\n                raise SerializationError",
        (
            "tests/unit/test_arrow_serializer.py::TestIntegrityAlwaysOn::test_corruption_detected_with_integrity_on",
            "tests/unit/test_serializer_integrity.py::TestArrowSerializerIntegrity::test_corrupted_arrow_data_detected",
        ),
    ),
    Mutant(
        "polars-returns-pandas", A,
        "return pl.from_arrow(table, rechunk=not _views_of(table, source))",
        "return table.to_pandas()",
        (
            "tests/unit/test_arrow_serializer.py::TestReturnFormatVariants::test_return_format_polars",
            "tests/unit/test_arrow_serializer.py::TestPolarsSupport::test_polars_dataframe_round_trip",
        ),
    ),
    Mutant(
        "arrow-returns-pandas", A,
        "return table  # no conversion; aliases ``body`` when the IPC is uncompressed",
        "return table.to_pandas()",
        (
            "tests/unit/test_arrow_serializer.py::TestReturnFormatVariants::test_return_format_arrow",
            "tests/unit/test_serializer_integrity.py::TestArrowSerializerIntegrity::test_arrow_return_format_integrity",
        ),
    ),
    Mutant(
        "ipc-single-unbounded-batch", A,
        "            with pa.ipc.new_file(sink, table.schema, options=write_options) as writer:\n"
        "                _write_bounded_batches(writer, table)",
        "            with pa.ipc.new_file(sink, table.schema, options=write_options) as writer:\n"
        "                writer.write_table(table)",
        (
            "tests/unit/test_arrow_serializer.py::TestBoundedBatching::test_skewed_frame_batches_stay_bounded",
            "tests/unit/test_arrow_serializer.py::TestBoundedBatching::test_uniform_frame_batching_not_fragmented",
        ),
    ),
    Mutant(
        "numpy-result-aliases-cache", "src/cachekit/serializers/auto_serializer.py",
        "arr = np.frombuffer(raw_bytes, dtype=_dtype_from_untrusted(dtype_str)).copy()",
        "arr = np.frombuffer(raw_bytes, dtype=_dtype_from_untrusted(dtype_str))",
        (
            "tests/unit/test_auto_serializer_mutation_and_corruption.py::TestDeserializedArraysAreWritable::test_top_level_numpy_writable_and_unaliased",
        ),
    ),
    Mutant(
        "mmap-read-disabled", H,
        "    def supports_mmap_read(self) -> bool:",
        "    def supports_mmap_read(self) -> bool:\n        return False",
        (
            "tests/unit/test_mmap_read_path.py::TestMmapReadEndToEnd::test_arrow_dataframe_roundtrips_through_real_mmap",
            "tests/unit/test_mmap_read_path.py::TestSupportsMmapRead::test_arrow_serializer_instance_is_eligible",
        ),
    ),
    Mutant(
        "streaming-write-disabled", H,
        "    def supports_streaming_write(self) -> bool:",
        "    def supports_streaming_write(self) -> bool:\n        return False",
        (
            "tests/unit/test_streaming_write_path.py::TestSupportsStreamingWrite::test_arrow_plaintext_is_eligible",
            "tests/unit/test_streaming_write_path.py::TestStoreResultRouting::test_streams_to_file_backend_and_skips_l1",
        ),
    ),
    Mutant(
        "mmap-read-when-encrypted", H,
        "        if self.encryption or self._serializer_string_name not in _ARROW_FRAME_TAGS",
        "        if self._serializer_string_name not in _ARROW_FRAME_TAGS",
        (
            "tests/unit/test_mmap_read_path.py::TestSupportsMmapRead::test_encrypted_arrow_not_eligible",
            "tests/unit/test_decrypt_fail_policy.py::TestArrowPostDecryptContainer::test_handler_read_of_sealed_bare_arrow_is_miss_and_evicted",
        ),
    ),
    Mutant(
        "get-buffer-serves-expired", F,
        "                    if expiry_timestamp > 0 and time.time() >= expiry_timestamp:\n"
        "                        self._safe_unlink_if_same_inode(fd, file_path)\n"
        "                        return None\n\n"
        "                    # Empty payload",
        "                    if False:\n"
        "                        self._safe_unlink_if_same_inode(fd, file_path)\n"
        "                        return None\n\n"
        "                    # Empty payload",
        (
            "tests/unit/backends/test_file_backend.py::TestMmapBuffer::test_get_buffer_expired_returns_none",
            "tests/unit/protocol/test_file_backend_vectors.py::test_miss_expired[get_buffer-None]",
        ),
    ),
    Mutant(
        "get-buffer-follows-symlinks", F,
        "                fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)\n            except FileNotFoundError:",
        "                fd = os.open(file_path, os.O_RDONLY)\n            except FileNotFoundError:",
        (
            "tests/unit/backends/test_file_backend.py::TestErrorPaths::test_get_buffer_refuses_symlink_to_valid_cache_file",
        ),
    ),
    Mutant(
        "zero-config-l1-off", "src/cachekit/decorators/wrapper.py",
        "_zero_config_pending = backend is None and not _l1_only_mode and interop is None"
        " and not serialization_handler.encryption",
        "_zero_config_pending = False",
        (
            "tests/unit/test_zero_config_l1.py::TestZeroConfigCaches::test_sync_preset_runs_once[bare]",
            "tests/critical/test_zero_config_fresh_interpreter.py::test_bare_cache_in_process_with_nothing_configured",
        ),
    ),
    Mutant(
        "serializer-name-mismatch-accepted", H,
        "            if serializer_name != self._serializer_string_name:\n                raise SerializationError(",
        "            if False:\n                raise SerializationError(",
        (
            "tests/unit/protocol/test_python_frame_vectors.py::TestSerializerNameMisses::test_planted_frame_is_a_miss[default_reader-auto_serializer_write]",
            "tests/unit/test_nameless_frame_guard.py::test_nameless_entry_is_miss_and_evict[v3-frame-unknown]",
        ),
    ),
)  # fmt: skip

BY_NAME = {m.name: m for m in MUTANTS}


class NotAppliedError(Exception):
    """The mutant's anchor does not match exactly once, so the mutant cannot be applied."""


def mutated_source(mutant: Mutant, source: str) -> str:
    count = source.count(mutant.anchor)
    if count != 1:
        raise NotAppliedError(f"{mutant.name}: anchor matches {count} times in {mutant.path}, expected exactly 1")
    return source.replace(mutant.anchor, mutant.replacement)


def killer_files(killers: tuple[str, ...]) -> list[str]:
    return sorted({k.split("::", 1)[0] for k in killers})


def junit_key(node_id: str) -> tuple[str, str]:
    """Map a pytest node id to junit's (classname, name): tests/a/b.py::C::t -> ("tests.a.b.C", "t")."""
    file, *scopes, name = node_id.split("::")
    return ".".join([file.removesuffix(".py").replace("/", "."), *scopes]), name


def junit_outcomes(xml_path: Path) -> dict[tuple[str, str], str]:
    """Outcome per test case: "failed", "error" (setup or teardown, never a kill), "skipped" or "passed"."""
    outcomes = {}
    for case in ET.parse(xml_path).iter("testcase"):  # noqa: S314 (trusted: junit this script just had pytest write)
        tags = {child.tag for child in case}
        if "failure" in tags:
            outcome = "failed"
        elif "error" in tags:
            outcome = "error"
        else:
            outcome = "skipped" if "skipped" in tags else "passed"
        outcomes[(case.get("classname", ""), case.get("name", ""))] = outcome
    return outcomes


def run_pytest(files: list[str]) -> tuple[int | None, dict[tuple[str, str], str]]:
    """Run pytest on files with junit output. Returns (exit code or None on timeout, outcomes)."""
    # A short, unique base temp: a locally spawned Redis listens on a unix socket under it, and a
    # long TMPDIR pushes that path past the ~107-character socket limit (the Makefile does the same).
    with tempfile.TemporaryDirectory(prefix="mutation-smoke-", dir="/tmp") as tmp:
        xml_path = Path(tmp) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
        cmd += ["-o", "junit_family=xunit2", f"--junitxml={xml_path}", f"--basetemp={tmp}/pt", *files]
        print("+", " ".join(cmd), flush=True)
        try:
            rc = subprocess.run(cmd, cwd=ROOT, timeout=PYTEST_TIMEOUT_S).returncode  # noqa: S603 (trusted: literal argv)
        except subprocess.TimeoutExpired:
            return None, {}
        return rc, junit_outcomes(xml_path) if xml_path.exists() else {}


def verdict(mutant: Mutant, rc: int | None, outcomes: dict[tuple[str, str], str]) -> tuple[bool, str]:
    """(killed, explanation). Killed only on exit 1 with at least one recorded killer failing."""
    if rc != PYTEST_EXIT_TESTS_FAILED:
        why = "timed out" if rc is None else f"pytest exited {rc}"
        return False, f"SURVIVED {mutant.name}: {why}, which is not a test failure"
    failed = [k for k in mutant.killers if outcomes.get(junit_key(k)) == "failed"]
    if not failed:
        return False, f"SURVIVED {mutant.name}: tests failed, but none of its killers: {', '.join(mutant.killers)}"
    return True, f"KILLED {mutant.name} by {', '.join(failed)}"


def run_mutant(mutant: Mutant) -> int:
    path = ROOT / mutant.path
    original = path.read_text()
    path.write_text(mutated_source(mutant, original))
    try:
        rc, outcomes = run_pytest(killer_files(mutant.killers))
    finally:
        path.write_text(original)
    killed, message = verdict(mutant, rc, outcomes)
    print(message)
    return 0 if killed else 1


def run_baseline() -> int:
    """Every anchor applies once, and every killer passes on the unmutated tree."""
    problems = []
    for mutant in MUTANTS:
        try:
            mutated_source(mutant, (ROOT / mutant.path).read_text())
        except NotAppliedError as exc:
            problems.append(str(exc))
    killers = sorted({k for m in MUTANTS for k in m.killers})
    rc, outcomes = run_pytest(killer_files(tuple(killers)))
    if rc != 0:
        problems.append("baseline timed out" if rc is None else f"baseline pytest exited {rc}")
    for k in killers:
        outcome = outcomes.get(junit_key(k), "not run")
        if outcome != "passed":
            problems.append(f"killer {k} did not pass on the baseline: {outcome}")
    for problem in problems:
        print(f"BASELINE: {problem}")
    print("BASELINE RED" if problems else f"BASELINE GREEN: {len(killers)} killers pass, {len(MUTANTS)} anchors apply")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print the mutant names")
    run = sub.add_parser("run", help="run the baseline or one mutant")
    run.add_argument("name", choices=["baseline", *BY_NAME])
    args = parser.parse_args(argv)

    if args.command == "list":
        print("\n".join(BY_NAME))
        return 0
    if args.name == "baseline":
        return run_baseline()
    try:
        return run_mutant(BY_NAME[args.name])
    except NotAppliedError as exc:
        print(f"NOT APPLIED {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
