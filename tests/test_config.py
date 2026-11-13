from harbor.config import Settings


def test_groq_is_the_default_provider():
    settings = Settings(
        llm_provider="groq",
        llm_model="",
        llm_base_url="",
        groq_api_key="",
        llm_api_key="",
    )
    assert settings.resolved_model() == "openai/gpt-oss-120b"
    assert settings.resolved_base_url() == "https://api.groq.com/openai/v1"
    assert settings.use_mock_llm is True
    status = settings.llm_status()
    assert status["active"] == "mock-fallback"
    # No Qdrant URL: retrieval stays on the in-memory index.
    assert settings.uses_memory_index() is True
    assert status["embeddings"]["provider"] == "bm25"


def test_groq_key_activates_the_live_model():
    settings = Settings(llm_provider="groq", groq_api_key="gsk_test", llm_model="")
    assert settings.use_mock_llm is False
    assert settings.resolved_api_key() == "gsk_test"
    assert settings.llm_status()["active"] == "groq"
    assert settings.llm_status()["model"] == "openai/gpt-oss-120b"


def test_other_providers_stay_optional():
    openai = Settings(llm_provider="openai", llm_model="", openai_api_key="sk-test")
    assert openai.resolved_base_url() == "https://api.openai.com/v1"
    assert openai.resolved_model() == "gpt-4o-mini"
    gemini = Settings(llm_provider="gemini", llm_model="gemini-2.5-flash", gemini_api_key="g")
    assert gemini.resolved_model() == "gemini-2.5-flash"
    assert "generativelanguage.googleapis.com" in gemini.resolved_base_url()


def test_explicit_base_url_overrides_the_preset():
    settings = Settings(
        llm_provider="groq",
        llm_base_url="https://example.test/v1",
        llm_model="custom-model",
        groq_api_key="gsk_test",
    )
    assert settings.resolved_base_url() == "https://example.test/v1"
    assert settings.resolved_model() == "custom-model"


def test_embedded_qdrant_stays_available_off_render(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    settings = Settings(vector_store="qdrant", qdrant_url="", embedding_provider="sentence-transformers")
    assert settings.uses_memory_index() is False
    assert settings.llm_status()["embeddings"]["provider"] == "sentence-transformers"
    assert "all-MiniLM-L6-v2" in settings.llm_status()["embeddings"]["model"]


def test_render_minilm_stays_on_bm25_even_with_a_qdrant_url(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    settings = Settings(
        vector_store="qdrant",
        qdrant_url="https://example.cloud.qdrant.io:6333",
        qdrant_api_key="qd_test",
        embedding_provider="sentence-transformers",
    )
    assert settings.uses_memory_index() is True
    assert settings.embedding_status()["provider"] == "bm25"


def test_hosted_embeddings_use_qdrant_cloud_on_render(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    settings = Settings(
        vector_store="qdrant",
        qdrant_url="https://example.cloud.qdrant.io:6333",
        qdrant_api_key="qd_test",
        embedding_provider="hosted",
        embedding_model="jina-embeddings-v3",
        embedding_api_url="https://api.jina.ai/v1",
    )
    assert settings.uses_memory_index() is False
    assert settings.embedding_status() == {
        "provider": "hosted",
        "model": "jina-embeddings-v3",
        "device": "api",
    }
    assert settings.email_is_configured is False
    assert settings.n8n_configured is False


def test_render_without_qdrant_url_uses_bm25(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    settings = Settings(
        vector_store="qdrant",
        qdrant_url="",
        embedding_provider="sentence-transformers",
    )
    assert settings.uses_memory_index() is True
    assert settings.llm_status()["embeddings"] == {"provider": "bm25", "model": "bm25", "device": "cpu"}


def test_mock_mode_ignores_shopify_credentials():
    settings = Settings(
        app_mode="mock",
        shopify_store_domain="demo.myshopify.com",
        shopify_admin_token="shpat_secret",
        whatsapp_token="token",
        whatsapp_phone_number_id="123",
    )
    assert settings.shopify_is_mock is True
    assert settings.whatsapp_is_live is False
