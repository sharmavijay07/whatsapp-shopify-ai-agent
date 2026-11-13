#!/bin/sh
# Render injects PORT. n8n listens on N8N_PORT and must bind every interface.
export N8N_PORT="${PORT:-5678}"
export N8N_HOST="${N8N_HOST:-0.0.0.0}"
export N8N_BLOCK_ENV_ACCESS_IN_NODE="${N8N_BLOCK_ENV_ACCESS_IN_NODE:-false}"
exec n8n start
