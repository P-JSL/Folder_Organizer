"""Honest phase progress and elapsed/remaining-time estimates, independent of Qt."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable


@dataclass(frozen=True)
class ProgressSnapshot:
    phase: str
    completed: int
    total: int | None
    unit: str
    message: str
    fraction: float | None
    elapsed: float
    eta_seconds: float | None
    terminal: bool = False
    cancelled: bool = False


class ProgressTracker:
    """Keep counters monotonic within a phase, never invent discovery totals.

    Percentages refer to the reported phase, not the entire multi-phase task.
    ETA is shown only after work has been measured against a stable known total.
    """
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.started = self.clock()
        self.phase_started = self.started
        self.phase = ""
        self.completed = 0
        self.total: int | None = None

    def update(self, event: dict) -> ProgressSnapshot:
        if not isinstance(event, dict):
            raise ValueError("진행 정보는 사전이어야 합니다.")
        now = self.clock()
        phase = str(event.get("phase", self.phase))[:128]
        completed = event.get("completed", 0)
        total = event.get("total")
        completed = completed if type(completed) is int and completed >= 0 else 0
        total = total if type(total) is int and total >= 0 else None
        if phase != self.phase or total != self.total:
            self.phase_started, self.completed = now, 0
        completed = max(self.completed, completed)
        if total is not None:
            completed = min(completed, total)
        self.phase, self.completed, self.total = phase, completed, total
        cancelled = bool(event.get("cancelled", False))
        terminal = bool(event.get("terminal", False))
        elapsed = max(0.0, now - self.started)
        phase_elapsed = max(0.0, now - self.phase_started)
        fraction = completed / total if total else (1.0 if total == 0 and terminal and not cancelled else None)
        eta = None
        if total and 0 < completed < total and phase_elapsed >= 0.5 and not terminal:
            estimate = phase_elapsed * (total - completed) / completed
            if math.isfinite(estimate):
                eta = estimate
        if terminal and not cancelled and total is not None and completed == total:
            eta = 0.0
        return ProgressSnapshot(phase, completed, total, str(event.get("unit", "항목"))[:32],
                                str(event.get("message", ""))[:2048], fraction, elapsed, eta,
                                terminal, cancelled)
