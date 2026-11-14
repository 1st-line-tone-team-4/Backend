from fastapi.testclient import TestClient

from tests.support import test_application as make_application

OWNER = {"Authorization": "Bearer test-owner-token"}
OTHER = {"Authorization": "Bearer test-other-token"}


def status(client, headers):
    response = client.get("/v1/billing/capabilities", headers=headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "monthly_subscription" and data["checkout_enabled"] is False
    return data["subscription_status"]


def test_demo_subscription_switch_is_per_user(tmp_path):
    app = make_application(tmp_path).app
    with TestClient(app) as client:
        assert status(client, OWNER) == "pricing_pending"
        on = client.put("/v1/billing/subscription", json={"active": True}, headers=OWNER)
        assert on.status_code == 200 and on.json()["data"]["subscription_status"] == "active"
        assert status(client, OWNER) == "active"
        assert status(client, OTHER) == "pricing_pending"
        off = client.put("/v1/billing/subscription", json={"active": False}, headers=OWNER)
        assert off.json()["data"]["subscription_status"] == "pricing_pending"
        assert status(client, OWNER) == "pricing_pending"
        assert client.put("/v1/billing/subscription", json={"active": "yes"}, headers=OWNER).status_code == 422
        assert client.put("/v1/billing/subscription", json={"active": True, "x": 1}, headers=OWNER).status_code == 422
        assert client.put("/v1/billing/subscription", json={"active": True}).status_code == 401
    app.state.context.db.engine.dispose()
