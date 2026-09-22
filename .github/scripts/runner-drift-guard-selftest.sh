#!/usr/bin/env bash
# Regression fixtures for the runner drift guard (the awk program in the
# runner-drift-guard job of .github/workflows/ci.yml). Every `bad` fixture must
# exit 1 with a ::error line; every `good` fixture must exit 0. Structural
# `bad` fixtures carry only hosted labels, so nothing but the shape under test
# can fail them. Add the shape here when you close a hole.
#
#   CI:     bash .github/scripts/runner-drift-guard-selftest.sh "$RUNNER_TEMP/runner-drift-guard.awk"
#   Local:  bash .github/scripts/runner-drift-guard-selftest.sh   (extracts the program from ci.yml)
set -u
cd "$(dirname "$0")/../.." || exit 1
tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT
prog=${1:-}
if [ -z "$prog" ]; then
  prog=$tmp/runner-drift-guard.awk
  sed -n "/<<'AWK'\$/,/^ *AWK\$/p" .github/workflows/ci.yml | sed '1d;$d' > "$prog"
fi
[ -s "$prog" ] || { echo "::error::guard program not found: ${prog}"; exit 1; }
fail=0

check() { # kind name body
  printf '%s\n' "$3" > "$tmp/$2.yml"
  out=$(awk -f "$prog" "$tmp/$2.yml" 2>&1); rc=$?
  case "$1" in
    bad)  [ "$rc" -eq 1 ] && grep -q '^::error' <<<"$out" && return ;;
    good) [ "$rc" -eq 0 ] && return ;;
  esac
  printf '::error::selftest %s %s: exit %s\n%s\n' "$1" "$2" "$rc" "$out"
  fail=1
}
bad()  { check bad  "$@"; }
good() { check good "$@"; }

# --- label policy ----------------------------------------------------------
bad pool-label            $'jobs:\n  j:\n    runs-on: self-hosted-pool'
bad hosted-prefix-pool    $'jobs:\n  j:\n    runs-on: ubuntu-pool'
bad unlisted-hosted-arm   $'jobs:\n  j:\n    runs-on: ubuntu-24.04-arm'
bad self-hosted-list      $'jobs:\n  j:\n    runs-on: [self-hosted, linux]'
bad vars-indirection      $'jobs:\n  j:\n    runs-on: ${{ vars.RUNNER }}'
bad matrix-os-pool        $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [ubuntu-latest, pool-xl]'
bad matrix-include-item   $'jobs:\n  j:\n    strategy:\n      matrix:\n        include:\n          - os: self-hosted-pool\n            rust: stable'
bad same-indent-include   $'jobs:\n  j:\n    strategy:\n      matrix:\n        include:\n        - os: self-hosted-pool'
bad quoted-key            $'jobs:\n  j:\n    "runs-on": self-hosted-pool'
bad quoted-key-os         $'jobs:\n  j:\n    strategy:\n      matrix:\n        \'os\': [self-hosted-pool]'
bad upper-key             $'jobs:\n  j:\n    strategy:\n      matrix:\n        OS: [self-hosted-pool]\n    runs-on: ${{ matrix.os }}'
# --- values not written inline (hosted labels: only the shape fails) --------
bad block-form            $'jobs:\n  j:\n    runs-on:\n      group: some-group\n      labels: [ubuntu-latest]'
bad unclosed-list         $'jobs:\n  j:\n    strategy:\n      matrix:\n        os: [ubuntu-latest\n          , macos-latest]'
bad block-sequence-os     $'jobs:\n  j:\n    strategy:\n      matrix:\n        os:\n          - ubuntu-latest'
# --- lines that may hide a runner label (hosted labels: only the shape fails)
bad flow-style-job        'jobs: {j: {runs-on: ubuntu-latest}}'
bad flow-strategy         $'jobs:\n  j:\n    strategy: {fail-fast: false, matrix: {os: [ubuntu-latest]}}'
bad flow-multiline        $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy: {matrix: {\n      os: [ubuntu-latest]}}'
bad flow-env-os           $'jobs:\n  j:\n    runs-on: ubuntu-latest\n    env: {foo: bar, os: linux}'
bad dynamic-matrix        $'jobs:\n  j:\n    strategy:\n      matrix: ${{ fromJSON(needs.p.outputs.m) }}\n    runs-on: ${{ matrix.os }}'
bad dynamic-include       $'jobs:\n  j:\n    strategy:\n      matrix:\n        os: [ubuntu-latest]\n        include: ${{ fromJSON(vars.EXTRA) }}\n    runs-on: ${{ matrix.os }}'
bad matrix-anchor-def     $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix: &shared\n        os: [ubuntu-latest]'
bad matrix-alias          $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix: *shared'
bad matrix-tag            $'jobs:\n  j:\n    strategy:\n      matrix: !!map\n        os: [ubuntu-latest]'
bad strategy-alias        $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy: *shared'
bad include-alias         $'x: &inc\n  - os: ubuntu-latest\njobs:\n  j:\n    strategy:\n      matrix:\n        include: *inc'
bad alias-item            $'x: &item\n  os: ubuntu-latest\njobs:\n  j:\n    strategy:\n      matrix:\n        include:\n          - *item'
bad merge-key             $'jobs:\n  j:\n    strategy:\n      matrix:\n        <<: *shared'
bad external-reusable     $'jobs:\n  j:\n    uses: other-org/tooling/.github/workflows/build.yml@main'
# --- must pass --------------------------------------------------------------
good ubuntu-latest        $'jobs:\n  j:\n    runs-on: ubuntu-latest'
good upper-hosted-label   $'jobs:\n  j:\n    runs-on: Ubuntu-Latest'
good comment-stripped     $'jobs:\n  j:\n    runs-on: ubuntu-latest  # was: self-hosted-pool'
good quoted-matrix-os     $'jobs:\n  j:\n    runs-on: "${{ matrix.os }}"'
good inline-matrix        $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [ubuntu-latest, macos-latest, \'windows-latest\']'
good include-item         $'jobs:\n  j:\n    strategy:\n      matrix:\n        include:\n          - os: windows-latest\n            python: 3.12'
good dispatch-input-os    $'on:\n  workflow_dispatch:\n    inputs:\n      os:\n        type: choice\n        options: [linux, mac]\njobs:\n  j:\n    runs-on: ubuntu-latest'
good action-with-os       $'jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: some/action@abc\n        with:\n          os: linux'
good action-with-include  $'jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: some/action@abc\n        with:\n          include: docs/**'
good os-after-matrix-end  $'jobs:\n  j:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [ubuntu-latest]\n    env:\n      os: linux'
good local-reusable       $'jobs:\n  j:\n    uses: ./.github/workflows/x.yml'
good script-string        $'jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo "runs-on: self-hosted-pool is banned"'
good markdown-bullets     $'jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          echo "* item" >> "$GITHUB_STEP_SUMMARY"\n          echo "- **bold** item" >> "$GITHUB_STEP_SUMMARY"'
good job-anchor           $'jobs:\n  a: &job\n    runs-on: ubuntu-latest\n  b: *job'

if [ "$fail" -ne 0 ]; then echo "runner-drift-guard selftest FAILED"; exit 1; fi
echo "runner-drift-guard selftest OK ($(grep -c '^bad ' "$0") bad, $(grep -c '^good ' "$0") good)"
