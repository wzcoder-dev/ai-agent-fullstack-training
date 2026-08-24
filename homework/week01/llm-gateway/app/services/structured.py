"""结构化输出出口防线：JSON 提取、Schema 校验、修复提示构造。

对应课程 1-5：校验结果收集为 ValidationIssue（validationIssue.py 模式），
修复提示要求"保持原任务含义不变"（repair.py 模式）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import SchemaError, validators

from app.core.errors import GatewayError


class JsonExtractionError(Exception):
    """模型输出无法提取出合法 JSON。"""


@dataclass(frozen=True)
class ValidationIssue:
    # 单条校验问题：实例路径 + 机器可读码 + 人类可读说明。
    path: str
    code: str
    message: str


_FENCED_PATTERN = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def check_schema_is_valid(schema: dict[str, Any]) -> None:
    # 请求携带的 response_schema 本身必须合法，否则入口 400。
    try:
        validator_class = validators.validator_for(schema)
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise GatewayError("invalid_json_schema", f"response_schema 不是合法 JSON Schema: {exc.message}", 400) from exc


def extract_json(text: str) -> Any:
    # 依次尝试：原文直解 → markdown 围栏 → 首尾大括号/中括号子串。
    candidate = text.strip()
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    for match in _FENCED_PATTERN.finditer(text):
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise JsonExtractionError("输出中找不到合法 JSON")


def validate_against_schema(instance: Any, schema: dict[str, Any]) -> list[ValidationIssue]:
    # 迭代收集全部校验错误而不是抛异常，供修复提示与审计使用。
    validator_class = validators.validator_for(schema)
    validator_class.check_schema(schema)
    issues: list[ValidationIssue] = []
    for error in validator_class(schema).iter_errors(instance):
        path = "$" + "".join(f"[{part!r}]" if isinstance(part, str) else f"[{part}]" for part in error.absolute_path)
        issues.append(ValidationIssue(path=path, code=error.validator or "invalid", message=error.message))
    return issues


def build_repair_message(issues: list[ValidationIssue], schema: dict[str, Any]) -> str:
    # 修复提示：只修正格式与字段，不改变任务语义（课程 1-5 原则）。
    lines = [
        "上一次输出未通过 JSON Schema 校验。请保持原任务含义不变，",
        "只修正 JSON 语法、字段、类型或字段组合，重新输出完整的 JSON，不要附加解释或 Markdown。",
        "",
        "要求符合的 JSON Schema：",
        json.dumps(schema, ensure_ascii=False, indent=2),
        "",
        "发现的问题：",
    ]
    for issue in issues:
        lines.append(f"- {issue.path}: {issue.code} — {issue.message}")
    if not issues:
        lines.append("- 输出不是合法 JSON")
    return "\n".join(lines)
