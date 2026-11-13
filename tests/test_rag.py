def test_memory_index_answers_without_a_vector_model(tmp_path):
    from pathlib import Path

    from harbor.config import Settings
    from harbor.service import build_service

    settings = Settings(
        app_mode="mock",
        llm_provider="mock",
        vector_store="memory",
        embedding_provider="bm25",
        database_path=str(tmp_path / "harbor.db"),
        qdrant_url="",
        knowledge_dir=str(Path(__file__).resolve().parents[1] / "src" / "harbor" / "knowledge"),
    )
    service = build_service(settings)
    try:
        assert service.rag.backend == "memory"
        assert service.rag.embedder is None
        assert service.rag.vector_count() >= 8
        hits = service.rag.search("can I send something back if it doesn't fit?", k=3)
        blob = " ".join(hit["text"] for hit in hits).lower()
        assert "30 days" in blob
        reply = service.handle("+15035550142", "What's your return policy?", "Avery Chen")
        assert "30 days" in reply.reply
    finally:
        service.close()


def test_vector_store_is_populated(service):
    assert service.rag.vector_count() >= 8
    assert service.rag.dim == 384


def test_return_policy_is_retrieved(service):
    hits = service.rag.search("can I send something back if it doesn't fit?", k=3)
    assert hits
    blob = " ".join(hit["title"] + " " + hit["text"] for hit in hits).lower()
    assert "30 days" in blob or "return" in blob
    reply = service.handle("+15035550142", "What's your return policy?", "Avery Chen")
    assert "30 days" in reply.reply
    assert any(source["kind"] == "policy" for source in reply.sources)


def test_shipping_answer_comes_from_the_knowledge_base(service):
    reply = service.handle("+15035550142", "How long does shipping take?", "Avery Chen")
    assert "3 to 5" in reply.reply or "Portland" in reply.reply
