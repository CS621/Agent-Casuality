"""Tests for Phase 6: Grounded Causal Explanation layer."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cli.main import FixtureEventLog, build_parser, cmd_explain
from core.explain import (
    build_evidence_package,
    explain,
    explanation_matches_format,
    render_evidence_summary,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixture" / "fixture.json"


@pytest.fixture
def fixture_log() -> FixtureEventLog:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return FixtureEventLog(data)


def test_build_evidence_package_failure_a4(fixture_log: FixtureEventLog) -> None:
    pkg = build_evidence_package("A4", fixture_log)

    assert pkg["target_event_id"] == "A4"
    assert pkg["target_event"]["agent_id"] == "A"
    assert pkg["target_event"]["payload"]["status"] == "failure"

    # Structural slice: 9 events
    s_slice = pkg["structural_slice"]
    assert s_slice["count"] == 9
    assert set(s_slice["event_ids"]) == {"A1", "B1", "C1", "B2", "C2", "B3", "C3", "A3", "A4"}

    # Decision contract resolved
    decision = pkg["decision"]
    assert decision is not None
    assert decision["decision_id"] == "dec_customer_approval_A3"
    assert "customer_status" in decision["ports"]
    assert "risk_score" in decision["ports"]

    # Minimal slice via ddmin: 4 events
    min_slice = pkg["minimal_slice"]
    assert min_slice is not None
    assert min_slice["count"] == 4
    assert min_slice["event_ids"] == ["B3", "C3", "A3", "A4"]
    assert min_slice["structural_count"] == 9
    assert min_slice["reduction_ratio"] > 0.5

    # Interaction attribution
    interaction = pkg["interaction_attribution"]
    assert interaction is not None
    assert "interactions" in interaction
    assert "B3_x_C3" in interaction["interactions"]
    assert interaction["interactions"]["B3_x_C3"]["value"] == 1.0

    # Provenance tracking
    prov = pkg["provenance"]
    assert len(prov) > 0
    # A3.output.approve must have all exact edges
    approve_chain = next(p for p in prov if p["target_field"] == "A3.output.approve")
    assert approve_chain["all_exact"] is True
    assert approve_chain["has_coarse"] is False
    assert len(approve_chain["edges"]) == 4

    # Agent state reconstruction
    states = pkg["agent_states"]
    assert "A" in states
    assert states["A"]["status"] == "failure"


def test_build_evidence_package_decision_a3(fixture_log: FixtureEventLog) -> None:
    pkg = build_evidence_package("A3", fixture_log)
    assert pkg["target_event_id"] == "A3"
    assert pkg["decision"] is not None
    assert pkg["decision"]["decision_id"] == "dec_customer_approval_A3"


def test_build_evidence_package_non_decision_event(fixture_log: FixtureEventLog) -> None:
    pkg = build_evidence_package("D3", fixture_log)
    assert pkg["target_event_id"] == "D3"
    assert pkg["structural_slice"]["count"] == 3
    assert set(pkg["structural_slice"]["event_ids"]) == {"D1", "D2", "D3"}
    # D3 is from an independent monitor agent with no decision contract
    assert pkg["decision"] is None
    assert pkg["minimal_slice"] is None


def test_explain_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is not set"):
        explain({"test": "evidence"}, api_key=None, load_env=False)


def test_explain_with_mock_transport() -> None:
    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        req_json = json.loads(request.content)
        assert req_json["model"] == "qwen/qwen3.8-27b:free"
        assert req_json["messages"][0]["role"] == "system"
        assert "strictly grounded" in req_json["messages"][0]["content"]
        assert "counterfactual comparison" in req_json["messages"][0]["content"]
        assert "A4" in req_json["messages"][1]["content"]

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Event A4 failed because A3 executed policy_check_v2 on inputs "
                                "from B3 and C3. Minimal slice isolates B3, C3, A3, A4. "
                                "Shapley interaction reveals joint interaction B3 x C3 = 1.0. "
                                "Provenance chain is exact."
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    evidence = {"target_event_id": "A4", "minimal_slice": ["B3", "C3", "A3", "A4"]}
    result = explain(evidence, api_key="sk-test-key", client=client)

    assert "Event A4 failed" in result
    assert "B3 x C3" in result
    assert len(captured_requests) == 1
    assert captured_requests[0].headers["authorization"] == "Bearer sk-test-key"


def test_explain_retry_on_429() -> None:
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "Rate limit exceeded"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Explanation after retry."}}]},
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    result = explain(
        {"target_event_id": "A4"},
        api_key="sk-test-key",
        client=client,
        max_retries=2,
        initial_delay=0.01,
    )
    assert result == "Explanation after retry."
    assert attempts == 2


def test_explain_429_exhausted() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "rate limit pool depleted"}},
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    with pytest.raises(RuntimeError, match="rate-limited"):
        explain(
            {"target_event_id": "A4"},
            api_key="sk-test-key",
            client=client,
            max_retries=1,
            initial_delay=0.01,
        )


def test_cli_explain_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["explain", "A4", "--no-llm", "--raw-evidence"])
    assert args.command == "explain"
    assert args.event_id == "A4"
    assert args.no_llm is True
    assert args.raw_evidence is True


def test_cli_cmd_explain_no_llm(
    fixture_log: FixtureEventLog, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd_explain(fixture_log, "A4", no_llm=True, raw_evidence=True)
    out = capsys.readouterr().out
    assert '"target_event_id": "A4"' in out
    assert "Diagnosis:" in out
    assert "Minimal causal chain: B3 -> C3 -> A3 -> A4." in out
    assert "Limitations:" in out


def test_render_evidence_summary_distinguishes_joint_failure(fixture_log: FixtureEventLog) -> None:
    pkg = build_evidence_package("A4", fixture_log)
    summary = render_evidence_summary(pkg)

    assert "status 'eligible' from B3 (marked as an incorrect lookup)" in summary
    assert "the risk input is marked correct on its own" in summary
    assert "The reported interaction between ['customer_status', 'risk_score'] is 1.0" in summary
    assert "conflicting downstream outputs" not in summary
    assert "why the upstream tools produced these values" in summary


def test_explanation_format_guard_requires_minimal_event_ids() -> None:
    evidence = {"minimal_slice": {"event_ids": ["B3", "C3", "A3", "A4"]}}
    valid = (
        "Diagnosis: A4 failed.\nEvidence:\n- B3 and C3 fed A3.\n"
        "Limitations: upstream causes are unknown. B3 C3 A3 A4"
    )
    invalid = "Here is the evidence: {\"A4\": \"failure\"}"

    assert explanation_matches_format(valid, evidence) is True
    assert explanation_matches_format(invalid, evidence) is False


def test_cli_falls_back_when_llm_ignores_output_contract(
    fixture_log: FixtureEventLog,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cli.main.explain",
        lambda *_args, **_kwargs: (
            "A long unstructured research report without the required sections."
        ),
    )

    cmd_explain(fixture_log, "A4")
    out = capsys.readouterr().out

    assert "LLM returned an unusable explanation" in out
    assert "marked as an incorrect lookup" in out
    assert "A long unstructured research report" not in out


def test_explain_acceptance_ground_truth_alignment(fixture_log: FixtureEventLog) -> None:
    """Validate acceptance criteria from implementation-plan.md L776-782:

    Must cite B3 and C3 correctly, identify the interaction rather than blaming one,
    and verify that provenance is exact, not coarse.
    """
    pkg = build_evidence_package("A4", fixture_log)

    # 1. Minimal slice contains B3 and C3
    assert set(pkg["minimal_slice"]["event_ids"]) == {"B3", "C3", "A3", "A4"}

    # 2. Interaction attribution isolates B3 x C3 with value 1.0
    interaction = pkg["interaction_attribution"]["interactions"]["B3_x_C3"]
    assert interaction["value"] == 1.0
    assert interaction["ports"] == ["customer_status", "risk_score"]

    # 3. Provenance chain used is exact, not coarse
    for prov_chain in pkg["provenance"]:
        if prov_chain["target_field"] == "A3.output.approve":
            assert prov_chain["all_exact"] is True
            assert prov_chain["has_coarse"] is False
