from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DialogRequest:
    kind: str
    title: str
    message: str
    actions: tuple[str, ...]
    topmost: bool = True
    taskbar_visible: bool = True
    conspicuous: bool = True
    can_confirm: bool = True
    field_values: dict[str, str | bool] = field(default_factory=dict)
