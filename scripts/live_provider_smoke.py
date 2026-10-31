"""Prepared one-call smoke test. Dry-run by default; no stored or discovered keys.

The operator must first confirm the exact model, current input/output prices and
budget. This script is not an account-level dollar quota. It bounds one request
and estimates a conservative token charge; the provider's billing is authoritative.
"""

import argparse
from decimal import Decimal
from getpass import getpass
import json
import time

from app.core import Settings
from app.providers import ProviderRegistry

SOURCE = "Momentum is conserved when total external force is zero."
PAYLOAD = {
    "instructions": "Return exactly a JSON object with answer and evidence_quote string fields. Answer when momentum is conserved. evidence_quote must be an exact substring of source.",
    "source": SOURCE,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One bounded OpenAI smoke request; default only prints a plan"
    )
    parser.add_argument("--model", required=True, help="Explicit approved openai:<model> ID")
    parser.add_argument("--budget-usd", type=Decimal, required=True)
    parser.add_argument(
        "--input-usd-per-million", type=Decimal, required=True, help="Operator-verified current price"
    )
    parser.add_argument(
        "--output-usd-per-million", type=Decimal, required=True, help="Operator-verified current price"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Prompt locally for key and make one generation"
    )
    args = parser.parse_args(argv)
    amounts = (args.budget_usd, args.input_usd_per_million, args.output_usd_per_million)
    if any(not x.is_finite() or x <= 0 for x in amounts):
        parser.error("Budget and verified token prices must be finite positive decimals")
    output_tokens = 256
    # Byte upper bound + large message framing reserve for this fixed ASCII fixture.
    input_bound = len(json.dumps(PAYLOAD).encode()) + 4096
    estimated = (
        input_bound * args.input_usd_per_million + output_tokens * args.output_usd_per_million
    ) / Decimal(1_000_000)
    registry = ProviderRegistry(
        Settings(profile="production", enabled_models=(args.model,)), max_completion_tokens=output_tokens
    )
    if estimated > args.budget_usd:
        parser.error("Conservative charge estimate exceeds the authorized budget")
    print(
        json.dumps(
            {
                "destination": "https://api.openai.com/v1",
                "model": args.model,
                "generation_calls": 1,
                "automatic_retries": 0,
                "max_completion_tokens": output_tokens,
                "estimated_upper_usd": str(estimated),
                "authorized_budget_usd": str(args.budget_usd),
                "source": "fixed nonprivate sample only",
                "dry_run": not args.execute,
            }
        )
    )
    if not args.execute:
        return 0
    if input("Confirm approved model, current prices, budget and one request; type RUN: ") != "RUN":
        print("Not executed.")
        return 1
    key = getpass("OpenAI API key (hidden; process memory only): ")
    started = time.monotonic()
    try:
        registry.validate_key(args.model, key)  # Model metadata GET, not a generation.
        result = registry.generate(args.model, key, "analysis", PAYLOAD)
        encoded = json.dumps(result)
        valid = (
            key not in encoded
            and set(result) == {"answer", "evidence_quote"}
            and all(isinstance(result[x], str) and result[x].strip() for x in result)
            and result["evidence_quote"] in SOURCE
        )
        print(
            json.dumps(
                {
                    "status": "pass" if valid else "invalid_output",
                    "generation_calls": 1,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
            )
        )
        return 0 if valid else 2
    except Exception:
        # Provider exception details may contain private data. Never echo them.
        print("Live smoke failed; no automatic retry. Check account usage before deciding to retry.")
        return 2
    finally:
        key = None


if __name__ == "__main__":
    raise SystemExit(main())
