"""核对结论收集与报告渲染。

每条核对结论记录：编号、名称、通过与否、证据、可选的明细。
最终渲染成终端摘要与 runtime/REPORT.md，便于部署过渡期人工复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Check:
    check_id: str
    name: str
    passed: bool
    evidence: str
    details: dict[str, Any] = field(default_factory=dict)


class CheckBook:
    def __init__(self) -> None:
        self._checks: list[Check] = []

    def record(self, check_id: str, name: str, passed: bool,
               evidence: str, **details: Any) -> Check:
        check = Check(check_id, name, bool(passed), evidence, details)
        self._checks.append(check)
        return check

    @property
    def checks(self) -> list[Check]:
        return list(self._checks)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self._checks)

    def render_text(self) -> str:
        lines: list[str] = []
        width = max((len(c.check_id) for c in self._checks), default=4)
        for c in self._checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"[{mark}] {c.check_id:<{width}}  {c.name}")
            lines.append(f"         证据: {c.evidence}")
        total = len(self._checks)
        passed = sum(1 for c in self._checks if c.passed)
        lines.append("")
        lines.append(f"核对汇总: {passed}/{total} 通过"
                     + ("，全部通过 ✅" if self.all_passed else "，存在失败 ❌"))
        return "\n".join(lines)

    def render_markdown(self) -> str:
        lines = [
            "# 批次版本迁移与投递核对报告",
            "",
            "| 结论 | 编号 | 核对项 | 证据 |",
            "| --- | --- | --- | --- |",
        ]
        for c in self._checks:
            mark = "✅ 通过" if c.passed else "❌ 失败"
            evidence = c.evidence.replace("|", "\\|")
            lines.append(f"| {mark} | `{c.check_id}` | {c.name} | {evidence} |")
        total = len(self._checks)
        passed = sum(1 for c in self._checks if c.passed)
        lines += [
            "",
            f"**核对汇总：{passed}/{total} 通过。**",
            "",
            "## 核对明细数据",
            "",
            "```json",
        ]
        import json
        lines.append(json.dumps(
            [c.__dict__ for c in self._checks],
            ensure_ascii=False, indent=2, sort_keys=True,
        ))
        lines += ["```", ""]
        return "\n".join(lines)
