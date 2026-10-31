import json
from decimal import Decimal

import pytest

from app.providers import ProviderFailure
from scripts.live_anthropic_e2e import Ledger, MemoryCredentials, RESERVATION, MAX_CALLS


def test_one_ledger_never_refunds_and_blocks_excess(tmp_path):
    path = tmp_path / "budget.json"
    ledger = Ledger(path)
    try:
        for _ in range(MAX_CALLS):
            ledger.reserve("analysis")
            ledger.observe(
                {
                    "provider": "anthropic",
                    "model": "claude-haiku-5-5",
                    "input_tokens": 100,
                    "output_tokens": 200,
                }
            )
            ledger.finish("response_received")
        with pytest.raises(ProviderFailure):
            ledger.reserve("questions")
        assert sum(Decimal(x["reserved_usd"]) for x in ledger.data["entries"]) == RESERVATION * MAX_CALLS
        assert RESERVATION * MAX_CALLS < Decimal("10")
        assert json.loads(path.read_text())["entries"][-1]["status"] == "response_received"
    finally:
        ledger.close()


def test_exclusive_budget_lock_and_memory_credentials(tmp_path):
    ledger = Ledger(tmp_path / "budget.json")
    try:
        with pytest.raises(FileExistsError):
            Ledger(tmp_path / "budget.json")
        credentials = MemoryCredentials()
        ref = credentials.put("test-only-memory-key")
        assert credentials.get(ref) == "test-only-memory-key"
        credentials.delete(ref)
        assert credentials.get(ref) is None
        assert not any("key" in p.name for p in tmp_path.iterdir())
    finally:
        ledger.close()
