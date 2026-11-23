"""迁移与投递核心规则单元测试（pytest 兼容，也可用 run_tests.py 直接跑）。"""
from __future__ import annotations

import json
import os
import tempfile

from bvm import migration
from bvm.common import read_json, read_jsonl
from bvm.delivery import DeliveryLedger, Target
from bvm.sample_v1 import build_v1_batch, write_v1_batch


def _tmp_runtime() -> str:
    return tempfile.mkdtemp(prefix="bvm-test-")


def test_classifier_separates_falsy_family() -> None:
    assert migration.classify_value(False) == migration.TYPE_BOOLEAN
    assert migration.classify_value(0) == migration.TYPE_INTEGER
    assert migration.classify_value("") == migration.TYPE_STRING
    assert migration.classify_value([]) == migration.TYPE_ARRAY
    assert migration.classify_value(None) == migration.TYPE_NULL
    assert migration.classify_value(0.0) == migration.TYPE_FLOAT
    # bool 不得被识别成 integer
    assert migration.classify_value(True) != migration.TYPE_INTEGER


def test_convert_fields_missing_distinct_from_null() -> None:
    fields = {"a": None, "b": False, "c": 0}
    converted = migration.convert_fields(fields, ["a", "b", "c", "d"])
    assert converted["a"] == {"type": migration.TYPE_NULL, "value": None}
    assert converted["b"] == {"type": migration.TYPE_BOOLEAN, "value": False}
    assert converted["c"] == {"type": migration.TYPE_INTEGER, "value": 0}
    assert converted["d"] == {"type": migration.TYPE_MISSING, "value": None}
    # 解码后类型仍然严格不同
    assert migration.decode_v2_field(converted["b"]) is False
    assert migration.decode_v2_field(converted["c"]) == 0
    assert migration.decode_v2_field(converted["d"]) is migration.MISSING


def test_unknown_source_keeps_raw_material() -> None:
    blank = migration.convert_source({"record_id": "x", "source": "", "fields": {}})
    absent = migration.convert_source({"record_id": "y", "fields": {}})
    good = migration.convert_source(
        {"record_id": "z", "source": "https://ok.example", "fields": {}}
    )
    assert blank["status"] == "unknown"
    assert blank["raw_material"] == {"source_key_present": True, "source": ""}
    assert absent["status"] == "unknown"
    assert absent["raw_material"] == {"source_key_present": False}
    assert good["status"] == "confirmed"
    assert good["value"] == "https://ok.example"


def test_scope_conflict_detects_undeclared_record() -> None:
    batch = build_v1_batch()
    scope = migration.reconcile_scope(batch)
    assert scope["status"] == "conflict"
    assert scope["undeclared_records"] == ["R006"]
    assert scope["actual_count"] == 6 and scope["declared_count"] == 5


def test_full_migration_roundtrip_and_manifest_verify() -> None:
    runtime = _tmp_runtime()
    bundle = write_v1_batch(runtime)
    manifest = migration.migrate(bundle["batch"], runtime)
    assert manifest["summary"]["total_records"] == 6
    assert manifest["unknown_source_records"] == ["R003", "R004", "R006"]
    # 反向复核零问题
    assert migration.verify_manifest(manifest, runtime) == []

    v2_records = read_jsonl(f"{runtime}/batches/"
                            f"{manifest['batch_id']}/v2/records_v2.jsonl")
    by_id = {r["record_id"]: r for r in v2_records}
    # R005.note 在 v1 缺键 -> missing；R003.note 显式 null
    assert by_id["R005"]["fields"]["note"]["type"] == migration.TYPE_MISSING
    assert by_id["R003"]["fields"]["note"]["type"] == migration.TYPE_NULL
    # R004.tags 含空串数组，与 R001 空数组区分
    assert by_id["R004"]["fields"]["tags"] == {"type": "array", "value": [""]}
    assert by_id["R001"]["fields"]["tags"] == {"type": "array", "value": []}


def test_manifest_tamper_detected() -> None:
    runtime = _tmp_runtime()
    bundle = write_v1_batch(runtime)
    manifest = migration.migrate(bundle["batch"], runtime)
    tampered = json.loads(json.dumps(manifest))
    tampered["scope"]["actual_count"] = 99
    issues = migration.verify_manifest(tampered, runtime)
    assert any("清单哈希不一致" in msg for msg in issues)


def test_ledger_idempotent_and_rollback_keeps_receipts() -> None:
    runtime = _tmp_runtime()
    bundle = write_v1_batch(runtime)
    manifest = migration.migrate(bundle["batch"], runtime)
    v1_records = bundle["batch"]["records"]
    v2_records = read_jsonl(f"{runtime}/batches/"
                            f"{manifest['batch_id']}/v2/records_v2.jsonl")

    ledger = DeliveryLedger(runtime)
    target = Target("lake", "数据湖", "v2")

    first = ledger.deliver_batch(
        target, manifest["batch_id"], "v2", v2_records[:3],
        "record_hash_v2", manifest["manifest_id"],
    )
    assert len(first["newly_ingested"]) == 3

    # 同版本重试：零新增
    retry = ledger.deliver_batch(
        target, manifest["batch_id"], "v2", v2_records[:3],
        "record_hash_v2", manifest["manifest_id"],
    )
    assert retry["newly_ingested"] == []
    assert len(retry["duplicate_suppressed"]) == 3
    assert ledger.ingestion_count("lake", manifest["batch_id"]) == 3

    # 回滚到 v1 后全量重发：6 张全新凭证，v2 三张保留
    ledger.rollback_target(target, "v2", "v1", "演练回滚")
    v1_target = target.with_version("v1")
    rolled = ledger.deliver_batch(
        v1_target, manifest["batch_id"], "v1", v1_records,
        "record_hash_v1", manifest["manifest_id"],
    )
    assert len(rolled["newly_ingested"]) == 6
    assert ledger.ingestion_count("lake", manifest["batch_id"]) == 9
    v2_range = ledger.received_range("lake", manifest["batch_id"], "v2")
    assert len(v2_range["receipts"]) == 3

    # 重启后状态一致、仍然幂等
    reloaded = DeliveryLedger(runtime)
    assert reloaded.ingestion_count("lake", manifest["batch_id"]) == 9
    again = reloaded.deliver_batch(
        v1_target, manifest["batch_id"], "v1", v1_records,
        "record_hash_v1", manifest["manifest_id"],
    )
    assert again["newly_ingested"] == []


def test_version_guard_rejects_wrong_schema() -> None:
    runtime = _tmp_runtime()
    bundle = write_v1_batch(runtime)
    manifest = migration.migrate(bundle["batch"], runtime)
    v2_records = read_jsonl(f"{runtime}/batches/"
                            f"{manifest['batch_id']}/v2/records_v2.jsonl")
    ledger = DeliveryLedger(runtime)
    v1_only = Target("wh", "旧仓库", "v1")
    try:
        ledger.deliver_record(
            v1_only, manifest["batch_id"], "R001", "v2", v2_records[0],
            v2_records[0]["record_hash_v2"], manifest["manifest_id"],
        )
    except ValueError:
        pass
    else:  # pragma: no cover - 防御性断言
        raise AssertionError("版本护栏应当拒绝错版投递")
    assert ledger.events() == []
