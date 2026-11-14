"""Monthly subscription state (contract 1.5.0).

Pricing and a payment provider are not decided yet, so checkout stays disabled.
`PUT /billing/subscription` is a demo switch: it marks the caller as subscribed
(`active`) or back to the free state (`pricing_pending`) without any payment.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from .core import get_context, get_user, utcnow
from .models import UserSubscription

router = APIRouter(tags=["Subscription"])


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: StrictBool


def _payload(status):
    return {
        "data": {
            "mode": "monthly_subscription",
            "subscription_status": status,
            "checkout_enabled": False,
        }
    }


@router.get("/billing/capabilities")
def billing_capabilities(request: Request):
    owner = get_user(request)
    with get_context(request).db.transaction() as session:
        row = session.get(UserSubscription, owner)
        return _payload(row.status if row else "pricing_pending")


@router.put("/billing/subscription")
def set_subscription(body: SubscriptionInput, request: Request):
    owner = get_user(request)
    status = "active" if body.active else "pricing_pending"
    with get_context(request).db.transaction() as session:
        row = session.get(UserSubscription, owner)
        if row is None:
            session.add(UserSubscription(user_id=owner, status=status, updated_at=utcnow()))
        else:
            row.status = status
            row.updated_at = utcnow()
    return _payload(status)
