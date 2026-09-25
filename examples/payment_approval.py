"""Run a captured payment approval failure and print commands to inspect it."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import cast

import casuality
from core.decision import register_decision_evaluator
from sdk.events import record_event

DECISION_TYPE = "examples.payment_approval.approve_payment"


def approve_payment(inputs: dict[str, object]) -> str:
    return (
        "approved"
        if inputs["currency"] == "USD" and cast(float, inputs["amount"]) <= 1000.0
        else "rejected"
    )


register_decision_evaluator(DECISION_TYPE, approve_payment)


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
    failure_event_id = ""
    outcome = ""

    try:
        @casuality.agent(role="currency-verifier")
        def verify_currency(_: str) -> str:
            return "USD"

        @casuality.agent(role="amount-calculator")
        def calculate_amount(_: str) -> float:
            # Bug/Failure scenario: Amount exceeds limit but passed approval
            return 800.0

        captured_approval = casuality.merge_decision(
            ports=["currency", "amount"],
            baselines={"currency": "EUR", "amount": 5000.0},
            decision_type=DECISION_TYPE,
        )(lambda currency, amount: approve_payment(
            {"currency": currency, "amount": amount}
        ))

        outcome = captured_approval(verify_currency("tx_123"), calculate_amount("tx_123"))
        decision_event = runtime.log.events()[-1]
        failure_event, _ = record_event(
            agent_id=runtime.agent_id,
            clock=runtime.clock,
            log=runtime.log,
            event_type="agent_finish",
            payload={
                "final_answer": outcome,
                "status": "failure",
                "note": "Payment exceeding $1000 limit was incorrectly processed.",
            },
            causal_parent_ids=[decision_event.id],
            causal_parent_seqs=[decision_event.logical_seq],
            run_id=runtime.run_id,
        )
        failure_event_id = failure_event.id
    finally:
        runtime.log.close()

    module_name = DECISION_TYPE.rsplit(".", 1)[0]
    print("Captured payment approval failure")
    print(f"Run: {runtime.run_id}")
    print(f"Database: {args.db}")
    print(f"Outcome: {outcome}")
    print(f"Failure event: {failure_event_id}", flush=True)

    print("\nOffline explanation", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "--load-module",
            module_name,
            "--db",
            str(args.db),
            "explain",
            failure_event_id,
            "--no-llm",
        ],
        check=True,
    )

    print("\nExplore further")
    print("Raw evidence:")
    print(
        f"uv run casuality --load-module {module_name} --db \"{args.db}\" "
        f"explain {failure_event_id} --raw-evidence --no-llm"
    )
    print("Optional LLM explanation:")
    print(
        f"uv run casuality --load-module {module_name} --db \"{args.db}\" "
        f"explain {failure_event_id} --model \"{args.model}\""
    )


if __name__ == "__main__":
    main()
