from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .config import CommandPolicySettings


@dataclass(frozen=True)
class CommandDecision:
    blocked: bool
    sensitive: bool
    reasons: list[str]


class CommandPolicy:
    def __init__(self, settings: CommandPolicySettings):
        self._settings = settings
        self._sensitive_patterns = [re.compile(pattern) for pattern in settings.sensitive_patterns]
        self._blocked_patterns = [re.compile(pattern) for pattern in settings.blocked_patterns]

    def evaluate(self, command: str) -> CommandDecision:
        reasons: list[str] = []
        blocked = False
        sensitive = False

        for pattern in self._blocked_patterns:
            if pattern.search(command):
                blocked = True
                reasons.append(f"blocked:{pattern.pattern}")

        for pattern in self._sensitive_patterns:
            if pattern.search(command):
                sensitive = True
                reasons.append(f"sensitive:{pattern.pattern}")

        return CommandDecision(blocked=blocked, sensitive=sensitive, reasons=reasons)

    def confirmation_token(self, command: str) -> str:
        digest = hashlib.sha256(command.encode("utf-8")).hexdigest().upper()
        return digest[: self._settings.confirmation_token_length]
