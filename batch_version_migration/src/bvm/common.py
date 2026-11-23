"""公共工具：规范化 JSON、SHA-256 哈希、只写一次落盘。

所有哈希都基于 ``canonical_json`` —— 键排序、无空白、UTF-8 原文，
保证同一条记录在迁移、投递、复核三个环节算出的哈希完全一致。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """UTC 时间戳，毫秒精度、带 Z 后缀，字典序即可按时间排序。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def canonical_json(obj: Any) -> str:
    """规范化序列化：sort_keys + 紧凑分隔符，中文不转义。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(obj: Any) -> str:
    """对任意可 JSON 化对象算内容哈希（SHA-256，十六进制）。"""
    return sha256_text(canonical_json(obj))


def short_hash(obj: Any, length: int = 16) -> str:
    return content_hash(obj)[:length]


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def write_once(path: str, data: str | bytes) -> None:
    """只写一次：目标已存在即报错。

    历史 v1 工件、转换清单、投递凭证都不允许静默覆盖，
    否则回滚后将无法复核切换期的真实内容。
    """
    if os.path.exists(path):
        raise FileExistsError(f"工件已存在，禁止覆盖: {path}")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as fh:  # type: ignore[call-overload]
        fh.write(data)


def write_json_once(path: str, obj: Any) -> None:
    write_once(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_jsonl(path: str, rows: list[Any]) -> None:
    """整批写 JSONL（用于一次性生成的 v1/v2 记录工件）。"""
    write_once(
        path,
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
    )


def read_jsonl(path: str) -> list[Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_jsonl(path: str, row: Any) -> None:
    """向追加型日志（投递账本）写一行。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
