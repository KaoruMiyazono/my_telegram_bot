from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class ToolDiscoveryState:
    """Small per-session LRU used only to preload recently used tools.

    Current-turn unlocks do not live here. They are stored on the turn-local
    ``BeforeReasoningCtx.tools`` list, which keeps concurrent turns isolated.
    """

    capacity: int = 4
    session_capacity: int = 1024
    _session_tools: dict[str, OrderedDict[str, None]] = field(default_factory=dict)
    _sessions: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def get_preloaded(self, session_key: str) -> list[str]:
        tools = self._session_tools.get(session_key)
        if tools is None:
            return []
        self._touch_session(session_key)
        return list(tools.keys())

    def remember(self, session_key: str, tool_name: str) -> None:
        if not session_key or not tool_name or self.capacity <= 0:
            return
        tools = self._session_tools.setdefault(session_key, OrderedDict())
        tools[tool_name] = None
        tools.move_to_end(tool_name)
        while len(tools) > self.capacity:
            tools.popitem(last=False)
        self._touch_session(session_key)

    def forget_tool(self, tool_name: str) -> None:
        empty_sessions: list[str] = []
        for session_key, tools in self._session_tools.items():
            tools.pop(tool_name, None)
            if not tools:
                empty_sessions.append(session_key)
        for session_key in empty_sessions:
            self._session_tools.pop(session_key, None)
            self._sessions.pop(session_key, None)

    def _touch_session(self, session_key: str) -> None:
        self._sessions[session_key] = None
        self._sessions.move_to_end(session_key)
        while len(self._sessions) > self.session_capacity:
            evicted, _ = self._sessions.popitem(last=False)
            self._session_tools.pop(evicted, None)
