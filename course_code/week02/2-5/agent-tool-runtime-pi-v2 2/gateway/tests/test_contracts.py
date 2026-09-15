from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import OrderStatusInput
from app.prompts import PromptRegistry


ROOT = Path(__file__).resolve().parents[2]


def test_order_schema_rejects_extra_fields_and_bad_id() -> None:
    with pytest.raises(ValidationError):
        OrderStatusInput.model_validate({"order_id": "bad", "approved": True})


def test_prompt_registry_requires_exact_variables() -> None:
    registry = PromptRegistry(ROOT / "config" / "prompts.yaml")
    with pytest.raises(ValueError, match="PROMPT_VARIABLE_MISMATCH"):
        registry.render("agent.default", "v2", {"tenant_id": "tenant_a", "user_id": "u1"})


def test_prompt_registry_renders_and_hashes_version() -> None:
    registry = PromptRegistry(ROOT / "config" / "prompts.yaml")
    result = registry.render(
        "agent.default",
        "v2",
        {"tenant_id": "tenant_a", "user_id": "u1", "agent_name": "support-agent"},
    )
    assert result["version"] == "v2"
    assert "tenant_a" in result["prompt"]
    assert len(result["content_hash"]) == 64
