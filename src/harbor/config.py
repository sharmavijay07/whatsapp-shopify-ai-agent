"""Environment-driven settings. Secrets stay in the environment, never in code."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# (base_url, model). LLM_MODEL / LLM_BASE_URL override these when set.
LLM_PRESETS: dict[str, tuple[str, str]] = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.0-flash",
    ),
    "ollama": ("http://127.0.0.1:11434/v1", "llama3.2"),
    "mock": ("", ""),
}

PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_mode: str = "mock"  # mock | live
    host: str = "0.0.0.0"
    port: int = 8091
    log_level: str = "INFO"
    cors_origins: str = "http://127.0.0.1:43123,http://localhost:43123"
    public_base_url: str = "http://127.0.0.1:8091"

    # Default brain is Groq's OpenAI-compatible API. Other providers are optional.
    llm_provider: str = "groq"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    groq_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 600

    # Groq does not serve embeddings.
    # sentence-transformers = local MiniLM (needs the `local` extra and ~1 GB RAM).
    # bm25 = in-memory keyword index that fits Render's 512 MB free instance.
    # bm25 fits Render's 512 MB instance.
    # sentence-transformers is local MiniLM (the `local` extra, too heavy for free Render).
    # fastembed is a small ONNX model (optional `vectors` extra). It falls back to BM25
    # if the package is missing or the model will not load.
    # hosted calls an OpenAI-compatible embeddings API (Jina's free tier is the one
    # that fits free Render memory). Set EMBEDDING_API_URL and EMBEDDING_API_KEY.
    embedding_provider: str = "sentence-transformers"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_device: str = "cpu"
    embedding_api_url: str = ""
    embedding_api_key: str = ""

    # auto: Qdrant when QDRANT_URL is set, otherwise the in-memory index.
    # qdrant: embedded file store when QDRANT_URL is empty (local demo and tests),
    # or Qdrant Cloud when QDRANT_URL and QDRANT_API_KEY are set.
    # memory: always the in-memory BM25 index. No Qdrant process.
    vector_store: str = "auto"
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_path: str = "./data/runtime/qdrant"
    qdrant_collection: str = "harbor_kb"

    database_path: str = "./data/runtime/harbor.db"
    knowledge_dir: str = ""

    shopify_store_domain: str = ""
    shopify_admin_token: str = ""
    shopify_api_version: str = "2026-10"
    # Custom app API secret key. Shopify signs webhooks with this, not the Admin token.
    shopify_webhook_secret: str = ""

    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "harbor-verify"
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_template_language: str = "en_US"
    whatsapp_status_template: str = "harbor_order_status"

    # Gmail SMTP. Both user and app password must be set, or email stays in demo mode.
    gmail_user: str = ""
    gmail_app_password: str = ""
    owner_email: str = ""

    n8n_order_webhook_url: str = ""
    n8n_error_webhook_url: str = ""
    n8n_notify_webhook_url: str = ""
    n8n_shared_secret: str = ""

    max_order_value: float = 500.0
    currency: str = "USD"
    draft_ttl_minutes: int = 30
    duplicate_window_minutes: int = 10

    @property
    def max_order_value_cents(self) -> int:
        return int(round(self.max_order_value * 100))

    def resolved_model(self) -> str:
        if self.llm_model.strip():
            return self.llm_model.strip()
        preset = LLM_PRESETS.get(self.llm_provider, LLM_PRESETS["groq"])
        return preset[1]

    def resolved_base_url(self) -> str:
        if self.llm_base_url.strip():
            return self.llm_base_url.strip()
        preset = LLM_PRESETS.get(self.llm_provider, LLM_PRESETS["groq"])
        return preset[0]

    def resolved_api_key(self) -> str:
        if self.llm_api_key.strip():
            return self.llm_api_key.strip()
        if self.llm_provider == "groq":
            return self.groq_api_key.strip()
        if self.llm_provider == "openai":
            return self.openai_api_key.strip()
        if self.llm_provider == "gemini":
            return self.gemini_api_key.strip()
        if self.llm_provider == "ollama":
            return "ollama"
        return ""

    @property
    def use_mock_llm(self) -> bool:
        """Deterministic planner when no model is configured.

        Mock Shopify mode does not force this. A Groq key uses Groq against
        the fake store, which is the portfolio demo. With no key, the same
        graph still runs so the sandbox works offline.
        """
        if self.llm_provider == "mock":
            return True
        if self.llm_provider == "ollama":
            return False
        return not bool(self.resolved_api_key())

    @property
    def shopify_is_mock(self) -> bool:
        if self.app_mode != "live":
            return True
        return not (self.shopify_store_domain.strip() and self.shopify_admin_token.strip())

    @property
    def whatsapp_is_live(self) -> bool:
        if self.app_mode != "live":
            return False
        return bool(self.whatsapp_token.strip() and self.whatsapp_phone_number_id.strip())

    @property
    def email_is_configured(self) -> bool:
        return bool(self.gmail_user.strip() and self.gmail_app_password.strip())

    @property
    def n8n_configured(self) -> bool:
        return any(
            value.strip()
            for value in (
                self.n8n_order_webhook_url,
                self.n8n_error_webhook_url,
                self.n8n_notify_webhook_url,
            )
        )

    def resolved_embedding_model(self) -> str:
        embed = self.embedding_provider.strip().lower()
        model = self.embedding_model.strip()
        default_minilm = "sentence-transformers/all-MiniLM-L6-v2"
        if embed == "fastembed" and model in {"", default_minilm}:
            return "BAAI/bge-small-en-v1.5"
        return model or default_minilm

    def uses_memory_index(self) -> bool:
        """True when retrieval must not load a vector model or a Qdrant client.

        Render's free web service has 512 MB of RAM. MiniLM does not fit there.
        Hosted embeddings (or fastembed) plus Qdrant Cloud do, because the
        vectors are not computed with PyTorch inside this process. With no
        QDRANT_URL the process stays on the in-memory BM25 index.
        """
        store = self.vector_store.strip().lower()
        embed = self.embedding_provider.strip().lower()
        if store in {"memory", "bm25"} or embed == "bm25":
            return True
        on_render = os.environ.get("RENDER", "").lower() == "true"
        if on_render and embed in {"sentence-transformers", "minilm"}:
            return True
        if on_render and not self.qdrant_url.strip():
            return True
        if store == "qdrant":
            return False
        return not self.qdrant_url.strip()

    def embedding_status(self) -> dict:
        if self.uses_memory_index():
            return {"provider": "bm25", "model": "bm25", "device": "cpu"}
        embed = self.embedding_provider.strip().lower()
        model = self.resolved_embedding_model()
        if embed == "hosted":
            return {"provider": "hosted", "model": model, "device": "api"}
        if embed == "fastembed":
            return {"provider": "fastembed", "model": model, "device": "cpu"}
        return {
            "provider": "sentence-transformers",
            "model": model,
            "device": self.embedding_device,
        }

    def knowledge_path(self) -> Path:
        if self.knowledge_dir.strip():
            return Path(self.knowledge_dir).expanduser()
        return PACKAGE_DIR / "knowledge"

    def llm_status(self) -> dict:
        active = self.llm_provider
        reason = ""
        if self.llm_provider == "mock":
            active = "mock"
        elif self.use_mock_llm:
            active = "mock-fallback"
            reason = (
                f"{self.llm_provider} is selected but no API key is set, "
                "so the deterministic planner is running."
            )
        return {
            "provider": self.llm_provider,
            "model": self.resolved_model(),
            "base_url": self.resolved_base_url(),
            "active": active,
            "reason": reason,
            "embeddings": self.embedding_status(),
        }


def get_settings() -> Settings:
    return Settings()
