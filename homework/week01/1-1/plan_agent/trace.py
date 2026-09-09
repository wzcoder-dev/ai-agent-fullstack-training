"""运行留痕。

验收要求「失败可定位」：每次 LLM 调用、错误、重试、工具执行、
Schema 校验失败与最终结论都按时间线记录，可整体落成 JSON 文件，
eval 或 CLI 运行失败时直接看 trace 定位是哪一步、什么原因。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Trace:
    def __init__(self, *, meta: dict | None = None) -> None:
        self.meta = meta or {}
        self.events: list[dict] = []
        self._started = time.perf_counter()

    def log(self, event_type: str, **fields) -> None:
        elapsed_ms = int((time.perf_counter() - self._started) * 1000)
        self.events.append({"elapsed_ms": elapsed_ms, "type": event_type, **fields})

    def to_dict(self) -> dict:
        return {"meta": self.meta, "events": self.events}

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def count(self, event_type: str) -> int:
        return sum(1 for event in self.events if event["type"] == event_type)
