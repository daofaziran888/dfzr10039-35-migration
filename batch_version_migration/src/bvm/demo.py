"""端到端场景编排：构造样本 -> 迁移 -> 双版本投递 -> 上线中途回滚 -> 核对。

场景时间线
----------
1. 落 v1 批次（含 false/0/空串/空数组/null/键缺失/来源缺失 + 摘要范围冲突）；
2. 迁移 v2，产出批次版本转换清单，反向逐值复核；
3. 双跑期：旧仓库 legacy-wh 读 v1 收全量；新目标 precision-lake 先收部分 v2
   （模拟上线进行到一半）；
4. 中途回滚：precision-lake 切回 v1，重发同一批次；
5. 验证：此前 v2 有效凭证仍在、v1 重发不产生重复入仓、再投一次仍被幂等拦截。

运行：``python3 -I run_demo.py``（仓库根目录提供入口脚本）。
"""
from __future__ import annotations

import json
import shutil
from typing import Any

from . import migration
from .checks import CheckBook
from .common import (
    content_hash,
    ensure_dir,
    read_json,
    utc_now,
    write_once,
)
from .delivery import DeliveryLedger, Target
from .sample_v1 import BATCH_ID, build_v1_batch, write_v1_batch

LEGACY_TARGET = "legacy-wh"        # 旧仓库：过渡期一直读 v1
PRECISION_TARGET = "precision-lake"  # 新目标：先 v2，回滚后 v1


def reset_runtime(runtime_dir: str) -> None:
    shutil.rmtree(runtime_dir, ignore_errors=True)
    ensure_dir(runtime_dir)


def _load_payload(ledger: DeliveryLedger, payload_path: str) -> dict[str, Any]:
    return read_json(f"{ledger.ledger_dir}/{payload_path}")


def run(runtime_dir: str) -> dict[str, Any]:
    reset_runtime(runtime_dir)
    book = CheckBook()
    context: dict[str, Any] = {"runtime_dir": runtime_dir}

    # ------------------------------------------------------------------
    # 阶段 1：v1 旧批次落盘（只写一次，历史不可变）
    # ------------------------------------------------------------------
    v1_bundle = write_v1_batch(runtime_dir)
    v1_batch = v1_bundle["batch"]
    v1_records = v1_batch["records"]
    v1_by_id = {r["record_id"]: r for r in v1_records}
    context["v1_doc_path"] = v1_bundle["doc_path"]
    v1_doc_hash_before = content_hash(v1_batch)

    # C1 摘要范围冲突被显式识别
    scope = migration.reconcile_scope(v1_batch)
    book.record(
        "C1", "旧摘要与明细范围冲突可识别",
        scope["status"] == "conflict"
        and scope["declared_count"] == 5 and scope["actual_count"] == 6
        and scope["undeclared_records"] == ["R006"],
        f"摘要声明 {scope['declared_count']} 条、实际明细 {scope['actual_count']} 条，"
        f"漏统 {scope['undeclared_records']}，冲突登记而非静默修正",
        scope=scope,
    )

    # ------------------------------------------------------------------
    # 阶段 2：迁移 v2 + 转换清单
    # ------------------------------------------------------------------
    manifest = migration.migrate(v1_batch, runtime_dir)
    context["manifest_path"] = f"{runtime_dir}/manifests/CV-{BATCH_ID}.json"

    issues = migration.verify_manifest(manifest, runtime_dir)
    book.record(
        "C2", "转换清单完整性与反向逐值复核",
        not issues,
        "清单自哈希、双侧工件哈希、逐记录映射、逐字段类型/值还原全部可重算"
        if not issues else "; ".join(issues),
        issues=issues,
        manifest_id=manifest["manifest_id"],
        manifest_hash=manifest["manifest_hash"],
    )

    v2_doc = read_json(f"{runtime_dir}/{manifest['v2_artifact']['batch_doc']}")
    v2_records = v2_doc["records"]
    v2_by_id = {r["record_id"]: r for r in v2_records}

    # C3 false / 0 / 空项 六类值精确分离（以 R001/R003/R005 为证）
    r001_v2 = v2_by_id["R001"]
    r003_v2 = v2_by_id["R003"]
    r005_v2 = v2_by_id["R005"]
    c3_ok = (
        r001_v2["fields"]["flag"] == {"type": "boolean", "value": False}
        and r001_v2["fields"]["score"] == {"type": "integer", "value": 0}
        and r001_v2["fields"]["note"] == {"type": "string", "value": ""}
        and r001_v2["fields"]["tags"] == {"type": "array", "value": []}
        and r003_v2["fields"]["note"] == {"type": "null", "value": None}
        and r005_v2["fields"]["note"] == {"type": "missing", "value": None}
    )
    book.record(
        "C3", "false/0/空串/空数组/null/键缺失 六类值不混同",
        c3_ok,
        "R001: flag=boolean:false, score=integer:0, note=string:'', tags=array:[]；"
        "R003.note=null；R005.note=missing（键缺失，区别于 null）",
        samples={
            "R001.flag": r001_v2["fields"]["flag"],
            "R001.score": r001_v2["fields"]["score"],
            "R001.note": r001_v2["fields"]["note"],
            "R001.tags": r001_v2["fields"]["tags"],
            "R003.note": r003_v2["fields"]["note"],
            "R005.note": r005_v2["fields"]["note"],
        },
    )

    # C4 来源未知：保留原材料、不伪造
    unknown = [
        (rid, v2_by_id[rid]["source"])
        for rid in manifest["unknown_source_records"]
    ]
    c4_ok = (
        manifest["unknown_source_records"] == ["R003", "R004", "R006"]
        and all(s["status"] == "unknown" and s["raw_material"] for _, s in unknown)
        and v2_by_id["R001"]["source"]["status"] == "confirmed"
        and v2_by_id["R003"]["source"]["reason"] == "v1_source_blank"
        and v2_by_id["R004"]["source"]["reason"]
        == "v1_record_missing_source_key"
    )
    book.record(
        "C4", "来源未知保留 v1 原材料且不伪造",
        c4_ok,
        "R003/R006 空串来源、R004 缺 source 键均标 unknown，原材料原样挂接；"
        "R001/R002/R005 标 confirmed",
        unknown_sources={rid: s for rid, s in unknown},
    )

    # C5 清单防篡改：改动任意字节即复核失败
    tampered = json.loads(json.dumps(manifest))
    tampered["summary"]["unknown_source_records"] = 999
    tampered_issues = migration.verify_manifest(tampered, runtime_dir)
    book.record(
        "C5", "转换清单哈希可检测篡改",
        any("清单哈希不一致" in msg for msg in tampered_issues),
        "改写清单正文后复核立即报哈希不一致",
        detected_messages=tampered_issues,
    )

    # C6 v1 历史工件不可变（迁移前后哈希一致，且重复写入被拒绝）
    v1_doc_hash_after = content_hash(read_json(v1_bundle["doc_path"]))
    immutable = v1_doc_hash_before == v1_doc_hash_after
    try:
        write_v1_batch(runtime_dir)
        overwrite_blocked = False
    except FileExistsError:
        overwrite_blocked = True
    book.record(
        "C6", "v1 历史工件保持不可变",
        immutable and overwrite_blocked,
        "迁移前后 v1 文档哈希一致，重复落盘触发只写一次保护",
        v1_hash=v1_doc_hash_before,
    )

    # ------------------------------------------------------------------
    # 阶段 3：双跑期投递
    # ------------------------------------------------------------------
    ledger = DeliveryLedger(runtime_dir)
    legacy = Target(LEGACY_TARGET, "旧数据仓库", "v1")
    precision = Target(PRECISION_TARGET, "精确类型数据湖", "v2")

    # 旧仓库读 v1，全量
    legacy_delivery = ledger.deliver_batch(
        legacy, BATCH_ID, "v1", v1_records, "record_hash_v1",
        manifest["manifest_id"],
    )
    # 新目标上线进行到一半：只收到前 3 条 v2（R001..R003）
    partial_v2 = [r for r in v2_records if r["record_id"] in ("R001", "R002", "R003")]
    partial_delivery = ledger.deliver_batch(
        precision, BATCH_ID, "v2", partial_v2, "record_hash_v2",
        manifest["manifest_id"],
    )

    # C7 双跑期：各目标只按自身版本接收，范围正确
    legacy_range = ledger.received_range(LEGACY_TARGET, BATCH_ID)
    precision_v2_range = ledger.received_range(PRECISION_TARGET, BATCH_ID, "v2")
    c7_ok = (
        len(legacy_delivery["newly_ingested"]) == 6
        and legacy_range["record_ids"] == [f"R00{i}" for i in range(1, 7)]
        and all(r["schema_version"] == "v1" for r in legacy_range["receipts"])
        and len(partial_delivery["newly_ingested"]) == 3
        and precision_v2_range["record_ids"] == ["R001", "R002", "R003"]
        and all(r["schema_version"] == "v2" for r in precision_v2_range["receipts"])
    )
    book.record(
        "C7", "双跑期按目标版本投递且接收范围正确",
        c7_ok,
        f"旧仓库 v1 实收 {legacy_range['receipt_count']} 条全量；"
        f"新目标 v2 上线中途实收 {precision_v2_range['receipt_count']} 条（R001-R003）",
        legacy_range=legacy_range["record_ids"],
        precision_v2_range=precision_v2_range["record_ids"],
    )

    # C8 两个版本下字段值精确核对（含实际投递载荷）
    r001_v1_receipt = next(
        r for r in legacy_range["receipts"] if r["record_id"] == "R001"
    )
    r001_v2_receipt = next(
        r for r in precision_v2_range["receipts"] if r["record_id"] == "R001"
    )
    v1_payload = _load_payload(ledger, r001_v1_receipt["payload_path"])
    v2_payload = _load_payload(ledger, r001_v2_receipt["payload_path"])
    c8_ok = (
        v1_payload["fields"]["flag"] is False
        and v1_payload["fields"]["score"] == 0
        and v1_payload["fields"]["note"] == ""
        and v1_payload["fields"]["tags"] == []
        and v2_payload["fields"]["flag"] == {"type": "boolean", "value": False}
        and v2_payload["fields"]["score"] == {"type": "integer", "value": 0}
        and v2_payload["fields"]["note"] == {"type": "string", "value": ""}
        and v2_payload["fields"]["tags"] == {"type": "array", "value": []}
        and v1_payload["record_hash"] == v1_by_id["R001"]["record_hash_v1"]
        and v2_payload["record_hash"] == v2_by_id["R001"]["record_hash_v2"]
    )
    book.record(
        "C8", "同一记录在 v1/v2 投递载荷中的字段值精确一致",
        c8_ok,
        "R001 两版载荷逐字段比对：v1 裸值 false/0/''/[] 与 v2 带类型标注一一对应，"
        "载荷哈希绑定凭证",
        v1_payload_fields=v1_payload["fields"],
        v2_payload_fields=v2_payload["fields"],
    )

    # C9 重试幂等：同版本重发不产生新凭证
    legacy_retry = ledger.deliver_batch(
        legacy, BATCH_ID, "v1", v1_records, "record_hash_v1",
        manifest["manifest_id"],
    )
    c9_ok = (
        not legacy_retry["newly_ingested"]
        and len(legacy_retry["duplicate_suppressed"]) == 6
        and ledger.ingestion_count(LEGACY_TARGET, BATCH_ID) == 6
    )
    book.record(
        "C9", "同版本重发被幂等拦截，不重复入仓",
        c9_ok,
        "旧仓库整批重投 6 条全部命中已有凭证，仅追加 duplicate_suppressed 审计事件，"
        "ingestion 凭证仍为 6 张",
        suppressed=[r["record_id"] for r in legacy_retry["duplicate_suppressed"]],
    )

    # ------------------------------------------------------------------
    # 阶段 4：上线中途回滚 precision-lake：v2 -> v1
    # ------------------------------------------------------------------
    rollback_event = ledger.rollback_target(
        precision, from_version="v2", to_version="v1",
        reason="上线观察期 v2 链路异常，按预案回滚到 v1 读取",
    )
    precision_v1 = precision.with_version("v1")

    # 回滚后向原目标重发同一批次（v1 全量）
    rollback_delivery = ledger.deliver_batch(
        precision_v1, BATCH_ID, "v1", v1_records, "record_hash_v1",
        manifest["manifest_id"],
    )

    # C10 回滚重发不重复：此前 v2 有效凭证保留，v1 按新版本键正常入仓
    precision_all = ledger.received_range(PRECISION_TARGET, BATCH_ID)
    c10_ok = (
        len(rollback_delivery["newly_ingested"]) == 6
        and all(r["schema_version"] == "v1"
                for r in rollback_delivery["newly_ingested"])
        and precision_v2_range["receipts"] == ledger.received_range(
            PRECISION_TARGET, BATCH_ID, "v2")["receipts"]
        # 同一目标下 v2 三张 + v1 六张 = 9 张凭证，无任何一张重复
        and ledger.ingestion_count(PRECISION_TARGET, BATCH_ID) == 9
    )
    book.record(
        "C10", "回滚后重发不重复入仓且切换期凭证保留",
        c10_ok,
        "回滚后 v1 全量 6 条为新凭证（版本键不同）；此前 3 张 v2 凭证原样可查，"
        "新目标累计 9 张凭证、0 张重复",
        v2_receipts_kept=[r["receipt_id"]
                          for r in precision_v2_range["receipts"]],
        v1_receipts_after_rollback=[r["receipt_id"]
                                    for r in rollback_delivery["newly_ingested"]],
        rollback_event_id=rollback_event["event_id"],
    )

    # C11 回滚后再投一次：无论 v1 还是 v2 视角都被幂等拦截
    second_retry = ledger.deliver_batch(
        precision_v1, BATCH_ID, "v1", v1_records, "record_hash_v1",
        manifest["manifest_id"],
    )
    c11_ok = (
        not second_retry["newly_ingested"]
        and len(second_retry["duplicate_suppressed"]) == 6
        and ledger.ingestion_count(PRECISION_TARGET, BATCH_ID) == 9
    )
    book.record(
        "C11", "回滚后再次重发仍被幂等拦截",
        c11_ok,
        "v1 二次重投 6 条全部 duplicate_suppressed，凭证总数维持 9 张",
    )

    # C12 凭证与载荷绑定可复核（含回滚后可查切换期已收内容）
    binding_issues = migration_verify_binding(ledger)
    c12_ok = not binding_issues and all(
        _load_payload(ledger, r["payload_path"])["record_id"] == r["record_id"]
        for r in precision_all["receipts"]
    )
    book.record(
        "C12", "全部凭证载荷落盘且哈希可重算，回滚后切换期已收内容可查",
        c12_ok,
        f"账本共 {len(ledger.events('ingestion'))} 张 ingestion 凭证，"
        "每张均可按 payload_path 取回实际发送内容并核对哈希"
        if not binding_issues else "; ".join(binding_issues),
        binding_issues=binding_issues,
        precision_all_receipts=precision_all["receipts"],
    )

    # C13 版本护栏：目标只接收清单为它选定的字段版本
    guard_blocked = False
    try:
        ledger.deliver_record(
            precision_v1, BATCH_ID, "R001", "v2", v2_by_id["R001"],
            v2_by_id["R001"]["record_hash_v2"], manifest["manifest_id"],
        )
    except ValueError:
        guard_blocked = True
    # 护栏拦截不应留下任何事件
    no_event_added = all(
        e["record_id"] != "R001" or e["schema_version"] != "v2"
        for e in ledger.events("duplicate_suppressed")
        if e["target_id"] == PRECISION_TARGET
    )
    book.record(
        "C13", "目标版本护栏拒绝错版字段投递",
        guard_blocked and no_event_added,
        "回滚后 precision-lake 仅接收 v1，强投 v2 记录被直接拒绝且无副作用",
    )

    # ------------------------------------------------------------------
    # 账本重启一致性：重新加载 JSONL 后幂等索引完全恢复
    # ------------------------------------------------------------------
    reloaded = DeliveryLedger(runtime_dir)
    reload_ok = (
        reloaded.ingestion_count(LEGACY_TARGET, BATCH_ID) == 6
        and reloaded.ingestion_count(PRECISION_TARGET, BATCH_ID) == 9
        and reloaded.current_versions().get(PRECISION_TARGET) == "v1"
    )
    retry_after_reload = reloaded.deliver_batch(
        Target(LEGACY_TARGET, "旧数据仓库", "v1"), BATCH_ID, "v1",
        v1_records, "record_hash_v1", manifest["manifest_id"],
    )
    reload_ok = reload_ok and not retry_after_reload["newly_ingested"]
    book.record(
        "C14", "账本进程重启后幂等性与回滚状态一致",
        reload_ok,
        "重建账本后凭证计数（6/9）、当前版本（precision-lake=v1）一致，"
        "重启后重试依旧全部抑制",
    )

    # ------------------------------------------------------------------
    # 输出报告
    # ------------------------------------------------------------------
    report_md = book.render_markdown()
    report_path = f"{runtime_dir}/REPORT.md"
    write_once(report_path, report_md)
    write_once(
        f"{runtime_dir}/run_meta.json",
        json.dumps({"ran_at": utc_now(), "batch_id": BATCH_ID,
                    "all_passed": book.all_passed},
                   ensure_ascii=False, indent=2, sort_keys=True),
    )

    context.update({
        "book": book,
        "manifest": manifest,
        "ledger": ledger,
        "report_path": report_path,
        "events": ledger.events(),
    })
    return context


def migration_verify_binding(ledger: DeliveryLedger) -> list[str]:
    """薄封装，避免 demo 直接依赖 delivery 内部函数顺序。"""
    from .delivery import verify_payload_binding
    return verify_payload_binding(ledger)
