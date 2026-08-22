"""Pipeline for processing a single turn of conversation."""

import asyncio
import logging
from dataclasses import replace

from agent.core.ids import identity_for_message
from agent.core.types import InboundMessage, OutboundMessage
from agent.observability.turn_trace import (
    TurnTrace,
    estimate_tokens,
    tool_names_from_schemas,
)
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.pipeline.invalidation_worker import InvalidationWorker
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase
from agent.pipeline.reasoner import Reasoner
from memory.store import MemoryStore
logger = logging.getLogger(__name__)


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
        self.last_trace: TurnTrace | None = None
        self.last_reasoner_result = None

    async def execute(self, inbound_message: InboundMessage) -> OutboundMessage:
        """Execute the full pipeline for a single turn."""
        identity = identity_for_message(
            user_id=inbound_message.user_id,
            chat_id=inbound_message.chat_id,
            channel=inbound_message.channel,
            metadata=inbound_message.metadata,
            turn_id=inbound_message.turn_id,
            trace_id=inbound_message.trace_id,
        )
        inbound_message = replace(
            inbound_message,
            channel=identity.session_key.split(":", 1)[0],
            turn_id=identity.turn_id,
            trace_id=identity.trace_id,
        )
        trace = TurnTrace(identity=identity)
        self.last_trace = trace

        try:
            outbound = await self._execute_traced(inbound_message, trace)
            logger.info("Turn trace: %s", trace.to_dict())
            return outbound
        except BaseException as error:
            trace.fail(error)
            logger.warning("Turn trace failed: %s", trace.to_dict())
            raise

    async def _execute_traced(
        self,
        inbound_message: InboundMessage,
        trace: TurnTrace,
    ) -> OutboundMessage:
        """Execute one turn while recording a payload-free regression trace."""
        # Phase 1: BeforeTurn - acquire session and retrieve memories
        trace.mark_phase("before_turn")
        turn_ctx = await self.before_turn.build_ctx(inbound_message)
        turn_ctx.turn_id = trace.identity.turn_id
        turn_ctx.trace_id = trace.identity.trace_id
        turn_ctx.session_key = trace.identity.session_key
        turn_ctx.session.session_key = trace.identity.session_key
        turn_ctx.session.channel = inbound_message.channel
        retrieval_trace = turn_ctx.retrieval_trace_raw
        if isinstance(retrieval_trace, dict):
            trace.retrieval_mode = str(retrieval_trace.get("retrieval_mode") or "")
        trace.retrieved_count = len(turn_ctx.retrieved_memories)
        if turn_ctx.abort:
            outbound = OutboundMessage(
                chat_id=inbound_message.chat_id,
                content=turn_ctx.abort_reply or "",
                turn_id=trace.identity.turn_id,
                trace_id=trace.identity.trace_id,
            )
            await self._dispatch_abort(outbound)
            trace.complete(finish_reason="before_turn_abort")
            return outbound

        # Phase 2: BeforeReasoning - prepare messages and tools for LLM
        trace.mark_phase("before_reasoning")
        reasoning_ctx = await self.before_reasoning.build_ctx(turn_ctx)
        reasoning_ctx.turn_id = trace.identity.turn_id
        reasoning_ctx.trace_id = trace.identity.trace_id
        reasoning_ctx.session_key = trace.identity.session_key
        trace.tools_visible = tool_names_from_schemas(reasoning_ctx.tools)
        trace.context_tokens_before = estimate_tokens(reasoning_ctx.messages)
        if reasoning_ctx.abort:
            outbound = OutboundMessage(
                chat_id=inbound_message.chat_id,
                content=reasoning_ctx.abort_reply or "",
                turn_id=trace.identity.turn_id,
                trace_id=trace.identity.trace_id,
            )
            await self._dispatch_abort(outbound)
            trace.complete(finish_reason="before_reasoning_abort")
            return outbound

        # Phase 3: Reasoner - call LLM and handle tool calls
        trace.mark_phase("reasoning")
        result = await self.reasoner.run_turn(reasoning_ctx)
        result.turn_id = trace.identity.turn_id
        result.trace_id = trace.identity.trace_id
        self.last_reasoner_result = result
        trace.context_tokens_after = estimate_tokens(reasoning_ctx.messages)

        # Phase 4: AfterReasoning - create outbound message and persist
        trace.mark_phase("after_reasoning")
        after_ctx = await self.after_reasoning.build_ctx(
            result=result,
            session=turn_ctx.session,
            chat_id=inbound_message.chat_id,
            user_id=inbound_message.user_id,
        )
        after_ctx.turn_id = trace.identity.turn_id
        after_ctx.trace_id = trace.identity.trace_id
        after_ctx.session_key = trace.identity.session_key

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
        trace.mark_phase("after_turn")
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

        trace.complete(
            finish_reason=result.finish_reason,
            tool_calls=result.tool_calls,
        )
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
        worker = self._invalidation
        if worker is None:
            return
        current_source_ref = _source_ref_for_last_turn(
            inbound_message.user_id,
            inbound_message.chat_id,
            len(session.messages),
        )

        async def _run():
            try:
                await worker.run(
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
        worker = self._consolidation
        store = self._store
        if worker is None or store is None:
            return

        if not worker.should_consolidate(session):
            return

        user_id = inbound_message.user_id
        chat_id = inbound_message.chat_id
        session_key = (user_id, chat_id)
        if session_key in self._consolidation_inflight:
            return
        self._consolidation_inflight.add(session_key)

        async def _run():
            try:
                await worker.consolidate(
                    session=session,
                    store=store,
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
