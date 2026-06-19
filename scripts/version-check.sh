#!/usr/bin/env bash
# Check version consistency between pyproject.toml and rust/Cargo.toml.
# Extracted verbatim from the Makefile `version-check` target.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

echo "${BLUE}Checking version consistency...${RESET}"
PYTHON_VERSION=$(grep -E "^version = " pyproject.toml | head -1 | cut -d'"' -f2)
RUST_VERSION=$(grep -E "^version = " rust/Cargo.toml | head -1 | cut -d'"' -f2)
echo "  Python version: $PYTHON_VERSION"
echo "  Rust version:   $RUST_VERSION"
if [ "$PYTHON_VERSION" != "$RUST_VERSION" ]; then
	echo "${YELLOW}❌ Version mismatch! Python and Rust versions must match.${RESET}"
	exit 1
else
	echo "${GREEN}✓ Versions match${RESET}"
fi
