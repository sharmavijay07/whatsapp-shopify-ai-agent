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
