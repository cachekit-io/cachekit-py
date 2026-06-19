#!/usr/bin/env bash
# Run the Atheris (Python) arm of fuzz-quick: 10 min per fuzz target.
# Extracted verbatim from the Python/Atheris arm of the Makefile `fuzz-quick`
# target. The Rust arm (`make -C rust/fuzz quick`) stays in the Makefile.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if command -v python &>/dev/null && python -c "import atheris" 2>/dev/null; then
	echo "${BLUE}Running Atheris fuzzing...${RESET}"
	for fuzz_target in tests/fuzzing/fuzz_*.py; do
		if [ -f "$fuzz_target" ]; then
			echo "${YELLOW}Fuzzing $fuzz_target...${RESET}"
			timeout 10m uv run python "$fuzz_target" -max_total_time=600 || true
		fi
	done
else
	echo "${YELLOW}⚠️  Atheris not available (macOS limitation - libFuzzer not in Apple Clang)${RESET}"
	echo "${YELLOW}   Atheris fuzzing will run in CI on Linux${RESET}"
fi
