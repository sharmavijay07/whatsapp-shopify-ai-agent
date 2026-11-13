#!/usr/bin/env bash
# One command for the credential-free sandbox.
# Groq is used automatically when GROQ_API_KEY is set; otherwise the same
# graph runs on the deterministic planner.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export HF_HOME="${HF_HOME:-$ROOT/.cache/huggingface}"
export SENTENCE_TRANSFORMERS_HOME="${SENTENCE_TRANSFORMERS_HOME:-$ROOT/.cache/sentence-transformers}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_DISABLE_TELEMETRY=1
export APP_MODE="${APP_MODE:-mock}"
export LLM_PROVIDER="${LLM_PROVIDER:-groq}"
export VECTOR_STORE="${VECTOR_STORE:-qdrant}"
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-sentence-transformers}"
export PYTHONPATH="$ROOT/src"
export API_INTERNAL_URL="${API_INTERNAL_URL:-http://127.0.0.1:8091}"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -U pip
  "$ROOT/.venv/bin/pip" install torch --index-url https://download.pytorch.org/whl/cpu
  "$ROOT/.venv/bin/pip" install -e ".[dev]"
fi
if [[ ! -d "$ROOT/web/node_modules" ]]; then
  npm --prefix "$ROOT/web" install
fi

mkdir -p "$ROOT/data/runtime"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

"$ROOT/.venv/bin/uvicorn" harbor.api:app --host 0.0.0.0 --port 8091 &
API_PID=$!
npm --prefix "$ROOT/web" run dev &
WEB_PID=$!

cleanup() {
  kill "$API_PID" "$WEB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 180); do
  if curl -sf "http://127.0.0.1:8091/health" >/dev/null; then
    break
  fi
  sleep 1
done

echo ""
echo "Harbor & Co. sandbox   http://127.0.0.1:43123"
echo "Agent health           http://127.0.0.1:8091/health"
echo "Shopify and WhatsApp are mocked. Set GROQ_API_KEY to plan with openai/gpt-oss-120b."
echo ""
wait
