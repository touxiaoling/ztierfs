import json

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Issue:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    repairable: bool = False
    repaired: bool = False


@dataclass
class CheckReport:
    command: str
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def has_unrepaired(self) -> bool:
        return any(not issue.repaired for issue in self.issues)

    @property
    def successful(self) -> bool:
        return not self.has_unrepaired

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "ok": self.successful,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass
class StatsReport:
    inodes: dict[str, int]
    entries: dict[str, int]
    chunks: dict[str, int]
    blocks: dict[str, int]
    storage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



def report_to_text(report: CheckReport) -> str:
    if not report.issues:
        return f"{report.command}: ok"
    lines = [f"{report.command}: {len(report.issues)} issue(s)"]
    for issue in report.issues:
        state = "repaired" if issue.repaired else "unrepaired"
        lines.append(f"- {issue.code}: {issue.message} [{state}]")
        if issue.details:
            lines.append(f"  {json.dumps(issue.details, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def stats_to_text(report: StatsReport) -> str:
    data = report.to_dict()
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"{section}:")
        for key, value in values.items():
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)
