#!/usr/bin/env bash
# Shared helpers for Makefile-extracted scripts.
#
# ANSI color codes mirror the Makefile's printf-based color variables
# (BLUE/GREEN/YELLOW/RESET) so scripts preserve the same colored UX when
# invoked via `make`. Sourced — do NOT set -e here; the sourcing script owns
# its own shell options.
BLUE=$'\033[36m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RESET=$'\033[0m'
