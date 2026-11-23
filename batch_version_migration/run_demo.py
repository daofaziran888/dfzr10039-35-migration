#!/usr/bin/env python3
"""端到端演示入口：在隔离目录中完成 v1->v2 迁移、双版本投递与回滚核对。

用法：
    python3 -I run_demo.py                # 默认 runtime/
    python3 -I run_demo.py /path/to/rt   # 自定义运行目录
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from bvm.demo import run  # noqa: E402


def main() -> int:
    runtime_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "runtime"
    )
    context = run(runtime_dir)
    book = context["book"]
    print(book.render_text())
    print()
    print(f"运行目录: {os.path.abspath(runtime_dir)}")
    print(f"核对报告: {os.path.abspath(context['report_path'])}")
    print(f"转换清单: {os.path.abspath(context['manifest_path'])}")
    print(f"账本事件: {os.path.abspath(runtime_dir)}/delivery/events.jsonl")
    return 0 if book.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
