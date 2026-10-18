"""只追加的本地审计日志。哈希链可发现修改，但不等于外部可信防篡改存储。"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from .state_store import digest


class Audit:
    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self.previous = '0' * 64
        if path.exists():
            for row in path.read_text().splitlines():
                item = json.loads(row)
                checksum = item.pop('hash')
                if item['previous'] != self.previous or checksum != digest(item):
                    raise ValueError('审计链校验失败')
                self.previous = checksum

    def record(self, rule, decision, tool_call_id=None, actor='program', **details):
        item = dict(time=datetime.now(timezone.utc).isoformat(), run_id=self.run_id,
                    rule=rule, decision=decision, actor=actor, tool_call_id=tool_call_id,
                    previous=self.previous, **details)
        item['hash'] = digest(item)
        with self.path.open('a') as f:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        self.previous = item['hash']
        return item
