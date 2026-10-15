# Build cachekit wheels for Linux (amd64 + arm64)
# Builds both Python and Rust components (cachekit-rs)
# Usage: docker buildx build --platform linux/amd64,linux/arm64 --output type=local,dest=./dist-linux .

# Stage 1: Builder
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

WORKDIR /app

# Install system dependencies: build tools, curl, git, and OpenSSL dev (needed for Rust)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    git \
    libssl-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install Rust (needed for cachekit-rs Rust extension)
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable

# Install uv via pip
RUN pip install --no-cache-dir uv

# Copy cachekit source (uses .dockerignore to exclude unnecessary files)
COPY . .

# Build wheels (will compile Rust extension automatically)
# BuildKit cache mounts persist cargo/rust compilation artifacts between builds
RUN --mount=type=cache,target=/root/.cargo \
    --mount=type=cache,target=/app/rust/target \
    uv build

# Stage 2: Extract wheels only (minimal filesystem for docker buildx output)
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# Copy only the built wheels from builder stage
COPY --from=builder /app/dist/*.whl /

# List wheels for verification
RUN ls -lh /*.whl
