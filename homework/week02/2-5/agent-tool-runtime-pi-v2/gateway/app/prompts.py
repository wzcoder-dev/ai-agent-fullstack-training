from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml


class PromptRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path

    def render(self, name: str, version: str | None, variables: dict[str, str]) -> dict[str, str]:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))["prompts"]
        if name not in raw:
            raise KeyError(f"PROMPT_NOT_FOUND: {name}")
        selected = version or raw[name]["active_version"]
        versions = raw[name]["versions"]
        if selected not in versions:
            raise KeyError(f"PROMPT_VERSION_NOT_FOUND: {name}@{selected}")
        entry = versions[selected]
        required = set(entry["required_variables"])
        supplied = set(variables)
        if required != supplied:
            missing = sorted(required - supplied)
            extra = sorted(supplied - required)
            raise ValueError(f"PROMPT_VARIABLE_MISMATCH: missing={missing}, extra={extra}")
        prompt = entry["template"]
        for key, value in variables.items():
            prompt = re.sub(r"{{\s*" + re.escape(key) + r"\s*}}", value, prompt)
        if re.search(r"{{[^}]+}}", prompt):
            raise ValueError("PROMPT_RENDER_INCOMPLETE")
        return {
            "name": name,
            "version": selected,
            "prompt": prompt,
            "content_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
