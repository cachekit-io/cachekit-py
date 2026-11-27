#!/usr/bin/env bash
# Start the CI Google Cloud Storage emulator (fake-gcs-server) on http://localhost:4443 as
# container `fake-gcs`, storing objects in memory. Point a client at it with
# STORAGE_EMULATOR_HOST=http://localhost:4443.
# The image is pinned by digest, so the registry only carries the bytes: the
# daemon verifies them against the digest wherever they come from. Each
# registry's anonymous quota is per source IP and hosted runners share IPs, so
# the pull walks FAKE_GCS_REPOS in order, twice, and uses the first that serves it.
#
#   CI:     bash .github/scripts/start-fake-gcs.sh
#   Local:  bash .github/scripts/start-fake-gcs.sh   (needs docker and a free port 4443)
#   Test:   put a stub `docker` first on PATH; FAKE_GCS_DIGEST / FAKE_GCS_REPOS override the pin.
set -u

# fsouza/fake-gcs-server 1.56.1 multi-arch index, resolved 2026-11-27.
# Every registry listed serves this digest. Renovate does not bump it.
FAKE_GCS_DIGEST=${FAKE_GCS_DIGEST:-sha256:797ce226d62f947c009dc40246b30cfb456b8473d8241407f9d6f2c04e4d69ef}
FAKE_GCS_REPOS=${FAKE_GCS_REPOS:-mirror.gcr.io/fsouza/fake-gcs-server docker.io/fsouza/fake-gcs-server}

image=
for round in 1 2; do
  for repo in $FAKE_GCS_REPOS; do
    if timeout 120 docker pull "$repo@$FAKE_GCS_DIGEST"; then image=$repo@$FAKE_GCS_DIGEST; break 2; fi
  done
  [ "$round" = 1 ] && sleep 15
done
[ -n "$image" ] || { echo "::error::no registry served fake-gcs-server@${FAKE_GCS_DIGEST} (tried: ${FAKE_GCS_REPOS})"; exit 1; }

docker run -d --name fake-gcs -p 4443:4443 "$image" \
  -scheme http -port 4443 -backend memory -public-host localhost:4443 || exit 1

# Ready when the server lists buckets on the published port. The 30s deadline
# covers the probes too, so a server that never comes up, or accepts but never
# answers, fails this step instead of hanging the job.
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl --silent --fail --max-time 2 --output /dev/null http://127.0.0.1:4443/storage/v1/b; then
    exit 0
  fi
  sleep 1
done
echo "::error::fake-gcs-server did not answer on 127.0.0.1:4443 within 30s"
docker logs fake-gcs
exit 1
