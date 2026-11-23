"""构造 v1 旧采集批次样本。

v1 存储模型（摘要 + 字段）的已知缺陷：
1. 字段值是裸 JSON，没有精确类型标注，下游容易把 false / 0 / 空串 / null 混成一个“空值”；
2. 来源是自由文本，历史上允许空串或缺失，无法确认来源的数据没有单独留痕；
3. 批次摘要是独立维护的计数，可能与实际明细范围不一致（本样本故意制造该冲突）。

样本批次 B-2026-1108-01 共 6 条明细，覆盖全部边界值；
摘要里声明 5 条（漏掉 R006），用于演示“新旧格式验证旧摘要的范围冲突”。
"""
from __future__ import annotations

from typing import Any

from .common import content_hash, ensure_dir, write_json_once, write_jsonl

BATCH_ID = "B-2026-1108-01"
SOURCE_JOB_ID = "JOB-7781"
COLLECTED_AT = "2026-11-08T09:30:00Z"

# v1 摘要声称的字段范围与计数（注意 declared_records 故意少一条）
V1_SUMMARY: dict[str, Any] = {
    "summary_version": "v1",
    "declared_record_count": 5,          # 实际明细 6 条 -> 范围冲突
    "declared_records": [f"R00{i}" for i in range(1, 6)],
    "field_scope": ["title", "flag", "score", "tags", "note", "code"],
    "non_empty_flag_count": 3,           # 旧口径把 false 当“空”统计，口径本身含糊
}

# 裸字段值，刻意区分 false / 0 / 空串 / 空数组 / null / 字段缺失
V1_RECORDS: list[dict[str, Any]] = [
    {
        "record_id": "R001",
        "source": "https://source-a.example.com/items/1",
        "fields": {
            "title": "常温商品A",
            "flag": False,            # 布尔 false，绝不能回填成 0
            "score": 0,               # 数字 0，绝不能回填成 false
            "tags": [],               # 空数组，不能与空串混同
            "note": "",               # 空字符串
            "code": 0,
        },
    },
    {
        "record_id": "R002",
        "source": "https://source-a.example.com/items/2",
        "fields": {
            "title": "促销商品B",
            "flag": True,
            "score": 12,
            "tags": ["生鲜", "满减"],
            "note": "每日 10 点更新",
            "code": 200,
        },
    },
    {
        "record_id": "R003",
        # 来源历史上没能确认：空串。原材料必须原样保留，不得伪造来源
        "source": "",
        "fields": {
            "title": "来源缺失商品C",
            "flag": False,
            "score": 0,
            "tags": [],
            "note": None,             # 显式 null，与“字段缺失”不是一回事
            "code": 500,
        },
    },
    {
        "record_id": "R004",
        # 整条没有 source 键，同样属于来源未知
        "fields": {
            "title": "来源缺失商品D",
            "flag": True,
            "score": 0,               # 又是 0 与 true 同框，防止混值
            "tags": [""],             # 含一个空串的数组，与空数组区分
            "note": "渠道下线，来源待补",
            "code": 0,
        },
    },
    {
        "record_id": "R005",
        "source": "https://source-b.example.com/items/5",
        "fields": {
            "title": "预售商品E",
            "flag": False,            # false
            "score": 0.75,
            "tags": ["预售"],
            # note 键整体缺失 -> v2 必须标注 missing 而不是 null
            "code": 404,
        },
    },
    {
        "record_id": "R006",            # 摘要漏统的“范围外”明细
        "source": "",
        "fields": {
            "title": "补录商品F",
            "flag": False,
            "score": 0,
            "tags": [],
            "note": "",
            "code": 0,
        },
    },
]


def build_v1_batch() -> dict[str, Any]:
    """组装 v1 批次文档（不写盘），并为每条明细补 v1 内容哈希。"""
    records: list[dict[str, Any]] = []
    for record in V1_RECORDS:
        stored = dict(record)
        stored["record_hash_v1"] = content_hash(record)  # 对原始记录（不含哈希）取哈希
        records.append(stored)

    return {
        "batch_id": BATCH_ID,
        "schema_version": "v1",
        "source_job_id": SOURCE_JOB_ID,
        "collected_at": COLLECTED_AT,
        "summary": V1_SUMMARY,
        "records": records,
    }


def write_v1_batch(runtime_dir: str) -> dict[str, Any]:
    """把 v1 批次落到 runtime 目录（只写一次，历史不可变）。

    返回的字典包含文档本身与工件路径，供迁移与核对使用。
    """
    batch = build_v1_batch()
    batch_dir = ensure_dir(f"{runtime_dir}/batches/{BATCH_ID}/v1")
    doc_path = f"{batch_dir}/batch_v1.json"
    records_path = f"{batch_dir}/records_v1.jsonl"
    write_json_once(doc_path, batch)
    write_jsonl(records_path, batch["records"])
    return {
        "batch": batch,
        "doc_path": doc_path,
        "records_path": records_path,
    }
