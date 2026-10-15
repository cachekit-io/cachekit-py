#!/usr/bin/env bash
# Start the CI Redis (no persistence) on localhost:6379 as container `redis`.
# The image is pinned by digest, so the registry only carries the bytes: the
# daemon verifies them against the digest wherever they come from. Each
# registry's anonymous quota is per source IP and hosted runners share IPs, so
# the pull walks REDIS_REPOS in order, twice, and uses the first that serves it.
#
#   CI:     bash .github/scripts/start-redis.sh
#   Local:  bash .github/scripts/start-redis.sh   (needs docker and a free port 6379)
#   Test:   put a stub `docker` first on PATH; REDIS_DIGEST / REDIS_REPOS override the pin.
set -u

# Docker official redis image, 7-alpine multi-arch index, resolved 2026-10-09.
# Every registry listed serves this digest. Renovate does not bump it.
REDIS_DIGEST=${REDIS_DIGEST:-sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499}
REDIS_REPOS=${REDIS_REPOS:-mirror.gcr.io/library/redis public.ecr.aws/docker/library/redis docker.io/library/redis}

image=
for round in 1 2; do
  for repo in $REDIS_REPOS; do
    if timeout 120 docker pull "$repo@$REDIS_DIGEST"; then image=$repo@$REDIS_DIGEST; break 2; fi
  done
  [ "$round" = 1 ] && sleep 15
done
[ -n "$image" ] || { echo "::error::no registry served redis@${REDIS_DIGEST} (tried: ${REDIS_REPOS})"; exit 1; }

docker run -d --name redis -p 6379:6379 "$image" redis-server --save "" --appendonly no || exit 1
until docker exec redis redis-cli ping | grep -q PONG; do sleep 1; done
