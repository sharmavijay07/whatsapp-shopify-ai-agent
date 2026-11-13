import os

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from pathlib import Path

import pytest

from harbor.config import Settings
from harbor.service import AgentService, build_service

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        app_mode="mock",
        llm_provider="mock",
        database_path=str(tmp_path / "harbor.db"),
        qdrant_path=str(tmp_path / "qdrant"),
        knowledge_dir=str(ROOT / "src" / "harbor" / "knowledge"),
        max_order_value=500,
        duplicate_window_minutes=10,
        draft_ttl_minutes=30,
    )


@pytest.fixture
def service(settings) -> AgentService:
    built = build_service(settings)
    yield built
    built.close()
