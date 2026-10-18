#!/usr/bin/env bash
# Run journeys/journeys.py the way a user meets cachekit: the built wheel with its [data] extra and polars,
# in a fresh virtualenv, outside the repository, with REDIS_URL and every CACHEKIT_* variable unset and
# nothing listening on localhost:6379. See docs/CONTRIBUTING.md, "User Journeys".
#
#   JOURNEY_PYTHON    interpreter for the build and the virtualenv (default 3.13: the oldest Python on
#                     which every documented Arrow fast path applies)
#   JOURNEY_CACHEKIT  what to install instead of a fresh build, as a requirement with the [data] extra,
#                     e.g. 'cachekit[data]==0.22.0' for a PyPI release
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${JOURNEY_PYTHON:-3.13}"
work="$(mktemp -d "${TMPDIR:-/tmp}/cachekit-journeys.XXXXXX")"
trap 'rm -rf "${work:?}"' EXIT

uv venv --quiet --python "$python" "$work/venv"
venv_python="$work/venv/bin/python"

# Fail before the build, which takes minutes. journeys.py checks again with its own resolver.
if ! "$venv_python" - <<'EOF'
import socket, sys
for family, _, _, _, addr in socket.getaddrinfo("localhost", 6379, type=socket.SOCK_STREAM):
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(addr) == 0:
            sys.exit(f"journeys: something listens on localhost:6379 ({addr[0]}); stop it, a user's machine has no Redis")
EOF
then
    exit 2
fi

requirement="${JOURNEY_CACHEKIT:-}"
if [[ -z "$requirement" ]]; then
    echo "journeys: building the wheel for Python $python"
    uv build --quiet --wheel --python "$python" --out-dir "$work/dist" "$repo"
    wheels=("$work"/dist/cachekit-*.whl)
    requirement="cachekit[data] @ file://${wheels[0]}"
fi

echo "journeys: installing $requirement"
uv pip install --quiet --python "$venv_python" "$requirement" polars

# The scrub matches journeys.py's, so a direct run of the script and this one see the same environment.
mapfile -t unset_args < <(compgen -e | grep -E '^(CACHEKIT_|REDIS_URL$)' | sed 's/^/-u/')
cd "$work"
env "${unset_args[@]}" "$venv_python" -I "$repo/journeys/journeys.py"
