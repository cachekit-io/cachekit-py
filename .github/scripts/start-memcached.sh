#!/usr/bin/env bash
# Start the CI Memcached on localhost:11211 as container `memcached`.
# The image is pinned by digest, so the registry only carries the bytes: the
# daemon verifies them against the digest wherever they come from. Each
# registry's anonymous quota is per source IP and hosted runners share IPs, so
# the pull walks MEMCACHED_REPOS in order, twice, and uses the first that serves it.
#
#   CI:     bash .github/scripts/start-memcached.sh
#   Local:  bash .github/scripts/start-memcached.sh   (needs docker and a free port 11211)
#   Test:   put a stub `docker` first on PATH; MEMCACHED_DIGEST / MEMCACHED_REPOS override the pin.
set -u

# Docker official memcached image, 1.6-alpine multi-arch index, resolved 2026-10-22.
# Every registry listed serves this digest. Renovate does not bump it.
MEMCACHED_DIGEST=${MEMCACHED_DIGEST:-sha256:4deb497302f98c3cc516ed4c57a0bcf203e6fc5bd5496bf250cbc103b7a99224}
MEMCACHED_REPOS=${MEMCACHED_REPOS:-mirror.gcr.io/library/memcached public.ecr.aws/docker/library/memcached docker.io/library/memcached}

image=
for round in 1 2; do
  for repo in $MEMCACHED_REPOS; do
    if timeout 120 docker pull "$repo@$MEMCACHED_DIGEST"; then image=$repo@$MEMCACHED_DIGEST; break 2; fi
  done
  [ "$round" = 1 ] && sleep 15
done
[ -n "$image" ] || { echo "::error::no registry served memcached@${MEMCACHED_DIGEST} (tried: ${MEMCACHED_REPOS})"; exit 1; }

docker run -d --name memcached -p 11211:11211 "$image" -m 64 || exit 1

# Ready when the server answers `version` on the published port. The 30s deadline
# covers the probes too, so a server that never comes up, or accepts but never
# answers, fails this step instead of hanging the job.
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/11211 && printf "version\r\n" >&3 && head -c 7 <&3' 2>/dev/null | grep -q VERSION; then
    exit 0
  fi
  sleep 1
done
echo "::error::memcached did not answer on 127.0.0.1:11211 within 30s"
docker logs memcached
exit 1
