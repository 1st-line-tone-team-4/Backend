import json
import os
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.providers import ProviderFailure
from scripts import live_anthropic_e2e as live
from scripts.live_anthropic_e2e import BudgetProvider, Ledger, MemoryCredentials, RESERVATION, MAX_CALLS


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


@pytest.mark.parametrize(
    "contents",
    [
        "{invalid-json",
        "[]",
        '{"model":"' + live.MODEL + '","budget_usd":"10","entries":[],"entries":[],"runs":[]}',
        json.dumps({"model": "other-model", "budget_usd": "10", "entries": [], "runs": []}),
        json.dumps(
            {
                "model": live.MODEL,
                "budget_usd": "10",
                "entries": [
                    {"number": 1, "status": "outcome_unknown", "reserved_usd": str(RESERVATION)},
                ],
                "runs": [],
            }
        ),
        json.dumps(
            {
                "model": live.MODEL,
                "budget_usd": "10",
                "entries": [
                    {"number": 1, "status": "reserved", "reserved_usd": str(RESERVATION)},
                ],
                "runs": [],
            }
        ),
        json.dumps(
            {
                "model": live.MODEL,
                "budget_usd": "10",
                "entries": [
                    {"number": 1, "status": "response_received", "reserved_usd": "NaN"},
                ],
                "runs": [],
            }
        ),
        json.dumps(
            {
                "model": live.MODEL,
                "budget_usd": "10",
                "entries": [
                    {"number": 1, "status": "response_received", "reserved_usd": "-10"},
                ],
                "runs": [],
            }
        ),
    ],
)
def test_invalid_constructor_releases_lock_and_descriptor(tmp_path, monkeypatch, contents):
    path = tmp_path / "budget.json"
    path.write_text(contents, encoding="utf-8")
    opened = []
    original_open = os.open

    def record_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(live.os, "open", record_open)
    with pytest.raises(ValueError):
        Ledger(path)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not path.with_suffix(".json.lock").exists()
    assert path.read_text(encoding="utf-8") == contents
    path.rename(tmp_path / "original-budget.json")
    repaired = Ledger(path)
    repaired.close()


def test_close_attempts_unlink_even_when_close_reports_failure(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "budget.json")
    fd = ledger.lock_fd
    original_close = os.close

    def close_then_fail(value):
        original_close(value)
        raise OSError("synthetic-close-failure")

    monkeypatch.setattr(live.os, "close", close_then_fail)
    with pytest.raises(OSError):
        ledger.close()
    assert not ledger.lock.exists()
    with pytest.raises(OSError):
        os.fstat(fd)
    ledger.close()


def test_close_can_retry_lock_unlink_after_descriptor_is_closed(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "budget.json")
    fd = ledger.lock_fd
    original_unlink = live.Path.unlink

    def fail_unlink(path, **kwargs):
        if path == ledger.lock:
            raise OSError("synthetic-unlink-failure")
        return original_unlink(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(live.Path, "unlink", fail_unlink)
        with pytest.raises(OSError):
            ledger.close()
    assert ledger.lock.exists()
    with pytest.raises(OSError):
        os.fstat(fd)
    ledger.close()
    assert not ledger.lock.exists()


def test_reservation_failure_never_dispatches_or_reserves_again(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "budget.json")
    ledger.write()
    provider = BudgetProvider(ledger)
    provider.actual = Mock()

    def fail_replace(*_):
        raise OSError("synthetic-write-failure")

    monkeypatch.setattr(live.os, "replace", fail_replace)
    try:
        with pytest.raises(OSError):
            provider.generate(live.MODEL, "synthetic-only-token", "analysis", {})
        provider.actual.generate.assert_not_called()
        assert not ledger.path.with_suffix(".json.tmp").exists()
        assert json.loads(ledger.path.read_text())["entries"] == []
        with pytest.raises(ProviderFailure):
            ledger.reserve("analysis")
    finally:
        ledger.close()


def test_unknown_outcome_blocks_retries_in_current_and_later_run(tmp_path):
    path = tmp_path / "budget.json"
    ledger = Ledger(path)
    provider = BudgetProvider(ledger)
    provider.actual = Mock()
    provider.actual.generate.side_effect = ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True)
    try:
        with pytest.raises(ProviderFailure):
            provider.generate(live.MODEL, "synthetic-only-token", "analysis", {})
        with pytest.raises(ProviderFailure):
            provider.generate(live.MODEL, "synthetic-only-token", "analysis", {})
        assert provider.actual.generate.call_count == 1
        assert len(ledger.data["entries"]) == 1
        assert ledger.data["entries"][0]["status"] == "outcome_unknown"
    finally:
        ledger.close()
    with pytest.raises(ValueError):
        Ledger(path)
    assert not path.with_suffix(".json.lock").exists()


def test_main_final_write_failure_closes_lock_without_secret_diagnostics(tmp_path, monkeypatch, capsys):
    token = "synthetic-only-token"
    key_file = tmp_path / "synthetic-input.txt"
    key_file.write_text(token, encoding="utf-8")
    path = tmp_path / "budget.json"
    monkeypatch.setattr(live, "execute", lambda *_: {"status": "pass", "model": live.MODEL})

    def fail_write(_):
        raise OSError(token)

    monkeypatch.setattr(Ledger, "write", fail_write)
    assert live.main(["--key-file", str(key_file), "--ledger", str(path)]) == 1
    assert not path.with_suffix(".json.lock").exists()
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert json.loads(output.out)["report"]["error"] == "LEDGER_WRITE_FAILED"


def test_main_invalid_ledger_never_reads_credential_or_prints_ledger(tmp_path, monkeypatch, capsys):
    path = tmp_path / "budget.json"
    sentinel = "synthetic-secret-sentinel"
    path.write_text(sentinel, encoding="utf-8")
    parse = Mock(side_effect=AssertionError("must not parse"))
    monkeypatch.setattr(live, "parse_credential_bytes", parse)
    assert live.main(["--key-file", str(tmp_path / "does-not-exist"), "--ledger", str(path)]) == 1
    parse.assert_not_called()
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err
    assert not path.with_suffix(".json.lock").exists()
