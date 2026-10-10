#!/usr/bin/env python3
"""callfans 应用入口（PyInstaller 打包目标）。

无参数启动 = 打开 Web 控制台（桌面应用体验：serve + 浏览器）。
其他用法与 CLI 一致：status / query / memory / plan / maintenance / serve。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 桌面应用形态默认 SQLite 持久化（~/.callfans/callfans.db）；
# 已显式设置环境变量时不覆盖。python -m kb CLI 不受影响（默认 memory）。
os.environ.setdefault("CALLFANS_STORE", "sqlite")

# 排障模式：CALLFANS_DEBUG=1 时 10 秒后自动打印卡住的调用栈并退出
if os.environ.get("CALLFANS_DEBUG"):
    import faulthandler
    faulthandler.dump_traceback_later(10, exit=True)

from kb.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "serve"]
    main()
