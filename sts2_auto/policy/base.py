"""Policy interface: map a decision point to one action."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Action:
    """One engine action, with an optional explanation for logs."""

    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    score: Optional[float] = None

    def as_command(self) -> Dict[str, Any]:
        cmd: Dict[str, Any] = {"cmd": "action", "action": self.name}
        if self.args:
            cmd["args"] = self.args
        return cmd


class Policy:
    """Base class; subclasses implement act()."""

    name = "base"

    def act(self, state: Dict[str, Any]) -> Action:  # pragma: no cover - abstract
        raise NotImplementedError

    def reset(self) -> None:
        """Called before each run; clear per-run memory here."""
