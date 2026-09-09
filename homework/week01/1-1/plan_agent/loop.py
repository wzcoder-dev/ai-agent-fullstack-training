"""Agent Loop 主流程。

一个循环做三件事，直到输出通过契约校验的计划或触发停止条件：
1. 调模型（LLMError 按类别退避重试，超过上限即失败）；
2. 模型要工具（tool_call JSON）→ 执行只读工具，结果回填上下文；
3. 模型给计划 → pydantic 契约校验，失败把「具体字段错误」回传重试。

所有停止条件（429/超时/拒答、Schema 重试耗尽、工具轮次耗尽）都写进
trace 再以 failed 状态返回，不静默、不死循环。

这里刻意用同步实现：本作业工具是纯本地只读读文件，没有真正需要
并发的 I/O，同步让 Loop 与测试都更直白（流式与沙箱执行是后面小节的主题）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader

from .adapters import LLMError, ModelAdapter
from .context import ToolError, ToolHub, scan_repo
from .schema import CONTRACT_DESCRIPTION, PlanContract, format_validation_errors, render_plan
from .trace import Trace

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_PROMPT_TEMPLATE = "planner.v1.j2"

MAX_TOOL_ROUNDS_DEFAULT = 6
MAX_SCHEMA_RETRIES_DEFAULT = 2
MAX_LLM_RETRIES_DEFAULT = 2
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 8.0
RETRYABLE_KINDS = {"rate_limit", "timeout", "api"}


@dataclass
class LoopResult:
    status: Literal["ok", "failed"]
    plan: PlanContract | None
    failure: dict | None
    trace: Trace
    messages: list[dict]

    def render(self) -> str:
        if self.status == "ok" and self.plan is not None:
            return render_plan(self.plan)
        failure = self.failure or {}
        return f"运行失败 [{failure.get('kind')}] {failure.get('message')}"


def extract_json_object(text: str) -> dict | None:
    """从模型输出里抠出一个 JSON 对象；容忍 ```json 围栏与前后闲话。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def render_system_prompt(repo_summary: str, *, max_tool_rounds: int) -> str:
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template(SYSTEM_PROMPT_TEMPLATE)
    return template.render(
        repo_summary=repo_summary,
        contract=CONTRACT_DESCRIPTION,
        max_tool_rounds=max_tool_rounds,
    )


def _complete_with_retry(
    adapter: ModelAdapter,
    messages: list[dict],
    trace: Trace,
    *,
    max_retries: int,
    backoff_base: float,
    sleep: Callable[[float], None],
) -> str:
    """调模型 + 指数退避重试。拒答不重试；其余可重试错误超过上限就抛出。"""
    attempt = 0
    while True:
        started = time.perf_counter()
        try:
            text = adapter.complete(messages)
        except LLMError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            trace.log("llm_error", attempt=attempt, kind=exc.kind, message=exc.message, duration_ms=duration_ms)
            retryable = exc.kind in RETRYABLE_KINDS
            if not retryable or attempt >= max_retries:
                raise
            delay = min(backoff_base * (2**attempt), BACKOFF_MAX_SECONDS)
            trace.log("llm_retry", attempt=attempt, kind=exc.kind, delay_s=round(delay, 2))
            sleep(delay)
            attempt += 1
        else:
            duration_ms = int((time.perf_counter() - started) * 1000)
            trace.log("llm_response", attempt=attempt, chars=len(text), duration_ms=duration_ms)
            return text


def _fail(trace: Trace, messages: list[dict], kind: str, message: str) -> LoopResult:
    trace.log("run_end", status="failed", kind=kind, message=message)
    return LoopResult(
        status="failed",
        plan=None,
        failure={"kind": kind, "message": message},
        trace=trace,
        messages=messages,
    )


def run_planner(
    *,
    issue: str,
    repo_root: Path,
    adapter: ModelAdapter,
    tools: ToolHub | None = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS_DEFAULT,
    max_schema_retries: int = MAX_SCHEMA_RETRIES_DEFAULT,
    max_llm_retries: int = MAX_LLM_RETRIES_DEFAULT,
    backoff_base: float = BACKOFF_BASE_SECONDS,
    trace: Trace | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LoopResult:
    """输入一个代码问题，输出一个通过契约校验的修改计划。"""
    trace = trace or Trace(
        meta={
            "adapter": getattr(adapter, "name", "?"),
            "model": getattr(adapter, "model", "?"),
            "repo": str(repo_root),
            "issue_chars": len(issue),
        }
    )
    trace.meta.setdefault("adapter", getattr(adapter, "name", "?"))
    trace.meta.setdefault("model", getattr(adapter, "model", "?"))
    tools = tools or ToolHub(repo_root)
    repo_summary = scan_repo(repo_root)
    messages: list[dict] = [
        {"role": "system", "content": render_system_prompt(repo_summary, max_tool_rounds=max_tool_rounds)},
        {"role": "user", "content": issue},
    ]
    trace.log("run_start", adapter=trace.meta["adapter"], model=trace.meta["model"], repo=str(repo_root))

    schema_retries = 0
    tool_rounds = 0

    try:
        while True:
            raw = _complete_with_retry(
                adapter, messages, trace,
                max_retries=max_llm_retries, backoff_base=backoff_base, sleep=sleep,
            )

            parsed = extract_json_object(raw)

            # 分支 1：模型请求工具，执行后回填结果继续循环
            if parsed is not None and parsed.get("type") == "tool_call":
                if tool_rounds >= max_tool_rounds:
                    return _fail(
                        trace, messages, "max_tool_rounds",
                        f"工具调用已达上限 {max_tool_rounds} 轮仍未给出计划，停止运行",
                    )
                name = str(parsed.get("name"))
                arguments = parsed.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                trace.log("tool_start", name=name, arguments=arguments)
                try:
                    result_text = tools.run(name, arguments)
                    is_error = False
                except ToolError as exc:
                    result_text, is_error = f"工具调用失败：{exc}", True
                trace.log("tool_end", name=name, is_error=is_error, result_chars=len(result_text))
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": f"[toolResult] {name}\n{result_text}"})
                tool_rounds += 1
                continue

            # 分支 2：模型给出计划（或无法解析），做契约校验
            if parsed is None:
                error_feedback = (
                    "输出不符合契约，具体字段错误如下：\n"
                    "- (根对象): 输出必须是单个 JSON 对象（工具请求或修改计划），不要输出自然语言"
                )
            else:
                try:
                    plan = PlanContract.model_validate(parsed)
                except ValueError as exc:
                    # pydantic 的 ValidationError 是 ValueError 子类，统一取字段级错误文本
                    error_feedback = format_validation_errors(exc)  # type: ignore[arg-type]
                else:
                    trace.log("plan", **plan.model_dump())
                    trace.log("run_end", status="ok", tool_rounds=tool_rounds, schema_retries=schema_retries)
                    return LoopResult(
                        status="ok", plan=plan, failure=None, trace=trace, messages=messages,
                    )

            if schema_retries >= max_schema_retries:
                return _fail(
                    trace, messages, "schema_retries_exhausted",
                    f"Schema 校验失败重试已达上限 {max_schema_retries} 次：{error_feedback}",
                )
            trace.log("schema_error", attempt=schema_retries, feedback=error_feedback)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"{error_feedback}\n请只输出修正后的 JSON。"})
            schema_retries += 1
    except LLMError as exc:
        # 429 / 超时 / 拒答等重试耗尽或不可重试：已记录 llm_error，这里收口
        return _fail(trace, messages, exc.kind, exc.message)
