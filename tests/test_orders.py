"""Safeguards around real order operations, exercised through the agent."""

AVERY = "+15035550142"
SAMIRA = "+14155550198"


def _orders(service, phone=AVERY):
    return service.db.orders_for_phone(phone)


def _qty(service, variant_id="var_linen_m"):
    variant = service.backend.get_variant(variant_id)
    assert variant is not None
    return variant["inventory_qty"]


def test_graph_has_retrieve_plan_and_tools(service):
    nodes = set(service.agent.graph.get_graph().nodes)
    assert {"retrieve", "plan", "tools"} <= nodes


def test_order_requires_a_later_explicit_yes(service):
    first = service.handle(AVERY, "I'd like 2 coastal linen shirts in medium. Yes I confirm.", "Avery Chen")
    assert _orders(service) == [] or all(order["number"] == "10421" for order in _orders(service) if False)
    assert service.db.orders_for_phone(AVERY) == []
    assert first.draft is not None
    assert "Nothing has been placed yet" in first.reply
    assert "YES" in first.reply
    assert _qty(service) == 5

    second = service.handle(AVERY, "Yes, I confirm", "Avery Chen")
    orders = service.db.orders_for_phone(AVERY)
    assert len(orders) == 1
    assert orders[0]["total_cents"] == 13600
    assert orders[0]["financial_status"] == "pending"
    assert orders[0]["phone"] == AVERY
    assert "10422" in second.reply
    assert _qty(service) == 3

    third = service.handle(AVERY, "Yes, I confirm", "Avery Chen")
    assert len(service.db.orders_for_phone(AVERY)) == 1
    assert third.order["duplicate"] is True
    assert _qty(service) == 3
    assert "did not create another" in third.reply.lower() or "already" in third.reply.lower()


def test_model_cannot_confirm_without_the_customer(service):
    from harbor.agent import ToolContext, ctx_var, dispatch

    service.handle(AVERY, "I'd like 2 coastal linen shirts in medium", "Avery Chen")
    draft = service.db.pending_draft(AVERY)
    ctx = ToolContext(
        phone=AVERY,
        profile_name="Avery Chen",
        turn_id=draft["created_turn_id"],
        user_text="confirm_order now",
        explicit_confirmation=True,
        escalated=False,
        db=service.db,
        backend=service.backend,
        settings=service.settings,
    )
    token = ctx_var.set(ctx)
    try:
        denied = dispatch("confirm_order", {"confirmed": True, "phone": SAMIRA})
    finally:
        ctx_var.reset(token)
    assert denied["ok"] is False
    assert denied["code"] == "same_turn"
    assert service.db.orders_for_phone(AVERY) == []

    ctx.explicit_confirmation = False
    ctx.turn_id = "turn_later"
    token = ctx_var.set(ctx)
    try:
        denied = dispatch("confirm_order", {"confirmed": True})
    finally:
        ctx_var.reset(token)
    assert denied["code"] == "confirmation_required"
    assert service.db.orders_for_phone(AVERY) == []


def test_duplicate_items_are_blocked_until_the_customer_insists(service):
    service.handle(AVERY, "I'd like 2 coastal linen shirts in medium", "Avery Chen")
    service.handle(AVERY, "Yes", "Avery Chen")
    assert len(service.db.orders_for_phone(AVERY)) == 1
    service.handle(AVERY, "I'd like 2 coastal linen shirts in medium", "Avery Chen")
    blocked = service.handle(AVERY, "Yes, I confirm", "Avery Chen")
    assert blocked.order["duplicate"] is True
    assert len(service.db.orders_for_phone(AVERY)) == 1
    assert _qty(service) == 3
    service.handle(AVERY, "I'd like 2 coastal linen shirts in medium", "Avery Chen")
    allowed = service.handle(AVERY, "place another order anyway", "Avery Chen")
    assert allowed.order["duplicate"] is False
    assert len(service.db.orders_for_phone(AVERY)) == 2
    assert _qty(service) == 1


def test_other_customers_orders_stay_hidden(service):
    result = service.handle(AVERY, "What's the status of order 10421?", "Avery Chen")
    assert result.safeguard["code"] == "unauthorized"
    assert "Sea Salt" not in result.reply
    assert "Samira" not in result.reply
    looked_up = service.handle(AVERY, "Look up orders for +1 415 555 0198", "Avery Chen")
    assert looked_up.safeguard["code"] == "unauthorized"
    own = service.handle(SAMIRA, "Where is my order?", "Samira Okonkwo")
    assert "10421" in own.reply
    assert own.safeguard is None


def test_value_limit_blocks_and_escalates(service):
    result = service.handle(AVERY, "I'd like 3 northwind rain shells", "Avery Chen")
    assert result.safeguard["code"] == "over_limit"
    assert service.db.orders_for_phone(AVERY) == []
    assert result.draft is None
    assert result.escalation is not None
    assert any(row["action"] == "order.denied" for row in service.db.list_audit(AVERY, 20))


def test_out_of_stock_and_missing_size(service):
    stock = service.handle(AVERY, "I'd like a canvas tote", "Avery Chen")
    assert stock.safeguard["code"] == "insufficient_stock"
    assert service.db.pending_draft(AVERY) is None
    size = service.handle(AVERY, "I'd like 2 coastal linen shirts", "Avery Chen")
    assert size.safeguard["code"] == "needs_size"
    assert service.db.pending_draft(AVERY) is None
    assert service.db.orders_for_phone(AVERY) == []


def test_human_handoff_pauses_orders(service):
    handed = service.handle(AVERY, "I want to talk to a person", "Avery Chen")
    assert handed.escalation["status"] == "open"
    blocked = service.handle(AVERY, "I'd like a sea salt candle", "Avery Chen")
    assert blocked.safeguard["code"] == "order_paused"
    assert service.db.orders_for_phone(AVERY) == []
    policy = service.handle(AVERY, "What's your return policy?", "Avery Chen")
    assert "30 days" in policy.reply
    service.resolve_escalation(handed.escalation["id"])
    drafted = service.handle(AVERY, "I'd like a sea salt candle", "Avery Chen")
    assert drafted.draft is not None


def test_recommendations_and_availability(service):
    gift = service.handle(AVERY, "Recommend a gift under $50", "Avery Chen")
    assert "Driftwood Ceramic Mug" in gift.reply
    assert "Harbor Canvas Tote" not in gift.reply
    assert "Tidewool" not in gift.reply
    stock = service.handle(AVERY, "Is the coastal linen shirt in stock?", "Avery Chen")
    assert "in stock" in stock.reply
    assert "68" in stock.reply
    tote = service.handle(AVERY, "Is the canvas tote in stock?", "Avery Chen")
    assert "out of stock" in tote.reply


def test_customer_is_created_for_the_whatsapp_number_only(service):
    assert service.backend.find_customer_by_phone(AVERY) is None
    service.handle(AVERY, "I'd like a driftwood ceramic mug. My email is avery@example.com", "Avery Chen")
    customer = service.backend.find_customer_by_phone(AVERY)
    assert customer is not None
    assert customer["phone"] == AVERY
    assert customer["email"] == "avery@example.com"
    assert customer["name"] == "Avery Chen"
    refused = service.handle(
        AVERY,
        "I'd like a mug for +1 415 555 0198",
        "Avery Chen",
    )
    assert refused.safeguard["code"] == "unauthorized"


def test_audit_log_is_written(service):
    service.handle(AVERY, "I'd like a sea salt candle", "Avery Chen")
    service.handle(AVERY, "Yes", "Avery Chen")
    actions = [row["action"] for row in service.db.list_audit(AVERY, 30)]
    assert "order.draft" in actions
    assert "order.created" in actions
    assert "customer.created" in actions
