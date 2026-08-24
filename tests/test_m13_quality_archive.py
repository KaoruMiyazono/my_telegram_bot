from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from agent.core.envelope import MessageEnvelope
from agent.core.session_lane import SessionLaneManager
from agent.runtime.cancellation import CancellationRegistry
from evaluation.proactive_metrics import ProactiveEvaluationCase, score_proactive_cases
from evaluation.test_archive import (
    build_archive,
    load_scenarios,
    parse_junit,
    render_markdown,
    verify_archive,
    write_archive,
)


def _write_junit(path, cases: list[tuple[str, str, str]]) -> None:
    bodies = []
    for file_name, name, status in cases:
        marker = "" if status == "passed" else f'<failure message="{status}" />'
        bodies.append(
            f'<testcase classname="{file_name[:-3].replace("/", ".")}" '
            f'name="{name}" file="{file_name}" time="0.01">{marker}</testcase>'
        )
    path.write_text(
        f'<testsuites><testsuite name="pytest">{"".join(bodies)}</testsuite></testsuites>',
        encoding="utf-8",
    )


def test_junit_parser_preserves_test_identity_status_and_duration(tmp_path) -> None:
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests/test_demo.py", "test_ok", "passed"),
            ("tests/test_demo.py", "test_bad", "failed"),
        ],
    )

    cases = parse_junit(junit)

    assert [case.node_id for case in cases] == [
        "tests/test_demo.py::test_bad",
        "tests/test_demo.py::test_ok",
    ]
    assert [case.status for case in cases] == ["failed", "passed"]
    assert cases[0].duration_seconds == 0.01


def test_archive_is_machine_readable_human_readable_and_verifiable(tmp_path) -> None:
    junit = tmp_path / "junit.xml"
    scenarios = tmp_path / "scenarios.toml"
    _write_junit(junit, [("tests/test_demo.py", "test_ok", "passed")])
    scenarios.write_text(
        """
[[scenario]]
id = "demo"
tier = "unit"
title = "Demo"
patterns = ["tests/test_demo.py::*"]
""".strip(),
        encoding="utf-8",
    )
    archive = build_archive(
        junit_path=junit,
        scenario_config=scenarios,
        gate_statuses={"pytest": "success", "pyright": "passed"},
        commit_sha="abc123",
        branch="main",
        run_id="42",
        generated_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    path = write_archive(archive, tmp_path / "archive", junit)

    assert verify_archive(path)
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["total"] == 1
    assert "Quality gate: **PASS**" in render_markdown(archive)
    assert (tmp_path / "archive/latest.json").exists()
    assert (tmp_path / "archive/junit-42.xml").exists()


def test_required_scenario_without_evidence_fails_quality_gate(tmp_path) -> None:
    junit = tmp_path / "junit.xml"
    scenarios = tmp_path / "scenarios.toml"
    _write_junit(junit, [("tests/test_demo.py", "test_ok", "passed")])
    scenarios.write_text(
        """
[[scenario]]
id = "missing"
tier = "e2e"
title = "Missing"
patterns = ["tests/test_missing.py::*"]
""".strip(),
        encoding="utf-8",
    )

    archive = build_archive(
        junit_path=junit,
        scenario_config=scenarios,
        gate_statuses={"pytest": "success"},
        commit_sha="abc",
        branch="main",
        run_id="local",
    )

    assert not archive["quality_gate_passed"]
    assert archive["scenario_summary"]["required_covered"] == 0


def test_proactive_metrics_measure_business_failures() -> None:
    metrics = score_proactive_cases(
        [
            ProactiveEvaluationCase("good", True, True, True, True, True),
            ProactiveEvaluationCase("noise", False, True, False, True, False),
            ProactiveEvaluationCase("miss", True, False),
            ProactiveEvaluationCase(
                "quiet", True, True, in_quiet_hours=True, passive_interfered=True
            ),
        ]
    )

    assert metrics["precision_of_push"] == 0.6667
    assert metrics["miss_rate"] == 0.3333
    assert metrics["duplicate_rate"] == 0.3333
    assert metrics["ack_accuracy"] == 0.6667
    assert metrics["quiet_hour_violations"] == 1
    assert metrics["passive_interference"] == 1


async def test_hundred_sessions_run_concurrently_but_each_session_is_serial() -> None:
    active_by_session: dict[str, int] = {}
    maximum_by_session: dict[str, int] = {}
    global_active = 0
    global_maximum = 0
    completed = 0
    done = asyncio.Event()

    async def handler(envelope: MessageEnvelope) -> None:
        nonlocal global_active, global_maximum, completed
        session = envelope.session_key
        active_by_session[session] = active_by_session.get(session, 0) + 1
        maximum_by_session[session] = max(
            maximum_by_session.get(session, 0), active_by_session[session]
        )
        global_active += 1
        global_maximum = max(global_maximum, global_active)
        await asyncio.sleep(0.005)
        global_active -= 1
        active_by_session[session] -= 1
        completed += 1
        if completed == 200:
            done.set()

    lanes = SessionLaneManager(handler, CancellationRegistry())
    try:
        for turn in range(2):
            for session in range(100):
                await lanes.submit(
                    MessageEnvelope(
                        message_id=f"m:{session}:{turn}",
                        session_key=f"telegram:{session}:{session}",
                        channel="telegram",
                        user_id=session,
                        chat_id=session,
                        client_message_id=f"client:{session}:{turn}",
                        payload={"turn_id": f"t:{session}:{turn}"},
                        created_at=datetime.now(timezone.utc),
                        direction="inbound",
                    )
                )
        await asyncio.wait_for(done.wait(), timeout=5)
        assert global_maximum > 1
        assert set(maximum_by_session.values()) == {1}
    finally:
        await lanes.close()


def test_real_scenario_catalog_has_all_m13_tiers() -> None:
    scenarios = load_scenarios(__import__("pathlib").Path("config/quality_scenarios.toml"))
    assert {scenario.tier for scenario in scenarios} >= {
        "unit",
        "integration",
        "e2e",
        "eval",
        "fault",
        "nightly",
    }
