"""Smoke tests for the Phase 3 CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.main import build_parser, cmd_reconstruct, cmd_slice, load_fixture_backend

FIXTURE_PATH = Path(__file__).parent.parent / "fixture" / "fixture.json"


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
