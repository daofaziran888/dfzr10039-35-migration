"""采集批次 v1 -> v2 版本迁移与双版本投递核对原型。

模块划分：
- common:    规范化哈希、只写一次落盘等公共能力
- sample_v1: 构造含 false/0/空值/来源缺失/摘要范围冲突的 v1 旧批次
- migration: v1 -> v2 转换、精确类型标注、批次版本转换清单
- delivery:  数据投递清单（账本）、幂等凭证、回滚留痕
- checks:    核对结论收集与报告渲染
- demo:      端到端场景编排
"""

__all__ = ["common", "sample_v1", "migration", "delivery", "checks", "demo"]
