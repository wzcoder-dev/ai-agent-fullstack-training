import os
import threading
import time
import pytest
from guarded_harness.sandbox import BoundaryError
from conftest import call, records


@pytest.mark.parametrize('path', ['artifacts/../../.env', '../outside', '/etc/passwd', '.env', '.ssh/id_rsa', '.env.local'])
def test_s1_escape_and_sensitive_denied(h, path, monkeypatch):
    monkeypatch.setattr(h, 'handle', lambda *a: pytest.fail('handler 被调用'))
    assert not call(h, 'read_file', {'path': path})['ok']
    assert records(h)[-1]['decision'] == 'DENY'


def test_s2_symlink_denied_and_audited(h, tmp_path):
    outside = tmp_path / 'secret'
    outside.write_text('private')
    (h.sandbox.root / 'link.txt').symlink_to(outside)
    assert call(h, 'read_file', {'path': 'link.txt'})['rule'] == 'SYMLINK_DENIED'
    assert records(h)[-1]['rule'] == 'SYMLINK_DENIED'


@pytest.mark.parametrize('command', ['pytest -q; echo BAD', 'env', 'python -c "print(1)"', 'pytest -q -x'])
def test_s3_command_allowlist(h, command, monkeypatch):
    monkeypatch.setattr(h, 'handle', lambda *a: pytest.fail('命令不应执行'))
    assert call(h, 'run_test', {'command': command})['rule'] == 'COMMAND_DENIED'


def slow_test(h):
    (h.sandbox.root / 'tests/test_calc.py').write_text('import time\n\ndef test_slow():\n    time.sleep(30)\n')


def test_s4_timeout_really_kills_process(h):
    slow_test(h)
    h.sandbox.timeout = .3
    result = h.sandbox.run_test()
    assert result['reason'] == 'TIMEOUT'
    with pytest.raises(ProcessLookupError):
        os.kill(h.sandbox.last_pid, 0)


def test_s4_cancellation_really_kills_process(h):
    slow_test(h)
    timer = threading.Timer(.3, h.sandbox.cancel.set)
    timer.start()
    result = h.sandbox.run_test()
    timer.join()
    assert result['reason'] == 'CANCELLED'
    with pytest.raises(ProcessLookupError):
        os.kill(h.sandbox.last_pid, 0)


def test_environment_is_scrubbed_and_output_bounded(h, monkeypatch):
    monkeypatch.setenv('HARNESS_TEST_SECRET', 'do-not-inherit')
    (h.sandbox.root / 'tests/test_calc.py').write_text(
        'import os\nprint("x" * 200000)\n\ndef test_env():\n    assert "HARNESS_TEST_SECRET" not in os.environ\n')
    # pytest 的捕获不会把成功用例输出交给父进程，因此直接在收集时报错使输出溢出。
    with (h.sandbox.root / 'tests/test_calc.py').open('a') as f:
        f.write('\nraise RuntimeError("z" * 100000)\n')
    h.sandbox.output_limit = 1024
    result = h.sandbox.run_test()
    assert len(result['output'].encode()) <= 1024
    assert result['truncated']


def test_child_process_is_killed_with_group(h):
    (h.sandbox.root / 'tests/test_calc.py').write_text(
        'import subprocess, sys, time\nfrom pathlib import Path\n'
        'def test_child():\n'
        '    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        '    Path("child.pid").write_text(str(p.pid))\n'
        '    time.sleep(30)\n')
    h.sandbox.timeout = 1
    assert h.sandbox.run_test()['reason'] == 'TIMEOUT'
    pid = int((h.sandbox.root / 'child.pid').read_text())
    # macOS 上孤儿子进程回收由系统异步完成；检查进程状态而非只查 PID。
    import subprocess
    status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith('Z')


def test_test_process_does_not_inherit_secret(h, monkeypatch):
    monkeypatch.setenv('HARNESS_TEST_SECRET', 'do-not-inherit')
    (h.sandbox.root / 'tests/test_calc.py').write_text(
        'import os\n\ndef test_env():\n    assert "HARNESS_TEST_SECRET" not in os.environ\n')
    assert h.sandbox.run_test()['returncode'] == 0
