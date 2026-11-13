import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import live_anthropic_e2e as live
from tests.support import test_application as make_test_application


TOKEN = "synthetic-cleanup-token-only"


@pytest.mark.parametrize("phase", ["validation", "interrupt", "client_entry", "request", "job"])
def test_execute_failure_clears_memory_and_disposes_database(tmp_path, monkeypatch, phase):
    fixture = make_test_application(tmp_path)
    dispose = Mock(wraps=fixture.ctx.db.engine.dispose)
    monkeypatch.setattr(fixture.ctx.db.engine, "dispose", dispose)
    monkeypatch.setattr(live, "make_test_application", lambda _: fixture)

    def validate(*_):
        fixture.ctx.credentials.put(TOKEN)
        if phase == "validation":
            raise RuntimeError("SYNTHETIC_VALIDATION_FAILURE")
        if phase == "interrupt":
            raise KeyboardInterrupt()
        return {"valid": True}

    monkeypatch.setattr(live, "BudgetProvider", lambda *_: SimpleNamespace(ensure_model=validate))
    client = MagicMock()
    client.headers = {}
    client.__enter__.return_value = client
    if phase == "client_entry":
        client.__enter__.side_effect = RuntimeError("SYNTHETIC_CLIENT_FAILURE")
    if phase == "request":
        client.post.side_effect = RuntimeError("SYNTHETIC_REQUEST_FAILURE")
    else:
        client.post.side_effect = [
            SimpleNamespace(
                status_code=201,
                json=lambda: {
                    "data": {
                        "upload_url": "/synthetic-upload",
                        "material": {"id": "synthetic-id"},
                    }
                },
            ),
            SimpleNamespace(status_code=202, json=lambda: {"data": {"job": {"id": "synthetic-job"}}}),
        ]
        client.put.return_value = SimpleNamespace(status_code=204)
    monkeypatch.setattr(live, "TestClient", lambda _: client)
    monkeypatch.setattr(fixture.ctx.jobs, "run_once", Mock(side_effect=RuntimeError("SYNTHETIC_JOB_FAILURE")))
    failure = KeyboardInterrupt if phase == "interrupt" else RuntimeError
    with pytest.raises(failure) as captured:
        live.execute(TOKEN, object(), tmp_path)
    assert isinstance(fixture.ctx.credentials, live.MemoryCredentials)
    assert fixture.ctx.credentials.items == {}
    dispose.assert_called_once_with()
    frames = []
    trace = captured.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name in {"execute", "post"}:
            frames.append(trace.tb_frame)
            assert trace.tb_frame.f_locals.get("key") is None
        trace = trace.tb_next
    assert frames
    # On Windows this also checks that dispose released pooled SQLite handles.
    database = tmp_path / "domain.sqlite3"
    database.rename(tmp_path / "disposed.sqlite3")
    assert all(TOKEN.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_execute_dispose_failure_still_clears_memory(tmp_path, monkeypatch):
    credentials = []
    dispose = Mock(side_effect=RuntimeError("SYNTHETIC_DISPOSE_FAILURE"))
    ctx = SimpleNamespace(db=SimpleNamespace(engine=SimpleNamespace(dispose=dispose)))
    monkeypatch.setattr(live, "make_test_application", lambda _: SimpleNamespace(ctx=ctx))

    def validate(*_):
        ctx.credentials.put(TOKEN)
        credentials.append(ctx.credentials)
        raise ValueError("SYNTHETIC_VALIDATION_FAILURE")

    monkeypatch.setattr(live, "BudgetProvider", lambda *_: SimpleNamespace(ensure_model=validate))
    with pytest.raises(RuntimeError, match="SYNTHETIC_DISPOSE_FAILURE"):
        live.execute(TOKEN, object(), tmp_path)
    dispose.assert_called_once_with()
    assert credentials[0].items == {}


def test_main_normalizes_input_without_echoing_key_or_arbitrary_errors(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "synthetic-input.txt"
    key_file.write_text('ANTHROPIC_API_KEY = "' + TOKEN + '"\n', encoding="utf-16")
    seen = []

    def fail_execute(key, ledger, directory):
        seen.append(key == TOKEN)
        assert isinstance(directory, Path)
        raise live.ProviderFailure(TOKEN)

    monkeypatch.setattr(live, "execute", fail_execute)
    ledger_path = tmp_path / "budget.json"
    assert live.main(["--key-file", str(key_file), "--ledger", str(ledger_path)]) == 1
    assert seen == [True]
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert json.loads(output.out)["report"]["error"] == "LIVE_CHECK_FAILED"
    assert TOKEN not in ledger_path.read_text()
    assert not ledger_path.with_suffix(".json.lock").exists()
