"""Smoke tests for the Phase 3 CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cli.main import build_parser, cmd_reconstruct, cmd_slice, load_fixture_backend

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_PATH = REPO_ROOT / "fixture" / "fixture.json"


@pytest.fixture
def fixture_log() -> object:
    return load_fixture_backend(FIXTURE_PATH)


def test_parser_accepts_fixture_and_command() -> None:
    args = build_parser().parse_args(
        [
            "--fixture",
            str(FIXTURE_PATH),
            "--load-module",
            "examples.customer_approval",
            "slice",
            "A4",
        ]
    )
    assert args.command == "slice"
    assert args.event_id == "A4"
    assert args.fixture == FIXTURE_PATH
    assert args.load_module == ["examples.customer_approval"]


def test_cmd_slice_prints_fixture_nine(
    fixture_log: object, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd_slice(fixture_log, "A4")
    out = capsys.readouterr().out
    assert "9 events" in out
    assert "A1, B1, C1, B2, C2, B3, C3, A3, A4" in out


def test_cmd_reconstruct_prints_state_and_hash(
    fixture_log: object, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd_reconstruct(fixture_log, "B", 4)
    out = capsys.readouterr().out
    assert "status=active" in out
    state_line = out.splitlines()[1]
    parsed = json.loads(state_line)
    assert parsed["tool_outputs"]["B2"] == {"customer_status": "eligible"}


def test_customer_approval_example_runs_end_to_end(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "examples" / "customer_approval.py"),
            "--db",
            str(tmp_path / "customer-approval.db"),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "Captured customer approval failure" in result.stdout
    assert "Outcome: approved" in result.stdout
    assert (
        "Diagnosis: The terminal event is marked 'failure' because the decision returned "
        "'approved' with customer_status='eligible' and risk_score=0.2."
    ) in result.stdout
    assert "Minimal tested chain: customer_status + risk_score -> decision -> terminal failure" in (
        result.stdout
    )
    assert "The structural slice contains 8 events" in result.stdout
    assert "The reported interaction between customer_status and risk_score is 1.0" in result.stdout
    assert "Limitations:" in result.stdout
    assert "Explore further" in result.stdout
    assert "Raw evidence:" in result.stdout
    assert result.stdout.index("Offline explanation") < result.stdout.index("Diagnosis:")
