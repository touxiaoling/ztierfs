"""fsck/stats 等命令的结构化报告与人可读文本、JSON 序列化。

`CheckReport.ok`：未发现任何 issue（`issues` 为空）。`CheckReport.successful`：不存在仍为「未修复」的 issue（每条均已修复，或列表为空）。二者不同：可有 issue 但全部已修复，此时非 ok 但仍 successful。
JSON：`CheckReport.to_dict()` 里键名 `"ok"` 对应属性 `successful`，不是属性 `ok`（历史命名）。
`Issue.repairable`/`repaired` 由 checker 语义填充；`StatsReport` 各字段为独立 SQL 聚合计数。
"""

import json

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Issue:
    """单条检查发现。

    `code`/`message`：分类与说明。`details`：附加键值（并入 JSON/issue 字典）。
    `repairable`：策略上是否允许尝试自动修复（未必实际执行）。`repaired`：本次流程是否已将该条视为修复完成。
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    repairable: bool = False
    repaired: bool = False


@dataclass
class CheckReport:
    """一次检查运行的汇总结果。

    `issues`：本轮发现的全部条目（含已修复与未修复）。
    """

    command: str
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        """是否「完全干净」：`issues` 为空（无任何发现）。"""
        return not self.issues

    @property
    def has_unrepaired(self) -> bool:
        """是否存在至少一条 `repaired` 为假的 issue。"""
        return any(not issue.repaired for issue in self.issues)

    @property
    def successful(self) -> bool:
        """是否无未修复残留：等价于「不存在未修复 issue」（含列表为空）。与 `ok` 不同：可有 issue 但均已修复时仍为 successful。"""
        return not self.has_unrepaired

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典：`command`、`issues`（嵌套 issue 字段），以及 `"ok"`（取值自 `successful`，非属性 `ok`）。"""
        return {
            "command": self.command,
            "ok": self.successful,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass
class StatsReport:
    """维护类统计快照（各字典值为 SQL 聚合得到的整数计数）。

    `inodes`/`entries`/`chunks`/`blocks`/`storage`：彼此独立的分类汇总；结构适合直接 `json.dumps(report.to_dict())`。
    """

    inodes: dict[str, int]
    entries: dict[str, int]
    chunks: dict[str, int]
    blocks: dict[str, int]
    storage: dict[str, int]
    maintenance: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """浅封装 `dataclasses.asdict`，得到与字段同结构的嵌套字典，便于 JSON 输出。"""
        return asdict(self)


def report_to_text(report: CheckReport) -> str:
    """将检查报告格式化为多行人可读字符串（非 JSON）。

    无 issue 时返回一行：报告 `command` 字段值后接字面量 `: ok`。否则首行给出 issue 数量，随后每条为「code: message [repaired|unrepaired]」；
    若某条含 `details`，下一行缩进打印该字典的单行 JSON（`ensure_ascii=False`，键排序）。
    """
    if not report.issues:
        return f"{report.command}: ok"
    lines = [f"{report.command}: {len(report.issues)} issue(s)"]
    for issue in report.issues:
        state = "repaired" if issue.repaired else "unrepaired"
        lines.append(f"- {issue.code}: {issue.message} [{state}]")
        if issue.details:
            lines.append(
                f"  {json.dumps(issue.details, ensure_ascii=False, sort_keys=True)}"
            )
    return "\n".join(lines)


def stats_to_text(report: StatsReport) -> str:
    """将统计报告格式化为分段人可读文本（非 JSON）：逐节标题下两行空格缩进一行「键: 值」，顺序与 `StatsReport.to_dict()` 一致。"""
    data = report.to_dict()
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"{section}:")
        for key, value in values.items():
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)
