#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无需安装的启动入口（适合服务器直接运行）

用法：
  python run_service.py
"""

import os
import sys


def main():
    repo_root = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(repo_root, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # 切到项目根，确保 config.json / 状态文件 / 锁文件 等相对路径在 cron/systemd 下也能解析
    os.chdir(repo_root)

    from carinfo.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()

