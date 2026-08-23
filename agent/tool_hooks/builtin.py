from __future__ import annotations

from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.redaction import redact_sensitive
from agent.tool_hooks.types import HookContext, HookOutcome


class SensitiveOutputRedactionHook(ToolHook):
    """Remove common credential shapes from every successful output."""

    name = "builtin:sensitive_output_redaction"
    event = "after_call"

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        redacted = redact_sensitive(ctx.result)
        changed = redacted != ctx.result
        return HookOutcome(
            output_updated=changed,
            updated_output=redacted,
            audit_metadata={"sensitive_output_redacted": changed},
        )
