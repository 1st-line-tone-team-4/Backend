import pytest

from scripts.live_provider_smoke import main


def test_live_harness_default_never_requests_key_or_connects(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry-run must not request key or connect")

    monkeypatch.setattr("scripts.live_provider_smoke.getpass", forbidden)
    monkeypatch.setattr("scripts.live_provider_smoke.ProviderRegistry.generate", forbidden)
    monkeypatch.setattr("scripts.live_provider_smoke.ProviderRegistry.validate_key", forbidden)
    assert (
        main(
            [
                "--model",
                "openai:example-model",
                "--budget-usd",
                "0.10",
                "--input-usd-per-million",
                "1",
                "--output-usd-per-million",
                "2",
            ]
        )
        == 0
    )
    assert '"dry_run": true' in capsys.readouterr().out


def test_live_harness_rejects_budget_before_key_entry(monkeypatch):
    monkeypatch.setattr("scripts.live_provider_smoke.getpass", lambda *args: pytest.fail("Must not read key"))
    with pytest.raises(SystemExit):
        main(
            [
                "--model",
                "openai:example-model",
                "--budget-usd",
                "0.000001",
                "--input-usd-per-million",
                "1",
                "--output-usd-per-million",
                "2",
            ]
        )
