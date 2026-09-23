"""Run a small captured approval failure and print commands to inspect it."""

from __future__ import annotations

import argparse
from pathlib import Path

import casuality
from core.decision import register_decision_evaluator
from sdk.events import record_event

DECISION_TYPE = "examples.customer_approval.approve_customer"


def approve_customer(inputs: dict[str, object]) -> str:
    return (
        "approved"
        if inputs["customer_status"] == "eligible" and inputs["risk_score"] < 0.5
        else "rejected"
    )


register_decision_evaluator(DECISION_TYPE, approve_customer)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(".casuality/example.db"),
        help="SQLite database path (default: .casuality/example.db)",
    )
    parser.add_argument(
        "--model",
        default="nex-agi/nex-n2.5-mini:free",
        help="OpenRouter model to show in the optional LLM command",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime = casuality.init(args.db)

    @casuality.agent(role="researcher")
    def research(_: str) -> str:
        return "eligible"

    @casuality.agent(role="risk-evaluator")
    def assess_risk(_: str) -> float:
        return 0.2

    captured_approval = casuality.merge_decision(
        ports=["customer_status", "risk_score"],
        baselines={"customer_status": "ineligible", "risk_score": 0.8},
        decision_type=DECISION_TYPE,
    )(lambda customer_status, risk_score: approve_customer(
        {"customer_status": customer_status, "risk_score": risk_score}
    ))

    outcome = captured_approval(research("customer X"), assess_risk("customer X"))
    decision_event_id = runtime.log.events()[-1].id
    failure_event, _ = record_event(
        agent_id=runtime.agent_id,
        clock=runtime.clock,
        log=runtime.log,
        event_type="agent_finish",
        payload={
            "final_answer": outcome,
            "status": "failure",
            "note": "The customer should not have been approved.",
        },
        causal_parent_ids=[decision_event_id],
        run_id=runtime.run_id,
    )

    print(f"Outcome: {outcome}")
    print(f"Database: {args.db}")
    print(f"Failure event: {failure_event.id}")
    print("\nOffline explanation:")
    print(
        f"uv run casuality --load-module {DECISION_TYPE.rsplit('.', 1)[0]} "
        f"--db {args.db} explain {failure_event.id} --no-llm"
    )
    print("\nOptional LLM explanation:")
    print(
        f"uv run casuality --load-module {DECISION_TYPE.rsplit('.', 1)[0]} "
        f"--db {args.db} explain {failure_event.id} --model {args.model}"
    )


if __name__ == "__main__":
    main()
