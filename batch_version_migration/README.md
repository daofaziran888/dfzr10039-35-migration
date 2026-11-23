# 采集批次 v1 → v2 版本迁移与双版本投递核对原型

面向部署过渡期的存量批次迁移演练：把按 v1（摘要 + 裸字段）存储的采集批次，
迁移为带来源状态与精确类型标注的 v2，以**批次版本转换清单**支撑过渡期的
目标读取，用**只追加投递账本**保证新旧部署同时读取时没有任何目标重复入仓，
且上线中途回滚后切换期已收内容仍可逐凭证回查。

纯 Python 标准库实现，无第三方依赖，建议始终以隔离模式运行：`python3 -I ...`。

## 需求与实现对照

| 需求 | 实现 | 核对项 |
| --- | --- | --- |
| v1 摘要/字段 → v2 来源 + 精确类型 | `migration.py` 类型分派（bool 先于 int 判定），字段落 `{type,value}` | C2/C3 |
| 可核对的批次版本转换清单 | `manifest` 含双侧工件哈希、逐记录映射哈希、规则集与自哈希，支持反向逐值复核 | C2/C5 |
| 新旧格式验证旧摘要的范围冲突 | `reconcile_scope` 比对摘要声明范围与实际明细，冲突登记不静默修正 | C1 |
| 历史可复核、v1 不被改写 | v1 工件只写一次（`write_once`），迁移产出独立 v2 工件 | C6 |
| false/0/空项回填不混值 | 禁用真值判断，六类值（boolean/integer/string/array/null/missing）独立标注 | C3 |
| 来源未知保留原材料 | `source.status=unknown` + 原因 + `raw_material` 原样挂接，不伪造来源 | C4 |
| 投递按清单读取目标可接收的字段版本 | `Target.accepted_version` 版本护栏，错版投递直接拒绝且无副作用 | C13 |
| 落下实际接收凭证 | 每条 ingestion 凭证绑定内容寻址载荷文件与载荷哈希 | C12 |
| 新旧部署同时读取不重复入仓 | 幂等键 = 目标+批次+记录+版本+记录内容哈希，重试只追加 `duplicate_suppressed` | C9/C11 |
| 回滚后可查切换期已收内容 | 回滚只追加事件、切换版本，不删改任何凭证；账本重启后索引可重建 | C10/C14 |
| 含 false/0/空值/来源缺失的旧明细 | `sample_v1.py` 6 条明细覆盖全部边界 + 摘要漏统 R006 | C1-C14 |

## 目录结构

```text
batch_version_migration/
├── run_demo.py                 # 端到端演示入口
├── run_tests.py                # 无 pytest 依赖的测试运行器
├── src/bvm/
│   ├── common.py               # 规范化 JSON / SHA-256 / 只写一次落盘
│   ├── sample_v1.py            # 含全部边界值与范围冲突的 v1 样本批次
│   ├── migration.py            # v1->v2 转换、转换清单、反向复核
│   ├── delivery.py             # 投递账本、幂等凭证、回滚留痕、版本护栏
│   ├── checks.py               # 核对结论收集与报告渲染
│   └── demo.py                 # 场景编排（双跑 -> 中途回滚 -> 14 项核对）
├── tests/test_migration_delivery.py   # 8 个规则测试（pytest 兼容）
└── runtime/                    # 运行产物（gitignore，重新运行即重建）
    ├── batches/<id>/v1/        # 不可变 v1 工件（doc + jsonl）
    ├── batches/<id>/v2/        # v2 工件（精确类型字段）
    ├── manifests/CV-<id>.json  # 批次版本转换清单
    ├── delivery/events.jsonl   # 只追加投递账本
    ├── delivery/payloads/      # 每张凭证实际发出的载荷（内容寻址）
    └── REPORT.md                # 核对报告
```

## 运行

```bash
# 单元测试（两种方式等价；环境无 pytest 时用前者）
python3 -I run_tests.py
pytest tests/

# 端到端演练：迁移 -> 双版本投递 -> 上线中途回滚 -> 14 项核对
python3 -I run_demo.py
# 或指定隔离运行目录
python3 -I run_demo.py /tmp/bvm-runtime
```

## 演练时间线（demo.py）

1. 落 v1 批次 `B-2026-1108-01`：6 条明细，覆盖 `false`、`0`、空串、空数组、
   显式 `null`、字段键缺失、空串来源、来源键缺失；摘要只声明 5 条（漏统 R006）。
2. 迁移产出 v2 与转换清单 `CV-B-2026-1108-01`，反向逐值复核全部通过，
   登记 5 条异常（1 条范围冲突、3 条来源未知、1 条旧摘要计数不符）。
3. 双跑期：旧仓库 `legacy-wh` 按 v1 收全量 6 条；新目标 `precision-lake`
   按 v2 上线到一半，只收到 R001–R003。
4. 中途回滚：`precision-lake` 登记 `v2 -> v1` 回滚事件，按 v1 重发全量。
5. 核对：此前 3 张 v2 凭证原样保留；v1 6 张为新凭证（版本键不同，非重复）；
   之后任何重投都被幂等拦截；账本进程重启后状态一致。

最终凭证计数：`legacy-wh` 6 张（v1），`precision-lake` 9 张（3 v2 + 6 v1），
全链路 0 重复入仓，18 次重复投递以 `duplicate_suppressed` 审计事件留痕。

## 关键设计取舍

- **禁止真值判断**：`if x` 会把 `False/0/""/[]/None` 全部判假，回填一律走
  `isinstance`/`is None`/键存在性分派；`bool` 先于 `int` 判断（前者是后者子类）。
- **missing ≠ null**：v1 缺键标 `missing`，显式 `null` 标 `null`，二者互不可替代。
- **确定性幂等**：凭证号由幂等键哈希派生，不依赖进程内状态，账本重建后
  重试依然命中同一张凭证。
- **只追加、只写一次**：历史工件与账本均不可变；回滚是新增事件而非删除，
  保证切换期事实永远可复核。
