from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ModelInfo:
    model_id: str
    owned_by: str | None = None


@dataclass
class ProbeResult:
    success: bool
    status_code: int | None
    latency_ms: int | None
    message: str
    raw: dict[str, Any] | list[Any] | str | None = None


@dataclass
class BalanceResult:
    is_valid: bool = True
    invalid_message: str | None = None
    plan_name: str | None = None
    remaining: float | None = None
    used: float | None = None
    total: float | None = None
    unit: str | None = None
    raw: Any = None
