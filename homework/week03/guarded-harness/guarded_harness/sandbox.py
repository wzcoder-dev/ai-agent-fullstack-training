"""应用层执行边界。不是操作系统隔离；测试代码必须来自可信仓库。"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

IGNORED = {'.git', '.venv', '__pycache__', '.pytest_cache'}
SENSITIVE = {'.env', '.ssh', '.aws', '.git', '.venv', 'secrets'}


class BoundaryError(ValueError):
    pass


class Sandbox:
    def __init__(self, root: Path, timeout=10.0, output_limit=12000):
        self.root = root.resolve()
        self.timeout, self.output_limit = timeout, output_limit
        self.cancel = threading.Event()
        self.last_pid = None

    def path(self, name: str, write=False) -> Path:
        """同时检查文本路径和真实落点；拒绝所有软链接，避免链接到敏感文件。"""
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            raise BoundaryError('PATH_ESCAPE')
        if any(p in SENSITIVE or p.startswith('.env') for p in rel.parts):
            raise BoundaryError('SENSITIVE_PATH')
        candidate = self.root / rel
        for part in [candidate, *candidate.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise BoundaryError('SYMLINK_DENIED')
        if not candidate.resolve().is_relative_to(self.root):
            raise BoundaryError('PATH_ESCAPE')
        if write and (rel.suffix != '.py' and not (rel.parts[0] == 'artifacts' and rel.suffix == '.md')):
            raise BoundaryError('WRITE_SCOPE')
        return candidate

    def manifest(self, sources_only=False):
        """哈希用于失效判断；不把文件内容或密钥放入审计。"""
        files = {}
        for base, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED)
            for name in sorted(names + [d for d in dirs if (Path(base) / d).is_symlink()]):
                path = Path(base) / name
                rel = path.relative_to(self.root).as_posix()
                if sources_only and rel.startswith('artifacts/'):
                    continue
                files[rel] = ('link:' + os.readlink(path)) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def revision(self):
        from .state_store import digest
        return digest(self.manifest(sources_only=True))

    def read(self, path: str):
        target = self.path(path)
        with target.open('rb') as f:
            data = f.read(self.output_limit + 1)
        return {'text': data[:self.output_limit].decode(errors='replace'),
                'truncated': len(data) > self.output_limit}

    def write(self, path: str, content: str):
        target = self.path(path, write=True)
        if len(content.encode()) > 100_000:
            raise BoundaryError('WRITE_TOO_LARGE')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {'path': path, 'sha256': hashlib.sha256(content.encode()).hexdigest()}

    def run_test(self, command='pytest -q'):
        # 完整匹配命令，不使用 shell，不接受附加参数或任意解释器代码。
        if command != 'pytest -q':
            raise BoundaryError('COMMAND_DENIED')
        env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
               'PYTHONPATH': str(self.root), 'LANG': 'C.UTF-8'}
        process = subprocess.Popen([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider'],
                                   cwd=self.root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.last_pid = process.pid
        chunks, kept, total = [], 0, 0

        def drain():
            nonlocal kept, total
            # 持续排空管道防止死锁；内存只保存限定字节，后续输出丢弃。
            while block := process.stdout.read(4096):
                total += len(block)
                part = block[:max(0, self.output_limit - kept)]
                chunks.append(part) if part else None
                kept += len(part)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline, reason = time.monotonic() + self.timeout, None
        try:
            while process.poll() is None:
                if self.cancel.is_set():
                    reason = 'CANCELLED'
                    break
                if time.monotonic() >= deadline:
                    reason = 'TIMEOUT'
                    break
                time.sleep(.01)
        finally:
            # 杀进程组，包含测试派生的子进程；正常退出也回收遗留后台进程。
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            reader.join(timeout=2)
            process.stdout.close()
        return dict(returncode=process.returncode, reason=reason,
                    output=b''.join(chunks).decode(errors='replace'), truncated=total > kept)
