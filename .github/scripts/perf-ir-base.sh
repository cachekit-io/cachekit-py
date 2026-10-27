#!/usr/bin/env bash
# Pick the commit the instruction-budget gate measures a pull request against, and
# write it to $GITHUB_OUTPUT as `sha` (what to build) and `label` (how the comment
# names it). Run from a checkout of the PR merge commit with its first parent.
#
# A feature PR is measured against its base, the merge commit's first parent. A
# release PR (head branch release-please--*) is rebased onto main on every push,
# so against main it would measure only a version bump. It is measured against
# the last release instead: tag v<version>, where <version> is the "." entry of
# the base's .release-please-manifest.json. The comment then shows every path's
# drift since that release. A tag that cannot be fetched fails the job; it never
# falls back to main, which would read PASS without measuring anything.
#
#   CI:    HEAD_REF=${{ github.head_ref }} bash .github/scripts/perf-ir-base.sh
#   Local: HEAD_REF=release-please--x GITHUB_OUTPUT=/dev/stdout bash .github/scripts/perf-ir-base.sh
set -euo pipefail

if [[ ${HEAD_REF:-} != release-please--* ]]; then
  sha=$(git rev-parse HEAD^1)
  label=$sha
else
  version=$(git show HEAD^1:.release-please-manifest.json | jq -er '."."') || {
    echo "::error::release PR: no \".\" version in the base's .release-please-manifest.json"
    exit 1
  }
  tag=v$version
  git fetch --quiet --no-tags --depth=1 origin "refs/tags/$tag:refs/tags/$tag" || {
    echo "::error::release PR: cannot fetch the last release tag $tag"
    exit 1
  }
  sha=$(git rev-parse "$tag^{commit}")
  label="$tag ($(git rev-parse --short=7 "$sha")), last release"
fi
echo "Base: $label"
{
  echo "sha=$sha"
  echo "label=$label"
} >> "$GITHUB_OUTPUT"
