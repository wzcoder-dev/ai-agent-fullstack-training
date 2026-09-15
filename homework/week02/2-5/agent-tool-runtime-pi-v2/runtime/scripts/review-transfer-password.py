"""Actual CLI password-input acceptance check using a PTY and an isolated database.

Run from runtime: python3 scripts/review-transfer-password.py
Only the public demo password is used. Captured terminal text is not persisted.
"""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import time


RUNTIME = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="transfer-review-pty-") as folder:
        config_path = str(Path(folder) / "platform.yaml")
        # Generate a temporary config through the already installed YAML library.
        subprocess.run([
            "node", "--input-type=module", "-e",
            "import{readFileSync,writeFileSync}from'node:fs';"
            "import{resolve}from'node:path';import{parse,stringify}from'yaml';"
            "const c=parse(readFileSync('../config/platform.yaml','utf8'));"
            "c.runtime.database=process.argv[2];"
            "c.tools.order_schema=resolve('../config',c.tools.order_schema);"
            "writeFileSync(process.argv[1],stringify(c));",
            config_path, str(Path(folder) / "test.db"),
        ], cwd=RUNTIME, check=True, capture_output=True)
        cli = ["node", "--import", "tsx", "src/cli.ts", "--config", config_path]
        subprocess.run(cli + ["accounts", "init"], cwd=RUNTIME, check=True, capture_output=True)
        submitted = subprocess.run(cli + ["transfer", "submit", "A", "B", "100", "--key", "pty-review"], cwd=RUNTIME, check=True, capture_output=True, text=True)
        request_id = json.loads(submitted.stdout)["content"]["transfer_request_id"]
        master, slave = pty.openpty()
        process = subprocess.Popen(cli + ["transfer", "auth", request_id], cwd=RUNTIME, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        output = bytearray()
        sent = False
        started = time.monotonic()
        try:
            while time.monotonic() - started < 10:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                # Give Node time to start. A working implementation should show a prompt.
                if not sent and ("支付密码".encode() in output or time.monotonic() - started > 1.5):
                    os.write(master, b"123456\n")
                    sent = True
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        observed = {
            "password_echoed": b"123456" in output,
            "prompt_visible": "支付密码".encode() in output,
            "transfer_succeeded": b"SUCCEEDED" in output,
            "exit_code": process.returncode,
        }
        print(json.dumps(observed))
        return 0 if not observed["password_echoed"] and observed["prompt_visible"] and observed["transfer_succeeded"] and observed["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
