"""验证脚本使用：真的退出进程，制造副作用已发生而结果未落盘的窗口。"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guarded_harness.harness import Harness

h = Harness(Path(sys.argv[1]), Path(sys.argv[2]), resume=True)

def crash_after_write(tool):
    if tool == 'write_file':
        # 不运行 finally，不保存 result，模拟进程突然消失。
        os._exit(73)

h.after_effect = crash_after_write
h.approve(h.store.data['pending']['id'], actor='crash-demo-script')
