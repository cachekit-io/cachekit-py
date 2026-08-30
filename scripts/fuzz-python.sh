#!/usr/bin/env bash
# Run the Atheris (Python) arm of fuzz-quick: 10 min per fuzz target.
# Extracted verbatim from the Python/Atheris arm of the Makefile `fuzz-quick`
# target. The Rust arm (`make -C rust/fuzz quick`) stays in the Makefile.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# macOS is the ONLY soft skip (Apple Clang ships no libFuzzer, so atheris
# cannot work there). Everywhere else the targets run unconditionally: if
# atheris is missing, the target's own import fails loudly and reds the run —
# probing for it first and skipping (the old behavior) was a silent green on
# Linux, the exact lie this contract exists to kill (LAB-1140).
if [ "$(uname -s)" = "Darwin" ]; then
	echo "${YELLOW}⚠️  Skipping Atheris fuzzing (macOS limitation - libFuzzer not in Apple Clang)${RESET}"
	echo "${YELLOW}   Atheris fuzzing runs in CI on Linux${RESET}"
	exit 0
fi

echo "${BLUE}Running Atheris fuzzing...${RESET}"
# Same exit-code contract as the atheris-fuzzing job in security-deep.yml
# (LAB-1140) — keep budgets/flags in sync with it: no `|| true`; a crash,
# import error, or hang fails the run (libFuzzer's -timeout=60 per-input
# watchdog writes a timeout-* reproducer; `timeout -k 30s 15m` is the
# backstop for native hangs, since libFuzzer traps SIGTERM; budget exhaustion
# exits 0 well before 15 min). Reproducers land in tests/fuzzing/artifacts/
# (gitignored), which only ever holds crash artifacts — no corpus dir is
# passed.
mkdir -p tests/fuzzing/artifacts
shopt -s nullglob
targets=(tests/fuzzing/fuzz_*.py)
if [ "${#targets[@]}" -eq 0 ]; then
	echo "${YELLOW}no Atheris targets matched tests/fuzzing/fuzz_*.py — refusing to pass having fuzzed nothing${RESET}" >&2
	exit 1
fi
for fuzz_target in "${targets[@]}"; do
	echo "${YELLOW}Fuzzing $fuzz_target...${RESET}"
	timeout -k 30s 15m uv run python "$fuzz_target" -max_total_time=600 -timeout=60 -artifact_prefix=tests/fuzzing/artifacts/
done
