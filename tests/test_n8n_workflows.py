import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "n8n" / "workflows"


def test_workflows_are_importable_and_guard_orders():
    files = sorted(WORKFLOWS.glob("*.json"))
    assert len(files) >= 4
    seen = set()
    for path in files:
        workflow = json.loads(path.read_text(encoding="utf-8"))
        assert workflow["name"]
        assert workflow["nodes"]
        assert workflow["connections"]
        seen.update(node["type"] for node in workflow["nodes"])
    assert "n8n-nodes-base.webhook" in seen
    assert "n8n-nodes-base.if" in seen
    assert "n8n-nodes-base.code" in seen
    assert "n8n-nodes-base.errorTrigger" in seen
    order = json.loads((WORKFLOWS / "02-order-processing.json").read_text(encoding="utf-8"))
    code = next(node for node in order["nodes"] if node["name"] == "Re-check safeguards")
    source = code["parameters"]["jsCode"]
    assert "confirmation.explicit" in source
    assert "idempotency_key" in source
    assert "verified_phone" in source
    assert "max_order_value_cents" in source
    assert "already_created" in source
