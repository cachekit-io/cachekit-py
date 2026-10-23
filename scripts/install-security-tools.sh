#!/usr/bin/env bash
# Install all Rust security tools (cargo-audit, deny, geiger, etc.) idempotently.
# Extracted verbatim from the Makefile `security-install` target.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

echo "${BLUE}Installing security tools...${RESET}"
command -v cargo >/dev/null 2>&1 || { echo "${YELLOW}❌ cargo not found. Install Rust: https://rustup.rs${RESET}"; exit 1; }

echo "${YELLOW}Installing cargo-audit...${RESET}"
if command -v cargo-audit >/dev/null 2>&1; then
	echo "  ${GREEN}✓ cargo-audit already installed${RESET}"
else
	cargo install --locked cargo-audit && echo "  ${GREEN}✓ cargo-audit installed${RESET}"
fi

echo "${YELLOW}Installing cargo-deny...${RESET}"
if command -v cargo-deny >/dev/null 2>&1; then
	echo "  ${GREEN}✓ cargo-deny already installed${RESET}"
else
	cargo install --locked cargo-deny && echo "  ${GREEN}✓ cargo-deny installed${RESET}"
fi

echo "${YELLOW}Installing cargo-geiger...${RESET}"
if command -v cargo-geiger >/dev/null 2>&1; then
	echo "  ${GREEN}✓ cargo-geiger already installed${RESET}"
else
	cargo install --locked cargo-geiger && echo "  ${GREEN}✓ cargo-geiger installed${RESET}"
fi

echo "${YELLOW}Installing cargo-machete...${RESET}"
if command -v cargo-machete >/dev/null 2>&1; then
	echo "  ${GREEN}✓ cargo-machete already installed${RESET}"
else
	cargo install --locked cargo-machete && echo "  ${GREEN}✓ cargo-machete installed${RESET}"
fi

echo "${YELLOW}Installing kani-verifier...${RESET}"
if command -v cargo-kani >/dev/null 2>&1; then
	echo "  ${GREEN}✓ kani-verifier already installed${RESET}"
else
	cargo install --locked kani-verifier && cargo kani setup && echo "  ${GREEN}✓ kani-verifier installed${RESET}"
fi

echo "${YELLOW}Installing cargo-fuzz...${RESET}"
if command -v cargo-fuzz >/dev/null 2>&1; then
	echo "  ${GREEN}✓ cargo-fuzz already installed${RESET}"
else
	cargo install --locked cargo-fuzz && echo "  ${GREEN}✓ cargo-fuzz installed${RESET}"
fi

echo "${YELLOW}Installing cargo-sbom...${RESET}"
if command -v cargo-sbom >/dev/null 2>&1; then
	echo "  ${GREEN}✓ cargo-sbom already installed${RESET}"
else
	cargo install --locked cargo-sbom && echo "  ${GREEN}✓ cargo-sbom installed${RESET}"
fi

echo "${YELLOW}Installing nightly toolchain with Miri...${RESET}"
if rustup toolchain list | grep -q nightly; then
	echo "  ${GREEN}✓ nightly toolchain already installed${RESET}"
	rustup component add miri --toolchain nightly 2>/dev/null || echo "  ${GREEN}✓ miri already installed${RESET}"
else
	rustup toolchain install nightly --component miri && echo "  ${GREEN}✓ nightly + miri installed${RESET}"
fi

echo "${GREEN}✓ All security tools installed${RESET}"
