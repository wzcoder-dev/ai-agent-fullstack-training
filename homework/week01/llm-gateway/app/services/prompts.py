"""Prompt 模板治理：文件加载、StrictUndefined 渲染、指纹与注入防护。

对应课程 1-4（Jinja2 StrictUndefined、信任级标签、sha256 指纹）
与课程 1-6（调用方只能按 (name, version) 选择模板，不能上传正文）。

模板文件格式（prompts/*.j2）：
    ---
    name: knowledge_decision
    version: v1
    description: 知识库决策器系统提示
    variables: [product_name]
    ---
    正文（Jinja2），frontmatter 必须声明正文使用的全部变量。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, Template, UndefinedError

from app.core.errors import GatewayError

_FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _render_environment() -> Environment:
    # StrictUndefined：未定义变量直接报错，杜绝悄悄变空串。
    return Environment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


@dataclass(frozen=True)
class PromptTemplate:
    # 加载后的模板资产：元数据 + 编译好的 Jinja 模板 + 正文指纹。
    name: str
    version: str
    description: str | None
    variables: tuple[str, ...]
    jinja_template: Template
    template_sha256: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)


@dataclass(frozen=True)
class RenderedPrompt:
    # 渲染产物与渲染指纹（同一模板同一变量必然同指纹）。
    content: str
    sha256: str


class PromptRegistry:
    # (name, version) → 模板 的受控注册表，进程内只读。
    def __init__(self, templates: dict[tuple[str, str], PromptTemplate]) -> None:
        self._templates = templates

    @classmethod
    def load(cls, prompts_dir: str | Path) -> PromptRegistry:
        # 扫描目录并做加载期校验；任何问题都让启动失败而不是运行时才暴露。
        directory = Path(prompts_dir)
        if not directory.is_dir():
            raise GatewayError(
                "gateway_misconfigured",
                f"Prompt 模板目录不存在: {directory}",
                503,
            )
        env = _render_environment()
        templates: dict[tuple[str, str], PromptTemplate] = {}
        for path in sorted(directory.glob("*.j2")):
            template = _load_template(path, env)
            if template.key in templates:
                raise ValueError(f"模板重复定义: {template.name} {template.version} ({path})")
            templates[template.key] = template
        if not templates:
            raise GatewayError("gateway_misconfigured", f"Prompt 模板目录为空: {directory}", 503)
        return cls(templates)

    def list(self) -> list[PromptTemplate]:
        return sorted(self._templates.values(), key=lambda t: (t.name, t.version))

    def get(self, name: str, version: str) -> PromptTemplate | None:
        return self._templates.get((name, version))

    def render(self, name: str, version: str, variables: dict[str, str]) -> RenderedPrompt:
        # 缺少声明变量 → 400 稳定错误；渲染结果带 sha256 指纹。
        template = self._templates.get((name, version))
        if template is None:
            raise GatewayError("unknown_prompt_template", f"Prompt 模板不存在: {name} {version}", 400)
        missing = [variable for variable in template.variables if variable not in variables]
        if missing:
            raise GatewayError("missing_prompt_variable", f"缺少 Prompt 变量: {', '.join(missing)}", 400)
        try:
            content = template.jinja_template.render(variables)
        except UndefinedError as exc:
            raise GatewayError("missing_prompt_variable", f"Prompt 渲染缺变量: {exc}", 400) from exc
        return RenderedPrompt(content=content, sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())


def _load_template(path: Path, env: Environment) -> PromptTemplate:
    # 解析 frontmatter 并做试渲染：正文引用未声明变量或语法错误 → 启动失败。
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        raise ValueError(f"模板缺少 frontmatter: {path}")
    try:
        meta: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"模板 frontmatter 不是合法 YAML: {path}") from exc
    name = meta.get("name")
    version = meta.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise ValueError(f"模板 frontmatter 必须声明 name 与 version: {path}")
    description = meta.get("description") if isinstance(meta.get("description"), str) else None
    raw_variables = meta.get("variables", [])
    if not isinstance(raw_variables, list) or not all(isinstance(v, str) for v in raw_variables):
        raise ValueError(f"模板 variables 必须是字符串列表: {path}")
    body = text[match.end() :]
    try:
        jinja_template = env.from_string(body)
        jinja_template.render({variable: "<placeholder>" for variable in raw_variables})
    except Exception as exc:
        raise ValueError(f"模板渲染自检失败（语法错误或引用未声明变量）: {path}: {exc}") from exc
    return PromptTemplate(
        name=name,
        version=version,
        description=description,
        variables=tuple(raw_variables),
        jinja_template=jinja_template,
        template_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
