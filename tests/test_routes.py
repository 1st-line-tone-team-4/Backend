import re
import json
from pathlib import Path
import pytest
from fastapi.openapi.utils import get_openapi

from app.core import Settings
from app.main import build_context
from tests.support import test_application as make_application


def test_published_integrated_contract_maps_to_real_routes(tmp_path):
    app = make_application(tmp_path).app

    def normalize(value):
        return re.sub(r"\{[^}]+\}", "{}", value)

    actual = get_openapi(title="Route inventory", version="test", routes=app.routes)
    implemented = {
        (method, normalize(path)) for path, operations in actual["paths"].items() for method in operations
    }
    document = app.openapi()
    expected = {
        (method, normalize("/v1" + path))
        for path, operations in document["paths"].items()
        for method in operations
        if method in ("get", "post", "patch", "delete", "put")
    }
    assert expected <= implemented
    assert ("get", "/v1/me") in expected
    assert ("get", "/v1/today") in expected
    assert any("teachback" in path for _, path in expected)
    # Contract 1.4.0 mounts only the read-only monthly subscription capability.
    assert ("get", "/v1/billing/capabilities") in expected
    assert not any(
        ("billing" in path and path != "/v1/billing/capabilities")
        or "score-claims" in path
        or "publications" in path
        for _, path in implemented
    )
    assert not any("validate-key" in path for _, path in implemented)
    assert len(expected) == 51
    documented = json.loads(
        (Path(__file__).resolve().parents[1] / "docs/openapi-full.json").read_text("utf-8")
    )
    assert documented == document
    app.state.context.db.engine.dispose()


def test_production_rejects_auth_provider_substitutes_and_schema_creation():
    with pytest.raises(ValueError, match="substitutes"):
        build_context(Settings(profile="production"), auth=object())
    with pytest.raises(ValueError, match="substitutes"):
        build_context(Settings(profile="development"), providers=object())
    with pytest.raises(ValueError, match="schema"):
        build_context(Settings(profile="production"), create_schema=True)
