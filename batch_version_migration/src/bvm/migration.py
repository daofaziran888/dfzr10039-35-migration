"""v1 -> v2 迁移引擎与批次版本转换清单。

设计红线：
- 回填全程只按“精确类型”分派，**禁止任何真值判断**（``if x`` 会把
  false / 0 / 空串 / 空数组统统判成假），因此类型分派用 ``isinstance`` 与
  ``is None``、键存在性判断完成；
- 来源无法确认时不伪造、不填默认值，只把 v1 原材料原样挂到
  ``source.raw_material`` 并标 ``unknown``；
- 转换不删除、不改写 v1 工件，所有映射都带双侧内容哈希，支持反向逐值复核；
- 摘要与明细的范围冲突不静默吞掉，写进清单的 scope/anomalies。
"""
from __future__ import annotations

import os
from typing import Any

from .common import (
    canonical_json,
    content_hash,
    ensure_dir,
    read_json,
    read_jsonl,
    sha256_text,
    utc_now,
    write_json_once,
    write_jsonl,
)

MANIFEST_KIND = "batch_conversion_manifest"

# v2 精确类型标签
TYPE_BOOLEAN = "boolean"
TYPE_INTEGER = "integer"
TYPE_FLOAT = "float"
TYPE_STRING = "string"
TYPE_ARRAY = "array"
TYPE_NULL = "null"
TYPE_MISSING = "missing"  # v1 中该字段键整体缺失，与显式 null 严格区分

# 字段缺失哨兵；只在内存里用，落盘时写成 {"type": "missing", "value": null}
MISSING = object()

# 固定的转换规则编号与说明，写进清单供复核方对照
CONVERSION_RULES: dict[str, Any] = {
    "rule_set_id": "CV-RULES-v1-to-v2-1.0",
    "type_dispatch": [
        "bool -> boolean（注意 bool 是 int 子类，必须先于 integer 判断）",
        "int -> integer",
        "float -> float",
        "str -> string（空串仍是 string，不转 null）",
        "list -> array（空数组仍是 array，不与空串混同）",
        "None -> null（显式 null）",
        "键缺失 -> missing（v1 根本没有该字段，不等同于 null）",
    ],
    "falsy_separation": "false / 0 / 空串 / 空数组 / null / 键缺失 六者各自独立，"
                        "回填禁止真值判断，禁止合并为同一个值",
    "source_policy": "source 为空串或键缺失 -> source.status=unknown，"
                     "原材料原样存入 source.raw_material，不伪造来源",
    "hash_policy": "v1 哈希取原始记录规范化 JSON；v2 哈希取转换后记录（不含自身哈希）",
    "immutability": "v1 工件只写一次，迁移不就地改写，历史可复核",
}


def classify_value(value: Any) -> str:
    """只按类型分派，不做任何真值判断。"""
    if value is None:
        return TYPE_NULL
    # bool 必须在 int 之前：Python 中 isinstance(True, int) 为真
    if isinstance(value, bool):
        return TYPE_BOOLEAN
    if isinstance(value, int):
        return TYPE_INTEGER
    if isinstance(value, float):
        return TYPE_FLOAT
    if isinstance(value, str):
        return TYPE_STRING
    if isinstance(value, list):
        return TYPE_ARRAY
    raise TypeError(f"v2 不支持的 v1 字段类型: {type(value)!r}")


def convert_fields(v1_fields: dict[str, Any], field_scope: list[str]) -> dict[str, Any]:
    """把 v1 裸字段转成 v2 精确类型字段。

    field_scope 来自 v1 摘要：摘要声称过、但明细里缺键的字段也要落一个
    missing 标注，保证“字段缺失”可被显式核对。
    """
    converted: dict[str, Any] = {}
    # 摘要范围 + 实际键取并集，避免摘要漏列字段时悄悄丢字段
    all_names = sorted(set(field_scope) | set(v1_fields.keys()))
    for name in all_names:
        if name not in v1_fields:
            converted[name] = {"type": TYPE_MISSING, "value": None}
            continue
        raw = v1_fields[name]
        converted[name] = {"type": classify_value(raw), "value": raw}
    return converted


def convert_source(v1_record: dict[str, Any]) -> dict[str, Any]:
    """来源转换：未知来源保留原材料，绝不编造。"""
    if "source" not in v1_record:
        return {
            "status": "unknown",
            "reason": "v1_record_missing_source_key",
            "value": None,
            "raw_material": {"source_key_present": False},
        }
    raw_source = v1_record["source"]
    if isinstance(raw_source, str) and raw_source.strip() == "":
        return {
            "status": "unknown",
            "reason": "v1_source_blank",
            "value": None,
            "raw_material": {"source_key_present": True, "source": raw_source},
        }
    return {
        "status": "confirmed",
        "reason": "v1_source_present",
        "value": raw_source,
        "raw_material": {"source_key_present": True, "source": raw_source},
    }


def convert_record(v1_record: dict[str, Any], field_scope: list[str]) -> dict[str, Any]:
    """单条 v1 明细 -> v2 明细（batch_id 与 v2 哈希由 migrate 统一回填）。"""
    v2_record: dict[str, Any] = {
        "batch_id": None,
        "record_id": v1_record["record_id"],
        "schema_version": "v2",
        "source": convert_source(v1_record),
        "fields": convert_fields(v1_record["fields"], field_scope),
        # v1 哈希在样本落盘时已对原始记录计算，直接引用保持同一口径
        "v1_record_hash": v1_record["record_hash_v1"],
    }
    v2_record["record_hash_v2"] = content_hash(
        _without_keys(v2_record, ("record_hash_v2",))
    )
    return v2_record


def _without_keys(obj: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if k not in keys}


def decode_v2_field(typed: dict[str, Any]) -> Any:
    """把 v2 字段还原成 Python 原值；missing 返回 MISSING 哨兵。"""
    if typed["type"] == TYPE_MISSING:
        return MISSING
    return typed["value"]


def reconcile_scope(v1_batch: dict[str, Any]) -> dict[str, Any]:
    """核对 v1 摘要声明范围与实际明细范围。"""
    declared = list(v1_batch["summary"]["declared_records"])
    actual = [r["record_id"] for r in v1_batch["records"]]
    declared_set, actual_set = set(declared), set(actual)
    declared_only = sorted(declared_set - actual_set)   # 摘要有、明细没有
    undeclared = sorted(actual_set - declared_set)      # 明细有、摘要漏统
    status = "match" if not declared_only and not undeclared else "conflict"
    return {
        "status": status,
        "declared_count": len(declared),
        "actual_count": len(actual),
        "declared_records": declared,
        "actual_records": actual,
        "declared_only": declared_only,
        "undeclared_records": undeclared,
    }


def _check_summary_metrics(v1_batch: dict[str, Any]) -> list[dict[str, Any]]:
    """复核摘要里的派生计数；旧口径含糊导致的不一致也留痕。"""
    anomalies: list[dict[str, Any]] = []
    true_flags = sum(
        1 for r in v1_batch["records"] if r["fields"].get("flag") is True
    )
    declared = v1_batch["summary"].get("non_empty_flag_count")
    if declared is not None and declared != true_flags:
        anomalies.append({
            "code": "SUMMARY_METRIC_MISMATCH",
            "field": "flag",
            "declared_non_empty_flag_count": declared,
            "recomputed_true_flag_count": true_flags,
            "note": "旧摘要把 false 归入“空”或漏统明细，计数与实际不符；"
                    "v2 以布尔精确类型重新计数，不沿用旧口径",
        })
    return anomalies


def migrate(v1_batch: dict[str, Any], runtime_dir: str) -> dict[str, Any]:
    """执行迁移：落 v2 工件 + 生成批次版本转换清单。

    返回清单字典（已含 manifest_hash）。
    """
    batch_id = v1_batch["batch_id"]
    field_scope = list(v1_batch["summary"]["field_scope"])
    converted_at = utc_now()

    # 1. 逐条转换
    v2_records: list[dict[str, Any]] = []
    for raw in v1_batch["records"]:
        v2 = convert_record(raw, field_scope)
        v2["batch_id"] = batch_id
        # 重算哈希，保证 batch_id 回填后哈希仍覆盖最终内容
        v2["record_hash_v2"] = content_hash(_without_keys(v2, ("record_hash_v2",)))
        v2_records.append(v2)

    # 2. 范围核对与异常收集
    scope = reconcile_scope(v1_batch)
    anomalies: list[dict[str, Any]] = []
    if scope["status"] == "conflict":
        anomalies.append({
            "code": "SCOPE_COUNT_CONFLICT",
            "declared_record_count": scope["declared_count"],
            "actual_record_count": scope["actual_count"],
            "undeclared_records": scope["undeclared_records"],
            "declared_only": scope["declared_only"],
            "note": "v1 摘要声明范围与实际明细不一致；迁移以实际明细为准，"
                    "冲突原样登记，不修改 v1 摘要",
        })

    # 3. 来源未知留痕 + 字段值形态统计
    mappings: list[dict[str, Any]] = []
    unknown_source_ids: list[str] = []
    for v1_rec, v2_rec in zip(v1_batch["records"], v2_records):
        if v2_rec["source"]["status"] == "unknown":
            unknown_source_ids.append(v2_rec["record_id"])
            anomalies.append({
                "code": "SOURCE_UNKNOWN",
                "record_id": v2_rec["record_id"],
                "reason": v2_rec["source"]["reason"],
                "raw_material": v2_rec["source"]["raw_material"],
                "note": "历史来源未能确认，保留 v1 原材料，v2 不写入任何猜测来源",
            })
        value_kinds = sorted({cell["type"] for cell in v2_rec["fields"].values()})
        mappings.append({
            "record_id": v2_rec["record_id"],
            "v1_record_hash": v2_rec["v1_record_hash"],
            "v2_record_hash": v2_rec["record_hash_v2"],
            "source_status": v2_rec["source"]["status"],
            "field_count": len(v2_rec["fields"]),
            "value_kinds": value_kinds,
        })

    anomalies.extend(_check_summary_metrics(v1_batch))

    # 4. 落 v2 工件（只写一次）
    v2_dir = ensure_dir(f"{runtime_dir}/batches/{batch_id}/v2")
    v2_records_path = f"{v2_dir}/records_v2.jsonl"
    v2_doc_path = f"{v2_dir}/batch_v2.json"
    write_jsonl(v2_records_path, v2_records)
    v2_doc = {
        "batch_id": batch_id,
        "schema_version": "v2",
        "source_job_id": v1_batch["source_job_id"],
        "collected_at": v1_batch["collected_at"],
        "converted_at": converted_at,
        "field_scope": sorted(set(field_scope) | {
            name for r in v1_batch["records"] for name in r["fields"].keys()
        }),
        "records": v2_records,
    }
    write_json_once(v2_doc_path, v2_doc)

    v1_doc_path = f"{runtime_dir}/batches/{batch_id}/v1/batch_v1.json"
    v1_records_path = f"{runtime_dir}/batches/{batch_id}/v1/records_v1.jsonl"

    # 5. 组装清单（先不含 manifest_hash），哈希对清单正文取
    manifest: dict[str, Any] = {
        "manifest_id": f"CV-{batch_id}",
        "kind": MANIFEST_KIND,
        "batch_id": batch_id,
        "from_version": "v1",
        "to_version": "v2",
        "source_job_id": v1_batch["source_job_id"],
        "collected_at": v1_batch["collected_at"],
        "converted_at": converted_at,
        "v1_artifact": {
            "batch_doc": os.path.relpath(v1_doc_path, runtime_dir),
            "records": os.path.relpath(v1_records_path, runtime_dir),
            "doc_hash": content_hash(v1_batch),
        },
        "v2_artifact": {
            "batch_doc": os.path.relpath(v2_doc_path, runtime_dir),
            "records": os.path.relpath(v2_records_path, runtime_dir),
            "doc_hash": content_hash(v2_doc),
        },
        "scope": scope,
        "anomalies": anomalies,
        "record_mappings": sorted(mappings, key=lambda m: m["record_id"]),
        "unknown_source_records": sorted(unknown_source_ids),
        "summary": {
            "total_records": len(v2_records),
            "confirmed_source_records": len(v2_records) - len(unknown_source_ids),
            "unknown_source_records": len(unknown_source_ids),
            "typed_field_cells": sum(len(r["fields"]) for r in v2_records),
            "anomaly_count": len(anomalies),
        },
        "conversion_rules": CONVERSION_RULES,
    }
    manifest["manifest_hash"] = sha256_text(
        canonical_json({k: v for k, v in manifest.items() if k != "manifest_hash"})
    )

    manifest_path = f"{runtime_dir}/manifests/CV-{batch_id}.json"
    write_json_once(manifest_path, manifest)
    return manifest


def verify_manifest(manifest: dict[str, Any], runtime_dir: str) -> list[str]:
    """复核清单：返回问题列表，空列表表示全部通过。

    检查项：
    1. 清单正文哈希（防篡改）；
    2. v1/v2 工件哈希与清单登记一致；
    3. 每条映射双侧记录哈希可重算；
    4. 反向逐值核对：v2 解出来的字段值/类型与 v1 原材料完全一致；
    5. 来源未知记录的原材料与 v1 一致；
    6. 范围核对结果可复算。
    """
    issues: list[str] = []
    batch_id = manifest["batch_id"]

    # 1. 清单自哈希
    stored_hash = manifest.get("manifest_hash")
    recomputed = sha256_text(
        canonical_json({k: v for k, v in manifest.items() if k != "manifest_hash"})
    )
    if stored_hash != recomputed:
        issues.append(f"清单哈希不一致: stored={stored_hash} recomputed={recomputed}")

    # 2. 工件哈希
    v1_doc = read_json(f"{runtime_dir}/{manifest['v1_artifact']['batch_doc']}")
    v2_doc = read_json(f"{runtime_dir}/{manifest['v2_artifact']['batch_doc']}")
    if content_hash(v1_doc) != manifest["v1_artifact"]["doc_hash"]:
        issues.append("v1 工件哈希与清单登记不符")
    if content_hash(v2_doc) != manifest["v2_artifact"]["doc_hash"]:
        issues.append("v2 工件哈希与清单登记不符")

    v1_records = read_jsonl(f"{runtime_dir}/{manifest['v1_artifact']['records']}")
    v2_records = read_jsonl(f"{runtime_dir}/{manifest['v2_artifact']['records']}")
    v1_by_id = {r["record_id"]: r for r in v1_records}
    v2_by_id = {r["record_id"]: r for r in v2_records}

    # 3/4/5. 逐记录双向核对
    for mapping in manifest["record_mappings"]:
        rid = mapping["record_id"]
        v1r = v1_by_id.get(rid)
        v2r = v2_by_id.get(rid)
        if v1r is None or v2r is None:
            issues.append(f"记录 {rid} 在 v1/v2 工件中缺失")
            continue
        if v1r["record_hash_v1"] != mapping["v1_record_hash"]:
            issues.append(f"记录 {rid} v1 哈希与清单映射不符")
        if v2r["record_hash_v2"] != mapping["v2_record_hash"]:
            issues.append(f"记录 {rid} v2 哈希与清单映射不符")
        if content_hash(
            {k: v for k, v in v2r.items() if k != "record_hash_v2"}
        ) != v2r["record_hash_v2"]:
            issues.append(f"记录 {rid} v2 自哈希不可重算")

        # 反向逐值：类型 + 值必须精确还原（false != 0 != "" != [] != None != missing）
        for name, typed in v2r["fields"].items():
            decoded = decode_v2_field(typed)
            if decoded is MISSING:
                if name in v1r["fields"]:
                    issues.append(f"记录 {rid} 字段 {name} 标 missing 但 v1 实际存在")
                continue
            if name not in v1r["fields"]:
                issues.append(f"记录 {rid} 字段 {name} v1 缺失但 v2 有值")
                continue
            raw = v1r["fields"][name]
            if typed["type"] != classify_value(raw):
                issues.append(f"记录 {rid} 字段 {name} 类型回填错误: "
                              f"{typed['type']} != {classify_value(raw)}")
            # 类型相同后再比值；False != 0、"" != [] 由类型标签先行保障
            if decoded != raw or type(decoded) is not type(raw):
                issues.append(f"记录 {rid} 字段 {name} 值回填错误: "
                              f"{decoded!r} != {raw!r}")

        # 来源原材料核对
        source = v2r["source"]
        if source["status"] == "unknown":
            if source["reason"] == "v1_record_missing_source_key":
                if "source" in v1r:
                    issues.append(f"记录 {rid} 来源标缺失但 v1 存在 source 键")
            elif v1r.get("source") != source["raw_material"].get("source"):
                issues.append(f"记录 {rid} 未知来源原材料未原样保留")
        elif source["value"] != v1r.get("source"):
            issues.append(f"记录 {rid} 已确认来源与 v1 不一致")

    # 6. 范围复算
    scope = reconcile_scope(v1_doc)
    if scope != manifest["scope"]:
        issues.append("范围核对结果不可复算（scope 与当前 v1 工件不一致）")

    return issues
