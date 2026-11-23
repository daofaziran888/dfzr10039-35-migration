#!/usr/bin/env python3
"""无 pytest 环境下的独立测试运行器：发现 tests/ 下 test_ 函数并执行。"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback


def _load_module(path: str):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root, "src"))
    tests_dir = os.path.join(root, "tests")

    failures: list[str] = []
    total = 0
    for fname in sorted(os.listdir(tests_dir)):
        if not fname.startswith("test_") or not fname.endswith(".py"):
            continue
        module = _load_module(os.path.join(tests_dir, fname))
        for test_name in sorted(dir(module)):
            if not test_name.startswith("test_"):
                continue
            total += 1
            try:
                getattr(module, test_name)()
                print(f"PASS {fname}::{test_name}")
            except Exception:  # noqa: BLE001
                failures.append(f"{fname}::{test_name}")
                print(f"FAIL {fname}::{test_name}")
                traceback.print_exc()

    print()
    print(f"{total - len(failures)}/{total} 测试通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
