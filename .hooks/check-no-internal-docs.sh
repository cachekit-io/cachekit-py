#!/bin/sh
# Block internal development artifacts from being committed to this public repo.
# Internal development files do not belong in this repository.
echo "BLOCKED - Internal development files must not be committed to this public repo"
echo "Files:"
for f in "$@"; do echo "  $f"; done
echo "Internal development files do not belong in this repository."
exit 1
