"""Pipeline for processing a single turn of conversation."""

import asyncio

from agent.core.types import InboundMessage, OutboundMessage
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.pipeline.invalidation_worker import InvalidationWorker
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase
from agent.pipeline.reasoner import Reasoner
from memory.store import MemoryStore
from uuid import UUID, uuid4


class PassiveTurnPipeline:
    """Pipeline for processing a single turn of conversation."""

    def __init__(
        self,
        before_turn: BeforeTurnPhase,
        before_reasoning: BeforeReasoningPhase,
        reasoner: Reasoner,
        after_reasoning: AfterReasoningPhase,
        after_turn: AfterTurnPhase,
        store: MemoryStore | None = None,
        consolidation_worker: ConsolidationWorker | None = None,
        invalidation_worker: InvalidationWorker | None = None,
        memory_runtime: object | None = None,
    ) -> None:
        self.before_turn = before_turn
        self.before_reasoning = before_reasoning
        self.reasoner = reasoner
        self.after_reasoning = after_reasoning
        self.after_turn = after_turn
        self._store = store
        self._consolidation = consolidation_worker
        self._invalidation = invalidation_worker
        self._memory_runtime = memory_runtime
        self._consolidation_inflight: set[tuple[int, int]] = set()

    async def execute(self, inbound_message: InboundMessage) -> OutboundMessage:
        """Execute the full pipeline for a single turn."""
        # Phase 1: BeforeTurn - acquire session and retrieve memories
        turn_ctx = await self.before_turn.build_ctx(inbound_message)
        if turn_ctx.abort:
            outbound = OutboundMessage(
                chat_id=inbound_message.chat_id,
                content=turn_ctx.abort_reply or "",
            )
            await self._dispatch_abort(outbound)
            return outbound

        # Phase 2: BeforeReasoning - prepare messages and tools for LLM
        reasoning_ctx = await self.before_reasoning.build_ctx(turn_ctx)
        if reasoning_ctx.abort:
            outbound = OutboundMessage(
                chat_id=inbound_message.chat_id,
                content=reasoning_ctx.abort_reply or "",
            )
            await self._dispatch_abort(outbound)
            return outbound

        # Phase 3: Reasoner - call LLM and handle tool calls
        result = await self.reasoner.run_turn(reasoning_ctx)
        self.last_reasoner_result = result

        # Phase 4: AfterReasoning - create outbound message and persist
        after_ctx = await self.after_reasoning.build_ctx(
            result=result,
            session=turn_ctx.session,
            chat_id=inbound_message.chat_id,
            user_id=inbound_message.user_id,
        )

        # Persist messages（对应 akashic PostResponseWorker：异步，不阻塞回复） 持久化记忆，但是现在还没做
        asyncio.create_task(
            self.after_reasoning.persist_messages(
                session=turn_ctx.session,
                user_message=inbound_message.content,
                assistant_message=result.content,
                user_id=inbound_message.user_id,
                chat_id=inbound_message.chat_id,
            )
        )

        # Phase 5: AfterTurn - emit event and send message
        new_memory_ids = []  # persist 异步，此处不再等待
        await self.after_turn.execute(
            ctx=after_ctx,
            user_id=inbound_message.user_id,
            new_memory_ids=new_memory_ids,
            inbound_content=inbound_message.content,
        )

        # Update session with new messages
        turn_ctx.session.messages.append({
            "role": "user",
            "content": inbound_message.content,
        })
        turn_ctx.session.messages.append({
            "role": "assistant",
            "content": result.content,
        })
        #  逐步讲解 PassiveTurnPipeline 在五个核心阶段完成后，如何追加并保存 Session 原始消息、刷新 RECENT_CONTEXT.md、异步提取长期记忆，以及检测并淘汰被用户纠正的旧记忆。
        # Persist session（对应 akashic sm.save(session)）
        from persistence.session_store import get_session_store
        get_session_store().save(
            inbound_message.user_id,
            inbound_message.chat_id,
            turn_ctx.session.messages,
            last_consolidated=turn_ctx.session.last_consolidated,
        )
        self._refresh_markdown_recent_turns(turn_ctx.session, inbound_message.user_id)

        # ── 窗口期 consolidation（对应 akashic on_turn_committed → _enqueue_maintenance）──
        # 从积累的 Session 原始对话中，提取少量半年后仍可能有用的长期事实。
        self._maybe_consolidate(turn_ctx.session, inbound_message)
        self._maybe_invalidate(inbound_message, result, turn_ctx.session)

        return after_ctx.outbound_message

    def _refresh_markdown_recent_turns(self, session, user_id: int) -> None:
        runtime = self._memory_runtime
        markdown = getattr(runtime, "markdown", None)
        store = getattr(markdown, "store", None)
        if store is None:
            return
        try:
            store.write_recent_turns(
                user_id=user_id,
                messages=session.messages,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Markdown recent turns refresh failed user=%d",
                user_id,
            )

    def _maybe_invalidate(self, inbound_message: InboundMessage, result, session) -> None:
        """Run akashic-style post-response invalidation asynchronously."""
        if self._invalidation is None:
            return
        current_source_ref = _source_ref_for_last_turn(
            inbound_message.user_id,
            inbound_message.chat_id,
            len(session.messages),
        )

        async def _run():
            try:
                await self._invalidation.run(
                    user_msg=inbound_message.content,
                    agent_response=result.content,
                    tool_calls=result.tool_calls,
                    user_id=inbound_message.user_id,
                    chat_id=inbound_message.chat_id,
                    source_ref=current_source_ref,
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Invalidation failed user=%d chat=%d",
                    inbound_message.user_id,
                    inbound_message.chat_id,
                )

        asyncio.create_task(_run())

    async def _dispatch_abort(self, outbound: OutboundMessage) -> None:
        adapter = getattr(self.after_turn, "telegram_adapter", None)
        if adapter is not None:
            await adapter.send(outbound)

    def _maybe_consolidate(
        self,
        session,
        inbound_message: InboundMessage,
    ) -> None:
        """
        对齐 akashic on_turn_committed → _enqueue_maintenance：
          每轮对话后异步检查是否攒够新消息，触发 LLM 提取长期记忆。

        fire-and-forget，不阻塞用户回复。
        """
        if self._consolidation is None or self._store is None:
            return

        if not self._consolidation.should_consolidate(session):
            return

        user_id = inbound_message.user_id
        chat_id = inbound_message.chat_id
        session_key = (user_id, chat_id)
        if session_key in self._consolidation_inflight:
            return
        self._consolidation_inflight.add(session_key)

        async def _run():
            try:
                await self._consolidation.consolidate(
                    session=session,
                    store=self._store,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                from persistence.session_store import get_session_store
                get_session_store().save(
                    user_id,
                    chat_id,
                    session.messages,
                    last_consolidated=session.last_consolidated,
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Consolidation failed user=%d chat=%d", user_id, chat_id,
                )
            finally:
                self._consolidation_inflight.discard(session_key)

        asyncio.create_task(_run())


def _source_ref_for_last_turn(user_id: int, chat_id: int, message_count: int) -> str:
    if message_count >= 2:
        return f"session:{user_id}:{chat_id}#msg:{message_count - 2}-{message_count - 1}"
    return f"session:{user_id}:{chat_id}"
