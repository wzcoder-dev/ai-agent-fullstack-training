"""SQLite 持久化：调用审计 traces 与流式 checkpoint stream_events。

课程 1-6 的内存 CALL_TRACES 升级为落库；
阻塞的 sqlite3 通过 asyncio.to_thread 参与事件循环（课程 1-1 同模式），
单连接 + 线程锁保证跨线程安全。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.schemas import CallTrace, UsageSummaryRow

_TRACE_COLUMNS = (
    "request_id, ts, key_id, requested_model, actual_model, prompt_name, prompt_version, "
    "input_tokens, output_tokens, cached_tokens, cost_usd, latency_ms, ttft_ms, attempts, status, error_code"
)

_DDL = """
CREATE TABLE IF NOT EXISTS traces (
    request_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    key_id TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    actual_model TEXT,
    prompt_name TEXT,
    prompt_version TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ttft_ms INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces (ts DESC);
CREATE TABLE IF NOT EXISTS stream_events (
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (request_id, seq)
);
"""


class UsageStore:
    # 建表在构造期同步完成（快），查询与写入经 to_thread 异步执行。
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, operation: Any) -> Any:
        # operation(conn) 在锁内于工作线程执行。
        def guarded() -> Any:
            with self._lock:
                return operation(self._conn)

        return await asyncio.to_thread(guarded)

    async def record_trace(self, trace: CallTrace) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO traces ({_TRACE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trace.request_id,
                    trace.timestamp.isoformat(),
                    trace.key_id,
                    trace.requested_model,
                    trace.actual_model,
                    trace.prompt_name,
                    trace.prompt_version,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.cached_tokens,
                    trace.cost_usd,
                    trace.latency_ms,
                    trace.ttft_ms,
                    trace.attempts,
                    trace.status,
                    trace.error_code,
                ),
            )
            conn.commit()

        await self._run(operation)

    async def list_traces(self, limit: int = 50, offset: int = 0) -> tuple[list[CallTrace], int]:
        def operation(conn: sqlite3.Connection) -> tuple[list[tuple], int]:
            total = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            rows = conn.execute(
                f"SELECT {_TRACE_COLUMNS} FROM traces ORDER BY ts DESC, request_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return rows, total

        rows, total = await self._run(operation)
        traces = [
            CallTrace(
                request_id=row[0],
                timestamp=datetime.fromisoformat(row[1]),
                key_id=row[2],
                requested_model=row[3],
                actual_model=row[4],
                prompt_name=row[5],
                prompt_version=row[6],
                input_tokens=row[7],
                output_tokens=row[8],
                cached_tokens=row[9] or 0,
                cost_usd=row[10],
                latency_ms=row[11],
                ttft_ms=row[12],
                attempts=row[13],
                status=row[14],
                error_code=row[15],
            )
            for row in rows
        ]
        return traces, total

    async def append_stream_event(self, request_id: str, seq: int, event_json: str) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO stream_events (request_id, seq, event_json, created_at) VALUES (?,?,?,?)",
                (request_id, seq, event_json, datetime.now(UTC).isoformat()),
            )
            conn.commit()

        await self._run(operation)

    async def list_stream_events(self, request_id: str, after_seq: int) -> list[tuple[int, str]]:
        def operation(conn: sqlite3.Connection) -> list[tuple[int, str]]:
            rows = conn.execute(
                "SELECT seq, event_json FROM stream_events WHERE request_id = ? AND seq > ? ORDER BY seq",
                (request_id, after_seq),
            ).fetchall()
            return [(row[0], row[1]) for row in rows]

        return await self._run(operation)

    async def usage_summary(self, group_by: Literal["model", "key"]) -> list[UsageSummaryRow]:
        # 成功调用口径：失败调用的 token 数不可靠，不计入用量与成本。
        column = "actual_model" if group_by == "model" else "key_id"

        def operation(conn: sqlite3.Connection) -> list[tuple]:
            query = (
                f"SELECT {column} AS grp, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) "
                "FROM traces WHERE status = 'success' AND actual_model IS NOT NULL "
                f"GROUP BY {column} ORDER BY COUNT(*) DESC"
            )
            return conn.execute(query).fetchall()

        rows = await self._run(operation)
        return [
            UsageSummaryRow(
                group=row[0],
                requests=row[1],
                input_tokens=row[2] or 0,
                output_tokens=row[3] or 0,
                cost_usd=row[4] or 0.0,
            )
            for row in rows
        ]
