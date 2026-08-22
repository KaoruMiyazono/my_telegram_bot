"""
评估运行器 —— 复刻 akashic-agent eval/longmemeval/run.py

用法：
  python eval/runner.py                           # 跑全部绿集
  python eval/runner.py --trace                   # 输出检索日志
  python eval/runner.py --compare baseline.json   # 对比基线，检测回归
  python eval/runner.py --trace --compare baseline.json  # 两者同时

trace 输出（对应 akashic trace.log）：
  每个用例的检索召回 top-N 记忆 → 哪些命中了 gold_answer 的关键词
  → 可以看到检索器到底召回了什么

compare 输出（对应 akashic resume 机制）：
  逐用例对比 F1/Judge 变化 → 标记回归(↓)和改进(↑)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.core.types import InboundMessage, OutboundMessage
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase
from agent.pipeline.reasoner import Reasoner
from agent.tool_hooks import ToolExecutor
from agent.tools import ToolRegistry
from agent.tools.memory import register_memory_tools
from config.settings import settings
from evaluation.dataset_builder import EvalDataset, EvalCase
from evaluation.metrics import evaluate_single, score_results, format_score_report, token_f1
from evaluation.seed_memory import seed_from_mock_conversations
from memory.bootstrap import build_memory_runtime, default_markdown_memory_root
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import init_db
from persistence.session_store import get_session_store

logger = logging.getLogger("eval.runner")

_DEFAULT_TIMEOUT_S = 60.0
_EVAL_USER_ID = 9000

# ── Adapter ──────────────────────────────────────────────────────────────

class EvalAdapter:
    def __init__(self) -> None:
        self.sent_messages: list[OutboundMessage] = []
    async def send(self, message: OutboundMessage) -> None:
        self.sent_messages.append(message)
    async def start(self) -> None: pass
    async def stop(self) -> None: pass


# ── Pipeline assembly ────────────────────────────────────────────────────

async def _create_pipeline() -> PassiveTurnPipeline:
    embedder = Embedder()
    store = MemoryStore(embedder)
    session_store = get_session_store()
    memory_runtime = build_memory_runtime(
        embedder=embedder,
        memory_store=store,
        session_store=session_store,
    )
    from agent.core.event_bus import EventBus
    event_bus = EventBus.get_instance()
    tool_registry = ToolRegistry()
    tool_executor = ToolExecutor()
    register_memory_tools(tool_registry, memory_runtime.engine)
    before_turn = BeforeTurnPhase(
        event_bus=event_bus,
        memory_engine=memory_runtime.engine,
    )
    before_reasoning = BeforeReasoningPhase(
        benchmark_mode=True,
        tool_registry=tool_registry,
        event_bus=event_bus,
        self_model_reader=memory_runtime.markdown.store.read_self,
        long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
        recent_context_reader=memory_runtime.markdown.store.read_recent_context,
    )
    await before_reasoning.preheat()
    reasoner = Reasoner(
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        event_bus=event_bus,
    )
    after_reasoning = AfterReasoningPhase(store)
    after_turn = AfterTurnPhase(event_bus, EvalAdapter())
    return PassiveTurnPipeline(
        before_turn=before_turn, before_reasoning=before_reasoning,
        reasoner=reasoner, after_reasoning=after_reasoning, after_turn=after_turn,
        store=store,
        consolidation_worker=None,  # eval 不触发窗口 consolidation（每次 --fresh + 独立 seed）
        memory_runtime=memory_runtime,
    )

# ── QA instance (with trace capture) ─────────────────────────────────────

async def run_qa_instance(
    pipeline: PassiveTurnPipeline,
    inst: EvalCase,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    trace: bool = False,
) -> dict[str, Any]:
    """运行单个 QA 用例。trace=True 时捕获检索日志。"""
    t0 = time.monotonic()
    error: str | None = None
    predicted = ""
    retrieval_trace: list[dict] = []
    tool_calls: list[dict] = []

    try:
        inbound = InboundMessage(
            user_id=_EVAL_USER_ID, chat_id=_EVAL_USER_ID, content=inst.question,
        )
        outbound = await asyncio.wait_for(
            pipeline.execute(inbound), timeout=timeout_s,
        )
        predicted = outbound.content if outbound else ""

        # 捕获检索日志（BeforeTurnPhase 记录的）
        if trace:
            bt = pipeline.before_turn
            for m in bt.last_retrieved:
                retrieval_trace.append({
                    "id": str(m.id),
                    "type": m.memory_type,
                    "summary": m.summary[:120],
                    "source_ref": m.source_ref,
                })

        # 捕获 tool_calls（从 Reasoner 的结果）
        # 通过 pipeline.reasoner 无法直接读，但从 ReasonerResult 可以
        # run_turn 返回 ReasonerResult，但我们没有保存引用
        # 简化：如果 trace，在 pipeline.execute 后从 ctx 读

    except asyncio.TimeoutError:
        error = f"timeout after {timeout_s}s"
    except Exception as exc:
        error = str(exc)

    elapsed = time.monotonic() - t0

    eval_result = evaluate_single(
        predicted_answer=predicted, gold_answer=inst.gold_answer, question=inst.question,
    )

    result: dict[str, Any] = {
        "question_id": inst.case_id,
        "question_type": inst.question_type.value,
        "question": inst.question,
        "gold_answer": inst.gold_answer,
        "predicted_answer": predicted,
        "token_f1": eval_result["token_f1"],
        "exact_match": eval_result["exact_match"],
        "judge_correct": eval_result.get("rule_judge_correct"),
        "elapsed_s": round(elapsed, 2),
        "error": error,
    }

    if trace:
        result["retrieval_trace"] = retrieval_trace
        # HyDE 信息
        bt = pipeline.before_turn
        if hasattr(bt, "_last_hyde_used") and bt._last_hyde_used:
            result["hyde_hypothesis"] = getattr(bt, "_last_hypothesis", None)
            result["hyde_used"] = True

    return result


# ── Ingest helpers ───────────────────────────────────────────────────────

def _clear_memories(store: MemoryStore) -> None:
    """清空所有 memory 数据（对应 akashic 重置 workspace）"""
    from persistence.database import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM memory_items")
    conn.execute("DELETE FROM vec_items")
    conn.commit()
    shutil.rmtree(
        default_markdown_memory_root() / "users" / str(_EVAL_USER_ID),
        ignore_errors=True,
    )


# ── Trace printer ────────────────────────────────────────────────────────

def _print_trace(result: dict[str, Any]) -> None:
    """打印检索日志"""
    gold = result.get("gold_answer", "")

    # HyDE 信息
    hyde_used = result.get("hyde_used")
    hyde_hypothesis = result.get("hyde_hypothesis")
    if hyde_used:
        print(f"\n  🧠 HyDE 假想记忆: {hyde_hypothesis}")

    trace_items = result.get("retrieval_trace", [])
    if not trace_items:
        print("  (未捕获检索日志)")
        return

    # 从 gold_answer 提取关键词，检查召回覆盖率
    gold_keywords = set()
    for kw in gold.replace(" ", ""):
        if "\u4e00" <= kw <= "\u9fff" or kw.isalpha():
            gold_keywords.add(kw)

    print(f"\n  检索召回 (top {len(trace_items)}):")
    hit_keywords: set[str] = set()
    for i, item in enumerate(trace_items):
        summary = item["summary"]
        src = item.get("source_ref", "") or ""
        src_short = f" [↗ {src[:30]}]" if src else ""
        # 检查覆盖率
        matched = [kw for kw in gold_keywords if kw in summary]
        hit_keywords.update(matched)
        match_mark = f"  ← 命中:{','.join(matched)}" if matched else ""
        print(f"    {i+1}. [{item['type']}] {summary[:80]}{src_short}{match_mark}")

    if gold_keywords:
        coverage = len(hit_keywords) / len(gold_keywords) * 100 if gold_keywords else 0
        missing = gold_keywords - hit_keywords
        print(f"  关键词覆盖率: {coverage:.0f}%  ({len(hit_keywords)}/{len(gold_keywords)})")
        if missing:
            print(f"  未召回关键词: {''.join(sorted(missing))}")


# ── Compare ──────────────────────────────────────────────────────────────

def _compare_results(
    current: list[dict[str, Any]],
    baseline_path: Path,
) -> None:
    """对比当前结果与基线，检测回归和改进。"""
    if not baseline_path.exists():
        print(f"\n⚠️  基线文件不存在: {baseline_path}")
        return

    baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_results = baseline_data.get("results", [])

    # 按 question_id 索引
    baseline_by_id = {r["question_id"]: r for r in baseline_results}

    regressions: list[dict] = []
    improvements: list[dict] = []
    stable: list[dict] = []

    for r in current:
        qid = r["question_id"]
        base = baseline_by_id.get(qid)
        if base is None:
            continue

        f1_delta = r["token_f1"] - base["token_f1"]
        judge_before = base.get("judge_correct", False)
        judge_after = r.get("judge_correct", False)

        entry = {
            "question_id": qid,
            "question": r["question"][:50],
            "f1_before": base["token_f1"],
            "f1_after": r["token_f1"],
            "f1_delta": round(f1_delta, 4),
            "judge_before": judge_before,
            "judge_after": judge_after,
        }

        # 退化：Judge 从 True→False，或 F1 下降 > 0.05
        if (judge_before and not judge_after) or f1_delta < -0.05:
            regressions.append(entry)
        # 改进：Judge 从 False→True，或 F1 上升 > 0.05
        elif (not judge_before and judge_after) or f1_delta > 0.05:
            improvements.append(entry)
        else:
            stable.append(entry)

    print(f"\n{'='*55}")
    print("回归检测  (vs baseline)")
    print(f"{'='*55}")

    if regressions:
        print(f"\n🔴 退化 ({len(regressions)} 个):")
        for e in regressions:
            arrow = "↓" if e["f1_delta"] < 0 else "→"
            judge_str = f"  judge: {e['judge_before']}→{e['judge_after']}"
            print(f"  {e['question_id']}  F1: {e['f1_before']:.3f}→{e['f1_after']:.3f} ({arrow}{abs(e['f1_delta']):.3f}){judge_str}")
    else:
        print("\n🟢 无退化")

    if improvements:
        print(f"\n🟢 改进 ({len(improvements)} 个):")
        for e in improvements:
            arrow = "↑" if e["f1_delta"] > 0 else "→"
            judge_str = f"  judge: {e['judge_before']}→{e['judge_after']}"
            print(f"  {e['question_id']}  F1: {e['f1_before']:.3f}→{e['f1_after']:.3f} ({arrow}{abs(e['f1_delta']):.3f}){judge_str}")

    print(f"\n稳定: {len(stable)} | 退化: {len(regressions)} | 改进: {len(improvements)}")


# ── Result printer ───────────────────────────────────────────────────────

def _print_result(result: dict[str, Any]) -> None:
    if result["error"]:
        status = "❌ ERROR"
    elif result["exact_match"]:
        status = "✅"
    elif result.get("judge_correct"):
        status = "⚠️"
    else:
        status = "❌"
    f1_str = f"F1={result['token_f1']:.3f}"
    pred = (result["predicted_answer"] or "(empty)")[:80]
    print(f"  {status} {f1_str}  {pred}")
    if result["error"]:
        print(f"  err: {result['error']}")


# ── CLI ──────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="RAG eval runner")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 个")
    p.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_S, help="超时秒数")
    p.add_argument("--output", type=Path, default=None, help="结果 JSON 路径")
    p.add_argument("--quiet", action="store_true", help="安静模式")
    p.add_argument("--trace", action="store_true", help="输出每次检索的召回日志")
    p.add_argument("--compare", type=Path, default=None, help="对比基线 JSON，检测回归")
    p.add_argument("--set", type=str, default="green", help="数据集: green | red | both")
    p.add_argument("--fresh", action="store_true", help="清空旧记忆后再播种（避免之前跑的数据污染）")
    return p


async def _main(args: argparse.Namespace) -> None:
    dataset = EvalDataset()
    instances: list[EvalCase] = []

    if args.set in ("green", "both"):
        instances.extend(dataset.load_green_set())
    if args.set in ("red", "both"):
        red_path = Path(__file__).parent.parent / "data" / "evaluation" / "red_set.json"
        if red_path.exists():
            red_data = json.loads(red_path.read_text(encoding="utf-8"))
            instances.extend([EvalCase.from_dict(d) for d in red_data])
        elif args.set == "red":
            print("❌ red_set.json 不存在。先创建 data/evaluation/red_set.json")
            sys.exit(1)

    if not instances:
        print("❌ 没有加载到任何用例")
        sys.exit(1)

    if args.limit > 0:
        instances = instances[:args.limit]

    n_total = len(instances)
    mode = "LIVE"
    set_label = args.set.upper()

    print(f"\n{'='*55}")
    print(f"RAG Eval — {mode} mode  |  {set_label} set  |  {n_total} instances")
    if args.trace:
        print(f"Trace: ON  |  输出检索召回日志")
    if args.compare:
        print(f"Compare: {args.compare}")
    print(f"{'='*55}")

    t_start = time.monotonic()

    init_db()
    pipeline = await _create_pipeline()

    # Ingest
    if args.fresh:
        # 清空旧记忆（对应 akashic 每次 eval 用隔离 workspace）
        store = pipeline.after_reasoning.store
        _clear_memories(store)
        if not args.quiet:
            print("Cleared old memories")

    if not args.quiet:
        print("Ingesting mock conversations...")
    store = pipeline.after_reasoning.store
    seeded = await seed_from_mock_conversations(store, eval_user_id=_EVAL_USER_ID)
    if not args.quiet:
        print(f"  {len(seeded)} sessions, {sum(len(v) for v in seeded.values())} memories")

    # Consolidation：提炼结构化摘要（对应 akashic ConsolidationService）
    if not args.quiet:
        print("Consolidating...")
    from evaluation.consolidation import consolidate_sessions
    n_summaries = await consolidate_sessions(store, seeded, _EVAL_USER_ID)
    if not args.quiet:
        print(f"  {n_summaries} structured summaries extracted")

    # QA
    results: list[dict[str, Any]] = []
    for i, inst in enumerate(instances):
        short_id = inst.case_id[:8]
        qt = inst.question_type.value
        if not args.quiet:
            print(f"\n[{i+1:03d}/{n_total}] {short_id}  {qt}")

        result = await run_qa_instance(pipeline, inst, timeout_s=args.timeout, trace=args.trace)
        results.append(result)

        if not args.quiet:
            _print_result(result)
            if args.trace:
                _print_trace(result)

    elapsed = time.monotonic() - t_start

    # Score
    scores = score_results(results)

    if args.quiet:
        print()
    report = format_score_report(scores)
    print(f"\n{report}")
    print(f"Elapsed: {elapsed:.1f}s")

    # Compare
    if args.compare:
        _compare_results(results, args.compare)

    # Save
    output = args.output
    if output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = Path(__file__).parent.parent / "data" / "evaluation" / "results" / f"{ts}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "live",
        "set": args.set,
        "n_instances": n_total,
        "elapsed_s": round(elapsed, 1),
        "scores": scores,
        "results": results,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved → {output}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
