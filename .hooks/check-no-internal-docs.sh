#!/bin/sh
# Block internal development artifacts from being committed to this public repo.
# Matched files belong in tooling/, strategy/, or MCP memory — not here.
echo "BLOCKED - Internal development files must not be committed to this public repo"
echo "Files:"
for f in "$@"; do echo "  $f"; done
echo "Move to tooling/, strategy/, or MCP memory instead."
exit 1
