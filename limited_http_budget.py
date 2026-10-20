"""In-memory HTTP budget guard for later limited live measurements.

This is deliberately not connected to production fetching code yet. It has no
file/cache output and no built-in network transport; callers must inject a
transport function.
"""
from dataclasses import dataclass, field
from typing import Any, Callable


class HttpBudgetExceeded(Exception):
    pass


@dataclass
class HttpBudget:
    per_line: int = 15
    total: int = 75
    per_line_counts: dict[str, int] = field(default_factory=dict)
    total_count: int = 0

    def remaining_for(self, line_id: str) -> int:
        return min(self.per_line - self.per_line_counts.get(line_id, 0), self.total - self.total_count)

    def claim(self, line_id: str) -> None:
        if self.total_count >= self.total:
            raise HttpBudgetExceeded("total_http_budget_exceeded")
        current = self.per_line_counts.get(line_id, 0)
        if current >= self.per_line:
            raise HttpBudgetExceeded("line_http_budget_exceeded")
        self.per_line_counts[line_id] = current + 1
        self.total_count += 1


Transport = Callable[[str, str, dict[str, Any] | None], Any]


class BudgetedTransport:
    def __init__(self, transport: Transport, budget: HttpBudget | None = None):
        self.transport = transport
        self.budget = budget or HttpBudget()

    def request(self, line_id: str, url: str, *, kind: str = "page", meta: dict[str, Any] | None = None) -> Any:
        self.budget.claim(line_id)
        payload = dict(meta or {})
        payload["kind"] = kind
        return self.transport(url, line_id, payload)
