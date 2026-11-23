"""数据投递清单（投递账本）：按转换清单选版本、幂等凭证、回滚留痕。

核心机制
--------
幂等键 = (目标, 批次, 记录, schema 版本, 该版本记录内容哈希)。
- 同一目标对同一记录的同一内容版本只可能产生一次 ``ingestion`` 事件；
- 重试投递命中已有凭证时只补一条 ``duplicate_suppressed`` 审计事件，
  不会产生新的入仓凭证 —— 新旧部署同时读取也不会让任一目标重复入仓；
- 回滚只追加 ``rollback`` 事件并切换目标读取版本，**不删除、不改写**
  切换期已经落下的凭证，因此回滚后仍可查到实际接收过的内容。

账本是只追加 JSONL；加载时重建内存索引，凭证号由幂等键确定性派生，
进程重启后重试仍然幂等。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .common import (
    append_jsonl,
    canonical_json,
    content_hash,
    ensure_dir,
    read_jsonl,
    sha256_text,
    utc_now,
    write_once,
)

EVENT_INGESTION = "ingestion"
EVENT_DUPLICATE = "duplicate_suppressed"
EVENT_BATCH = "batch_delivery"
EVENT_ROLLBACK = "rollback"


@dataclass(frozen=True)
class Target:
    """消费目标：目标编号与它当前能接收的字段版本。"""

    target_id: str
    name: str
    accepted_version: str  # "v1" 或 "v2"

    def with_version(self, version: str) -> "Target":
        return Target(self.target_id, self.name, version)


def idempotency_key(target_id: str, batch_id: str, record_id: str,
                    version: str, record_hash: str) -> str:
    """确定性幂等键：同样输入永远得到同样的键。"""
    raw = "|".join([target_id, batch_id, record_id, version, record_hash])
    return sha256_text(raw)[:24]


def receipt_id(target_id: str, batch_id: str, record_id: str,
               version: str, record_hash: str) -> str:
    return f"RCP-{idempotency_key(target_id, batch_id, record_id, version, record_hash)}"


class DeliveryLedger:
    """只追加投递账本。"""

    def __init__(self, runtime_dir: str):
        self.runtime_dir = runtime_dir
        self.ledger_dir = ensure_dir(f"{runtime_dir}/delivery")
        self.event_log = f"{self.ledger_dir}/events.jsonl"
        self.payload_dir = ensure_dir(f"{self.ledger_dir}/payloads")
        # 幂等索引：idempotency_key -> ingestion 事件
        self._ingested: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        if os.path.exists(self.event_log):
            for event in read_jsonl(self.event_log):
                self._events.append(event)
                if event["event_type"] == EVENT_INGESTION:
                    self._ingested[event["idempotency_key"]] = event

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        event.setdefault("event_id", f"EVT-{sha256_text(canonical_json(event))[:20]}")
        event.setdefault("occurred_at", utc_now())
        append_jsonl(self.event_log, event)
        self._events.append(event)
        return event

    def _write_payload(self, target_id: str, batch_id: str, record_id: str,
                       version: str, payload: dict[str, Any]) -> str:
        """投递载荷按内容寻址落盘（只写一次），凭证可回查实际发出去的字节。"""
        digest = content_hash(payload)
        rel_path = (f"payloads/{batch_id}/{target_id}/{version}/"
                    f"{record_id}.{digest[:12]}.json")
        abs_path = f"{self.ledger_dir}/{rel_path}"
        if not os.path.exists(abs_path):
            import json
            write_once(
                f"{self.ledger_dir}/{rel_path}",
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            )
        return rel_path

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------
    def deliver_record(self, target: Target, batch_id: str, record_id: str,
                       version: str, record: dict[str, Any],
                       record_hash: str, manifest_id: str | None) -> dict[str, Any]:
        """投递一条记录，返回投递结果（含是否首次入仓）。

        target.accepted_version 必须与投递版本一致 —— 目标读什么版本，
        账本就只允许发什么版本，避免过渡期错发字段格式。
        """
        if target.accepted_version != version:
            raise ValueError(
                f"目标 {target.target_id} 当前只接收 {target.accepted_version}，"
                f"拒绝投递 {version} 记录 {record_id}"
            )

        key = idempotency_key(target.target_id, batch_id, record_id,
                              version, record_hash)
        existing = self._ingested.get(key)
        if existing is not None:
            # 已入仓：只留审计事件，绝不重复产生凭证
            self._append({
                "event_type": EVENT_DUPLICATE,
                "target_id": target.target_id,
                "batch_id": batch_id,
                "record_id": record_id,
                "schema_version": version,
                "record_hash": record_hash,
                "idempotency_key": key,
                "existing_receipt_id": existing["receipt_id"],
                "manifest_id": manifest_id,
                "note": "重试/切换期重复投递，命中幂等键，未产生新入仓凭证",
            })
            return {
                "record_id": record_id,
                "schema_version": version,
                "ingested": False,
                "receipt_id": existing["receipt_id"],
                "idempotency_key": key,
            }

        payload = self._build_payload(target, batch_id, record_id, version,
                                      record, record_hash, manifest_id)
        payload_path = self._write_payload(target.target_id, batch_id,
                                           record_id, version, payload)
        rcpt = receipt_id(target.target_id, batch_id, record_id,
                          version, record_hash)
        event = self._append({
            "event_type": EVENT_INGESTION,
            "target_id": target.target_id,
            "target_name": target.name,
            "batch_id": batch_id,
            "record_id": record_id,
            "schema_version": version,
            "record_hash": record_hash,
            "idempotency_key": key,
            "receipt_id": rcpt,
            "manifest_id": manifest_id,
            "payload_path": payload_path,
            "payload_hash": content_hash(payload),
        })
        self._ingested[key] = event
        return {
            "record_id": record_id,
            "schema_version": version,
            "ingested": True,
            "receipt_id": rcpt,
            "idempotency_key": key,
            "payload_path": payload_path,
        }

    def deliver_batch(self, target: Target, batch_id: str, version: str,
                      records: list[dict[str, Any]],
                      hash_field: str, manifest_id: str | None) -> dict[str, Any]:
        """按清单选定的版本整批投递，返回批次级投递汇总与逐记录凭证。"""
        results = [
            self.deliver_record(
                target, batch_id, r["record_id"], version, r,
                r[hash_field], manifest_id,
            )
            for r in sorted(records, key=lambda r: r["record_id"])
        ]
        first = [r for r in results if r["ingested"]]
        suppressed = [r for r in results if not r["ingested"]]
        batch_key_material = "|".join(
            [target.target_id, batch_id, version,
             "".join(r["idempotency_key"] for r in results)]
        )
        delivery_id = f"DLV-{sha256_text(batch_key_material)[:20]}"
        self._append({
            "event_type": EVENT_BATCH,
            "delivery_id": delivery_id,
            "target_id": target.target_id,
            "batch_id": batch_id,
            "schema_version": version,
            "manifest_id": manifest_id,
            "record_count": len(results),
            "newly_ingested_count": len(first),
            "duplicate_suppressed_count": len(suppressed),
            "record_ids": [r["record_id"] for r in results],
            "receipt_ids": [r["receipt_id"] for r in results],
        })
        return {
            "delivery_id": delivery_id,
            "target_id": target.target_id,
            "batch_id": batch_id,
            "schema_version": version,
            "newly_ingested": first,
            "duplicate_suppressed": suppressed,
            "results": results,
        }

    def _build_payload(self, target: Target, batch_id: str, record_id: str,
                       version: str, record: dict[str, Any],
                       record_hash: str, manifest_id: str | None) -> dict[str, Any]:
        """实际发往目标的载荷：v1 发裸字段，v2 发精确类型字段，均挂哈希。"""
        if version == "v1":
            body = {
                "batch_id": batch_id,
                "record_id": record_id,
                "schema_version": "v1",
                "source": record.get("source"),
                "fields": record["fields"],
            }
        elif version == "v2":
            body = {
                "batch_id": batch_id,
                "record_id": record_id,
                "schema_version": "v2",
                "source": record["source"],
                "fields": record["fields"],
                "v1_record_hash": record["v1_record_hash"],
            }
        else:
            raise ValueError(f"未知 schema 版本: {version}")
        body["manifest_id"] = manifest_id
        body["record_hash"] = record_hash
        return body

    # ------------------------------------------------------------------
    # 回滚与查询
    # ------------------------------------------------------------------
    def rollback_target(self, target: Target, from_version: str,
                        to_version: str, reason: str) -> dict[str, Any]:
        """登记目标回滚：只追加事件，凭证全部保留。"""
        return self._append({
            "event_type": EVENT_ROLLBACK,
            "target_id": target.target_id,
            "target_name": target.name,
            "batch_scope": "all",
            "from_version": from_version,
            "to_version": to_version,
            "reason": reason,
            "note": "回滚仅切换读取版本，切换期已开 ingestion 凭证保留可查",
        })

    def received_range(self, target_id: str, batch_id: str,
                       version: str | None = None) -> dict[str, Any]:
        """查某目标对某批次“实际接收过”的范围（按凭证去重）。"""
        receipts = [
            e for e in self._events
            if e["event_type"] == EVENT_INGESTION
            and e["target_id"] == target_id
            and e["batch_id"] == batch_id
            and (version is None or e["schema_version"] == version)
        ]
        # 同一 record 在不同版本各有一张凭证，都要保留
        return {
            "target_id": target_id,
            "batch_id": batch_id,
            "version_filter": version,
            "receipt_count": len(receipts),
            "record_ids": sorted({e["record_id"] for e in receipts}),
            "receipts": sorted(
                ({"receipt_id": e["receipt_id"],
                  "record_id": e["record_id"],
                  "schema_version": e["schema_version"],
                  "record_hash": e["record_hash"],
                  "payload_path": e["payload_path"],
                  "payload_hash": e["payload_hash"],
                  "occurred_at": e["occurred_at"]} for e in receipts),
                key=lambda x: (x["record_id"], x["schema_version"]),
            ),
        }

    def ingestion_count(self, target_id: str, batch_id: str) -> int:
        """入仓凭证数（同一目标+批次+记录+版本+内容只计一次）。"""
        return len({
            e["idempotency_key"] for e in self._events
            if e["event_type"] == EVENT_INGESTION
            and e["target_id"] == target_id
            and e["batch_id"] == batch_id
        })

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type is None:
            return list(self._events)
        return [e for e in self._events if e["event_type"] == event_type]

    def current_versions(self) -> dict[str, str]:
        """根据回滚事件推导每个目标当前读取版本（无回滚记录则查不到）。"""
        versions: dict[str, str] = {}
        for event in self._events:
            if event["event_type"] == EVENT_ROLLBACK:
                versions[event["target_id"]] = event["to_version"]
        return versions


def verify_payload_binding(ledger: DeliveryLedger) -> list[str]:
    """复核所有 ingestion 凭证：载荷文件存在且哈希与凭证登记一致。"""
    issues: list[str] = []
    for event in ledger.events(EVENT_INGESTION):
        path = f"{ledger.ledger_dir}/{event['payload_path']}"
        if not os.path.exists(path):
            issues.append(f"凭证 {event['receipt_id']} 的载荷文件缺失: {path}")
            continue
        from .common import read_json
        payload = read_json(path)
        if content_hash(payload) != event["payload_hash"]:
            issues.append(f"凭证 {event['receipt_id']} 的载荷哈希不可重算")
        if payload["record_hash"] != event["record_hash"]:
            issues.append(f"凭证 {event['receipt_id']} 载荷内记录哈希与凭证不符")
    return issues
