#!/usr/bin/env bash
# Build Linux wheels for amd64 + arm64 using Docker buildx.
# Extracted verbatim from the Makefile `build-multiarch-linux` target.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

LOG_BUILD_DIR="logs/build"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo "${BLUE}Building multi-arch Linux wheels...${RESET}"
command -v docker >/dev/null 2>&1 || { echo "${YELLOW}❌ docker not found. Install Docker: https://www.docker.com/products/docker-desktop${RESET}"; exit 1; }
echo "${YELLOW}Setting up multi-platform builder...${RESET}"
if ! docker buildx ls | grep -q "cachekit-builder"; then
	echo "${YELLOW}Creating buildx builder for multi-platform support...${RESET}"
	docker buildx create --name cachekit-builder --platform linux/amd64,linux/arm64 --use || \
		docker buildx use cachekit-builder
else
	echo "${GREEN}✓ Using existing cachekit-builder${RESET}"
	docker buildx use cachekit-builder
fi
echo "${YELLOW}Building for linux/amd64 and linux/arm64...${RESET}"
mkdir -p "$LOG_BUILD_DIR" dist .dist-linux-build
docker buildx build --platform linux/amd64,linux/arm64 \
	--output type=local,dest=./.dist-linux-build \
	--file Dockerfile . 2>&1 | tee "$LOG_BUILD_DIR/multiarch_$TIMESTAMP.log" || \
	{ echo "${YELLOW}Build failed. Check $LOG_BUILD_DIR/multiarch_$TIMESTAMP.log${RESET}"; exit 1; }
echo "${YELLOW}Extracting wheels to dist/...${RESET}"
cp .dist-linux-build/linux_amd64/cachekit-*.whl dist/ 2>/dev/null || true
cp .dist-linux-build/linux_arm64/cachekit-*.whl dist/ 2>/dev/null || true
echo "${GREEN}✓ Multi-arch wheels built${RESET}"
ls -lh dist/cachekit-*linux*.whl 2>/dev/null || { echo "${YELLOW}⚠️  No Linux wheels found. Check $LOG_BUILD_DIR/multiarch_$TIMESTAMP.log${RESET}"; exit 1; }
