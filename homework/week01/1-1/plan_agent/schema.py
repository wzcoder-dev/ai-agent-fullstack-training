"""修改计划的输出契约。

契约来自作业要求：
- action ∈ {edit, ask_user, finish}
- file_path: string?，必须是被分析仓库内的相对路径
- reason: 1-500 字，说明为什么修改
- risk ∈ {low, medium, high}

校验失败时用 format_validation_errors 生成「具体到字段」的错误文本，
由 loop 回传给模型重试。额外字段按 pydantic 默认行为忽略，避免模型多输出
一个无害的 summary 就触发重试。
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ValidationError, model_validator

Action = Literal["edit", "ask_user", "finish"]
Risk = Literal["low", "medium", "high"]

REASON_MIN_CHARS = 1
REASON_MAX_CHARS = 500

CONTRACT_DESCRIPTION = """\
{
  "action": "edit | ask_user | finish",
  "file_path": "字符串或 null；action=edit 时必填，且必须是仓库内相对路径",
  "reason": "1-500 字，说明为什么这样修改",
  "risk": "low | medium | high"
}"""


class PlanContract(BaseModel):
    action: Action
    file_path: str | None = None
    reason: str
    risk: Risk

    @model_validator(mode="after")
    def _check_contract(self) -> PlanContract:
        if self.action == "edit" and not (self.file_path and self.file_path.strip()):
            raise ValueError("action=edit 时必须给出 file_path，指明要修改哪个文件")
        if self.file_path is not None:
            _validate_relative_path(self.file_path)
        reason_len = len(self.reason.strip())
        if not REASON_MIN_CHARS <= reason_len <= REASON_MAX_CHARS:
            raise ValueError(
                f"reason 长度必须在 {REASON_MIN_CHARS}-{REASON_MAX_CHARS} 字之间，当前 {reason_len} 字"
            )
        return self


def _validate_relative_path(path: str) -> None:
    """file_path 必须是仓库内相对路径：拒绝绝对路径、~、反斜杠与 .. 越界。"""
    p = path.strip()
    if not p:
        raise ValueError("file_path 不能是空字符串，请填写仓库内相对路径，或填 null")
    if PurePosixPath(p).is_absolute() or PureWindowsPath(p).is_absolute() or p.startswith("~"):
        raise ValueError(f"file_path 必须是仓库内相对路径，收到的是绝对路径: {p!r}")
    if "\\" in p:
        raise ValueError(f"file_path 请使用 / 作为路径分隔符: {p!r}")
    parts = PurePosixPath(p).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"file_path 不允许包含 .. 跳出仓库: {p!r}")
    if PurePosixPath(p) == PurePosixPath("."):
        raise ValueError("file_path 不能指向仓库根目录本身，请指明具体文件")


def format_validation_errors(exc: ValidationError) -> str:
    """把校验异常压成「字段: 错误」列表，回传给模型时能定位到具体字段。"""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(根对象)"
        lines.append(f"- {loc}: {err['msg']}")
    return "输出不符合契约，具体字段错误如下：\n" + "\n".join(lines)


def render_plan(plan: PlanContract) -> str:
    """把计划渲染成给人看的文本。"""
    file_path = plan.file_path or "-"
    return (
        f"action: {plan.action}\n"
        f"file_path: {file_path}\n"
        f"risk: {plan.risk}\n"
        f"reason: {plan.reason}"
    )
