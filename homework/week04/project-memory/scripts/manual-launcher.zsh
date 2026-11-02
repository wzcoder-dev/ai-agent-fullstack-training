#!/bin/zsh
set -eu
# :A 先解析符号链接；不能按 runs/start-manual.command 所在目录定位样本。
RUN_DIR="${0:A:h}"
PM_ROOT="$(cd "$RUN_DIR/../.." && pwd)"
cd "$RUN_DIR/login-project"
export PATH="$PM_ROOT/.venv/bin:$PM_ROOT/node_modules/.bin:$PATH"
export PI_TELEMETRY=0
SESSION_DIR="$PM_ROOT/artifacts/${RUN_DIR:t}/sessions"
exec "$PM_ROOT/node_modules/.bin/pi" \
  --no-extensions --no-skills --no-prompt-templates \
  --no-context-files --no-themes \
  --provider deepseek --model deepseek-flash --thinking off \
  --session-dir "$SESSION_DIR" \
  -e "$PM_ROOT/src/extension.ts" "$@"
