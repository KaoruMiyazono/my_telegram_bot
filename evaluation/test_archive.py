"""Build and verify an auditable archive from pytest JUnit output.

The archive is intentionally independent from pipeline hooks. It consumes test
artifacts after execution and records exactly what ran, which business scenarios
were covered, and whether every CI gate passed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import platform
import shutil
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PASSING_GATE_STATES = {"passed", "success"}


@dataclass(frozen=True)
class TestCaseResult:
    node_id: str
    file: str
    name: str
    classname: str
    duration_seconds: float
    status: str
    message: str = ""


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    tier: str
    title: str
    patterns: tuple[str, ...]
    required: bool = True


def parse_junit(path: Path) -> list[TestCaseResult]:
    """Turn a pytest JUnit XML file into stable, JSON-safe case records."""

    root = ET.parse(path).getroot()
    cases: list[TestCaseResult] = []
    for element in root.iter("testcase"):
        classname = element.attrib.get("classname", "")
        name = element.attrib.get("name", "")
        file_name = element.attrib.get("file", "") or _file_from_classname(classname)
        failure = element.find("failure")
        error = element.find("error")
        skipped = element.find("skipped")
        marker = failure if failure is not None else error
        if marker is None:
            marker = skipped
        if failure is not None:
            status = "failed"
        elif error is not None:
            status = "error"
        elif skipped is not None:
            status = "skipped"
        else:
            status = "passed"
        cases.append(
            TestCaseResult(
                node_id=f"{file_name}::{name}",
                file=file_name,
                name=name,
                classname=classname,
                duration_seconds=round(float(element.attrib.get("time", 0.0)), 6),
                status=status,
                message=(marker.attrib.get("message", "") if marker is not None else ""),
            )
        )
    return sorted(cases, key=lambda case: case.node_id)


def _file_from_classname(classname: str) -> str:
    parts = classname.split(".")
    if parts and parts[0] == "tests":
        return "/".join(parts) + ".py"
    return classname.replace(".", "/") + (".py" if classname else "")


def load_scenarios(path: Path) -> list[ScenarioSpec]:
    """Read the human-maintained business-scenario catalogue."""

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        ScenarioSpec(
            scenario_id=str(item["id"]),
            tier=str(item["tier"]),
            title=str(item["title"]),
            patterns=tuple(str(pattern) for pattern in item.get("patterns", [])),
            required=bool(item.get("required", True)),
        )
        for item in payload.get("scenario", [])
    ]


def scenario_evidence(
    scenarios: Sequence[ScenarioSpec], cases: Sequence[TestCaseResult]
) -> list[dict[str, Any]]:
    """Map business scenarios to the concrete pytest cases that prove them."""

    evidence: list[dict[str, Any]] = []
    for scenario in scenarios:
        matched = [
            case
            for case in cases
            if any(fnmatch.fnmatch(case.node_id, pattern) for pattern in scenario.patterns)
        ]
        statuses = Counter(case.status for case in matched)
        passed = statuses["passed"] > 0 and not any(
            status in statuses for status in ("failed", "error")
        )
        evidence.append(
            {
                "id": scenario.scenario_id,
                "tier": scenario.tier,
                "title": scenario.title,
                "required": scenario.required,
                "covered": bool(matched),
                "passed": passed,
                "matched_count": len(matched),
                "statuses": dict(sorted(statuses.items())),
                "evidence": [case.node_id for case in matched],
            }
        )
    return evidence


def build_archive(
    *,
    junit_path: Path,
    scenario_config: Path,
    gate_statuses: dict[str, str],
    commit_sha: str,
    branch: str,
    run_id: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one immutable test-run record from JUnit and CI gate outcomes."""

    cases = parse_junit(junit_path)
    scenarios = scenario_evidence(load_scenarios(scenario_config), cases)
    counts = Counter(case.status for case in cases)
    required = [scenario for scenario in scenarios if scenario["required"]]
    tests_ok = not counts["failed"] and not counts["error"] and bool(cases)
    scenarios_ok = all(item["covered"] and item["passed"] for item in required)
    gates_ok = all(value.lower() in PASSING_GATE_STATES for value in gate_statuses.values())
    timestamp = generated_at or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "run": {
            "run_id": run_id,
            "commit_sha": commit_sha,
            "branch": branch,
            "generated_at": timestamp.isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "summary": {
            "total": len(cases),
            "passed": counts["passed"],
            "failed": counts["failed"],
            "errors": counts["error"],
            "skipped": counts["skipped"],
            "duration_seconds": round(sum(case.duration_seconds for case in cases), 4),
            "pass_rate": round(counts["passed"] / len(cases), 4) if cases else 0.0,
        },
        "gates": dict(sorted(gate_statuses.items())),
        "scenario_summary": {
            "total": len(scenarios),
            "required": len(required),
            "required_covered": sum(item["covered"] for item in required),
            "required_passed": sum(item["covered"] and item["passed"] for item in required),
        },
        "scenarios": scenarios,
        "slowest_tests": [
            asdict(case)
            for case in sorted(cases, key=lambda case: case.duration_seconds, reverse=True)[:10]
        ],
        "test_cases": [asdict(case) for case in cases],
        "quality_gate_passed": tests_ok and scenarios_ok and gates_ok,
    }


def render_markdown(archive: dict[str, Any]) -> str:
    """Render the JSON archive as a compact report for humans."""

    summary = archive["summary"]
    scenario_summary = archive["scenario_summary"]
    lines = [
        "# M13 Test Archive",
        "",
        f"- Run: `{archive['run']['run_id']}`",
        f"- Commit: `{archive['run']['commit_sha']}`",
        f"- Generated: `{archive['run']['generated_at']}`",
        f"- Quality gate: **{'PASS' if archive['quality_gate_passed'] else 'FAIL'}**",
        "",
        "## Test result",
        "",
        "| Total | Passed | Failed | Error | Skipped | Pass rate | Duration |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {summary['total']} | {summary['passed']} | {summary['failed']} | "
            f"{summary['errors']} | {summary['skipped']} | "
            f"{summary['pass_rate']:.2%} | {summary['duration_seconds']:.2f}s |"
        ),
        "",
        "## CI gates",
        "",
    ]
    lines.extend(f"- {name}: `{status}`" for name, status in archive["gates"].items())
    lines.extend(
        [
            "",
            "## Business scenario evidence",
            "",
            (
                f"Required coverage: {scenario_summary['required_covered']}/"
                f"{scenario_summary['required']}"
            ),
            "",
            "| Tier | Scenario | Required | Covered | Passed | Cases |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in archive["scenarios"]:
        lines.append(
            f"| {item['tier']} | {item['title']} | {'yes' if item['required'] else 'no'} "
            f"| {'yes' if item['covered'] else 'no'} | {'yes' if item['passed'] else 'no'} "
            f"| {item['matched_count']} |"
        )
    lines.extend(["", "## Slowest tests", ""])
    for case in archive["slowest_tests"]:
        lines.append(f"- `{case['node_id']}` — {case['duration_seconds']:.4f}s")
    return "\n".join(lines) + "\n"


def write_archive(archive: dict[str, Any], output_dir: Path, junit_path: Path) -> Path:
    """Write immutable and latest JSON/Markdown/JUnit files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = _safe_name(str(archive["run"]["run_id"]))
    stem = f"test-archive-{run_id}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_text = json.dumps(archive, ensure_ascii=False, indent=2) + "\n"
    markdown_text = render_markdown(archive)
    json_path.write_text(json_text, encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")
    (output_dir / "latest.json").write_text(json_text, encoding="utf-8")
    (output_dir / "latest.md").write_text(markdown_text, encoding="utf-8")
    shutil.copy2(junit_path, output_dir / f"junit-{run_id}.xml")
    return json_path


def _safe_name(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in "-_." else "-" for character in value)
    return safe.strip("-.") or "local"


def verify_archive(path: Path) -> bool:
    """Return whether an archived run satisfies all required quality gates."""

    return bool(json.loads(path.read_text(encoding="utf-8"))["quality_gate_passed"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--junit", type=Path, required=True)
    build.add_argument("--scenario-config", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--pytest-status", default="unknown")
    build.add_argument("--pyright-status", default="unknown")
    build.add_argument("--rag-status", default="unknown")
    build.add_argument("--commit-sha", default="local")
    build.add_argument("--branch", default="local")
    build.add_argument("--run-id", default="local")
    verify = subparsers.add_parser("verify")
    verify.add_argument("archive", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify":
        passed = verify_archive(args.archive)
        print(f"M13 quality gate: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    archive = build_archive(
        junit_path=args.junit,
        scenario_config=args.scenario_config,
        gate_statuses={
            "pytest": args.pytest_status,
            "pyright": args.pyright_status,
            "rag_smoke": args.rag_status,
        },
        commit_sha=args.commit_sha,
        branch=args.branch,
        run_id=args.run_id,
    )
    path = write_archive(archive, args.output_dir, args.junit)
    print(f"Wrote M13 test archive: {path}")
    print(render_markdown(archive))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
