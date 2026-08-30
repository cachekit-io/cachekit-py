#!/usr/bin/env bash
# Run the Atheris (Python) arm of fuzz-quick: 10 min per fuzz target.
# Extracted verbatim from the Python/Atheris arm of the Makefile `fuzz-quick`
# target. The Rust arm (`make -C rust/fuzz quick`) stays in the Makefile.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if command -v python &>/dev/null && python -c "import atheris" 2>/dev/null; then
	echo "${BLUE}Running Atheris fuzzing...${RESET}"
	# Same exit-code contract as the atheris-fuzzing job in security-deep.yml
	# (LAB-1140): no `|| true` — a crash, import error, or hang (timeout exit
	# 124; budget exhaustion exits 0 well before 15 min) fails the run, and
	# reproducers land in tests/fuzzing/artifacts/ (gitignored), which only
	# ever holds crash artifacts since no corpus dir is passed.
	mkdir -p tests/fuzzing/artifacts
	shopt -s nullglob
	targets=(tests/fuzzing/fuzz_*.py)
	if [ "${#targets[@]}" -eq 0 ]; then
		echo "${YELLOW}no Atheris targets matched tests/fuzzing/fuzz_*.py — refusing to pass having fuzzed nothing${RESET}" >&2
		exit 1
	fi
	for fuzz_target in "${targets[@]}"; do
		echo "${YELLOW}Fuzzing $fuzz_target...${RESET}"
		timeout 15m uv run python "$fuzz_target" -max_total_time=600 -artifact_prefix=tests/fuzzing/artifacts/
	done
else
	echo "${YELLOW}⚠️  Atheris not available (macOS limitation - libFuzzer not in Apple Clang)${RESET}"
	echo "${YELLOW}   Atheris fuzzing will run in CI on Linux${RESET}"
fi
