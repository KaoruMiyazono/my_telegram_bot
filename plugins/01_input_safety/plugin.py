from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.lifecycle.phase import PhaseFrame
from agent.plugins import Plugin

_VERDICT_SLOT = "safety:input_verdict"
_ABORT_REPLY_SLOT = "session:abort_reply"
_ALLOWED_CONTROLS = {"\t", "\n", "\r"}


@dataclass(frozen=True)
class InputSafetyPolicy:
    max_chars: int = 12_000
    reject_null_bytes: bool = True
    reject_control_characters: bool = True


class InputSafetyModule:
    """Validate inbound transport boundaries before retrieval and reasoning."""

    slot = "input_safety.validate"
    requires = ("before_turn.acquire_session",)
    produces = (_VERDICT_SLOT,)
    output_types = {_VERDICT_SLOT: dict}

    def __init__(self, policy: InputSafetyPolicy) -> None:
        self._policy = policy

    async def run(self, frame: PhaseFrame[Any, Any]) -> PhaseFrame[Any, Any]:
        content = str(getattr(frame.input, "content", "") or "")
        reason = self._rejection_reason(content)
        frame.slots[_VERDICT_SLOT] = {
            "accepted": reason is None,
            "reason": reason or "",
            "chars": len(content),
            "max_chars": self._policy.max_chars,
        }
        if reason is not None:
            frame.slots[_ABORT_REPLY_SLOT] = reason
        return frame

    def _rejection_reason(self, content: str) -> str | None:
        if len(content) > self._policy.max_chars:
            return (
                "消息过长，暂时无法处理。"
                f"请将内容拆分为不超过 {self._policy.max_chars} 个字符的多条消息。"
            )
        if self._policy.reject_null_bytes and "\x00" in content:
            return "消息包含无法处理的 NUL 控制字符，请清理后重新发送。"
        if self._policy.reject_control_characters and any(
            ord(char) < 32 and char not in _ALLOWED_CONTROLS
            for char in content
        ):
            return "消息包含无法处理的控制字符，请清理后重新发送。"
        return None


class InputSafetyPlugin(Plugin):
    name = "input_safety"
    version = "1.0.0"
    desc = "Validate inbound message boundaries before expensive Agent work"
    author = "Zhiyong Zheng"

    def before_turn_modules(self) -> list[object]:
        config = self.context.config
        max_chars = int(config.get("max_chars", 12_000)) if config else 12_000
        if max_chars < 1:
            raise ValueError("input_safety.max_chars must be at least 1")
        policy = InputSafetyPolicy(
            max_chars=max_chars,
            reject_null_bytes=(
                bool(config.get("reject_null_bytes", True)) if config else True
            ),
            reject_control_characters=(
                bool(config.get("reject_control_characters", True))
                if config
                else True
            ),
        )
        return [InputSafetyModule(policy)]
