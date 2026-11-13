#!/usr/bin/env bash
# Render free-tier build. No PyTorch, no Qdrant, no n8n.
# Node is unpacked into the repo so `next build` can run on Render's Python image.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install --upgrade pip
python -m pip install .

if ! command -v npm >/dev/null 2>&1; then
  NODE_VERSION="${NODE_VERSION:-v22.21.0}"
  mkdir -p "$PWD/.node"
  curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.gz" \
    | tar -xz --strip-components=1 -C "$PWD/.node"
  export PATH="$PWD/.node/bin:$PATH"
fi

export NEXT_OUTPUT=export
export NEXT_TELEMETRY_DISABLED=1
npm --prefix web ci
npm --prefix web run build
test -f web/out/index.html
