FROM n8nio/n8n:1.70.3

USER root
COPY start.sh /harbor-n8n-start.sh
RUN chmod 755 /harbor-n8n-start.sh
USER node

ENV N8N_HOST=0.0.0.0 \
    N8N_BLOCK_ENV_ACCESS_IN_NODE=false \
    N8N_DIAGNOSTICS_ENABLED=false \
    N8N_SECURE_COOKIE=false \
    GENERIC_TIMEZONE=America/Los_Angeles

ENTRYPOINT ["/harbor-n8n-start.sh"]
