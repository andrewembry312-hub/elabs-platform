"""
billing_router.py — Stripe subscription billing for E-Labs Platform
────────────────────────────────────────────────────────────────────
Mounted at /api/billing in app.py:
    from billing_router import router as _billing_router
    app.include_router(_billing_router, prefix="/api/billing")

Environment variables required (production):
    STRIPE_SECRET_KEY          sk_live_... or sk_test_...
    STRIPE_WEBHOOK_SECRET      whsec_...
    STRIPE_COPILOT_PRO_PRICE   price_... (Copilot Pro, $19/mo)
    STRIPE_COPILOT_TEAM_PRICE  price_... (Copilot Team, $49/mo)
    STRIPE_MACHINE_STARTER_PRICE price_... (Machine Starter, $29/mo)
    STRIPE_MACHINE_PRO_PRICE   price_... (Machine Pro, $79/mo)
    PUBLIC_DOMAIN              copilot.elabsai.com  (used to build redirect URLs)

Usage limits (enforced by get_user_tier() in app.py):
    Free     — 50 messages / month, 0 machine runs
    Pro      — unlimited messages, unlimited machine runs
    Team     — unlimited messages, unlimited machine runs + org features
    Starter  — unlimited messages, 100 machine workflow runs / month
"""

import os
import sqlite3
import time
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from auth_router import get_current_user, _bearer_scheme  # noqa: F401 — shared auth

log = logging.getLogger("billing")

# ── Stripe configuration ──────────────────────────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
_PUBLIC_DOMAIN = os.environ.get("PUBLIC_DOMAIN", "copilot.elabsai.com")

# Price IDs (created once in the Stripe Dashboard → Products → Add product)
_PRICE_IDS: dict[str, str] = {
    "copilot:pro":      os.environ.get("STRIPE_COPILOT_PRO_PRICE", ""),
    "copilot:team":     os.environ.get("STRIPE_COPILOT_TEAM_PRICE", ""),
    "machine:starter":  os.environ.get("STRIPE_MACHINE_STARTER_PRICE", ""),
    "machine:pro":      os.environ.get("STRIPE_MACHINE_PRO_PRICE", ""),
}

# ── Usage limits per tier ─────────────────────────────────────────────────────
TIER_LIMITS: dict[str, dict] = {
    "free":    {"messages_per_month": 50,  "machine_runs_per_month": 0,   "all_models": False, "persistent_memory": False, "tools": False},
    "pro":     {"messages_per_month": -1,  "machine_runs_per_month": -1,  "all_models": True,  "persistent_memory": True,  "tools": True},
    "team":    {"messages_per_month": -1,  "machine_runs_per_month": -1,  "all_models": True,  "persistent_memory": True,  "tools": True,  "org_features": True},
    "starter": {"messages_per_month": -1,  "machine_runs_per_month": 100, "all_models": False, "persistent_memory": True,  "tools": True},
}

# ── DB helpers ────────────────────────────────────────────────────────────────
_DB_PATH = os.path.join(os.path.dirname(__file__), "elabs_users.db")


@contextmanager
def _db():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_billing_tables():
    """Create billing-related tables if they don't exist yet."""
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                INTEGER NOT NULL,
            org_id                 INTEGER,
            product                TEXT NOT NULL,          -- 'copilot' | 'machine'
            tier                   TEXT NOT NULL,          -- 'free'|'pro'|'team'|'starter'
            stripe_customer_id     TEXT,
            stripe_subscription_id TEXT,
            status                 TEXT DEFAULT 'active',  -- 'active'|'canceled'|'past_due'
            current_period_end     INTEGER,               -- unix timestamp
            created_at             INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS organizations (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            owner_id           INTEGER NOT NULL,
            plan               TEXT DEFAULT 'free',
            stripe_customer_id TEXT,
            created_at         INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS org_members (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id        INTEGER NOT NULL,
            user_id       INTEGER,
            role          TEXT DEFAULT 'member',   -- 'owner'|'admin'|'member'
            invited_email TEXT,
            invite_token  TEXT UNIQUE,
            accepted_at   INTEGER,
            created_at    INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY(org_id) REFERENCES organizations(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS usage_counters (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            org_id       INTEGER,
            product      TEXT NOT NULL,    -- 'copilot' | 'machine'
            metric       TEXT NOT NULL,    -- 'messages' | 'workflow_runs'
            count        INTEGER DEFAULT 0,
            period_start INTEGER NOT NULL,
            period_end   INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_subs_user    ON subscriptions(user_id);
        CREATE INDEX IF NOT EXISTS idx_subs_stripe  ON subscriptions(stripe_subscription_id);
        CREATE INDEX IF NOT EXISTS idx_usage_user   ON usage_counters(user_id, product, metric, period_end);
        CREATE INDEX IF NOT EXISTS idx_org_members  ON org_members(org_id, user_id);
        """)


_init_billing_tables()

# ── Tier resolution ───────────────────────────────────────────────────────────

def _month_period() -> tuple[int, int]:
    """Return (period_start, period_end) for the current calendar month (UTC)."""
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def get_user_tier(user_id: int, product: str = "copilot") -> dict:
    """
    Return the effective tier dict for a user on a given product.
    Checks for active subscription; falls back to 'free'.
    """
    with _db() as conn:
        now = int(time.time())
        row = conn.execute(
            """SELECT tier FROM subscriptions
               WHERE user_id = ? AND product = ? AND status = 'active'
                 AND (current_period_end IS NULL OR current_period_end > ?)
               ORDER BY id DESC LIMIT 1""",
            (user_id, product, now),
        ).fetchone()
        tier = row["tier"] if row else "free"
    limits = dict(TIER_LIMITS.get(tier, TIER_LIMITS["free"]))
    limits["tier"] = tier
    return limits


def check_and_increment_usage(user_id: int, product: str, metric: str) -> bool:
    """
    Check if user is within their usage limit for this metric.
    Increments counter if allowed; returns True if allowed, False if over limit.
    """
    tier_info = get_user_tier(user_id, product)
    limit_key = f"{metric}_per_month"
    limit = tier_info.get(limit_key, 0)
    if limit == -1:
        # Unlimited — no DB write needed for enforcement, just allow
        return True
    if limit == 0:
        return False

    period_start, period_end = _month_period()
    with _db() as conn:
        row = conn.execute(
            """SELECT id, count FROM usage_counters
               WHERE user_id = ? AND product = ? AND metric = ?
                 AND period_start = ? AND period_end = ?""",
            (user_id, product, metric, period_start, period_end),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO usage_counters(user_id, product, metric, count, period_start, period_end)
                   VALUES(?, ?, ?, 1, ?, ?)""",
                (user_id, product, metric, period_start, period_end),
            )
            return True
        current = row["count"]
        if current >= limit:
            return False
        conn.execute(
            "UPDATE usage_counters SET count = count + 1 WHERE id = ?",
            (row["id"],),
        )
        return True


# ── Router ────────────────────────────────────────────────────────────────────
router = APIRouter(tags=["billing"])


class CheckoutRequest(BaseModel):
    product: str  # 'copilot' | 'machine'
    tier: str     # 'pro' | 'team' | 'starter'


@router.post("/checkout")
async def create_checkout_session(
    body: CheckoutRequest,
    user=Depends(get_current_user),
):
    """Create a Stripe Checkout session for the requested product + tier."""
    key = f"{body.product}:{body.tier}"
    price_id = _PRICE_IDS.get(key, "")
    if not price_id:
        raise HTTPException(status_code=400, detail=f"Unknown product/tier: {key}. Set env var STRIPE_{key.upper().replace(':', '_')}_PRICE")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured (STRIPE_SECRET_KEY missing)")

    base_url = f"https://{_PUBLIC_DOMAIN}"
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{base_url}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/?checkout=canceled",
            client_reference_id=str(user["id"]),
            customer_email=user.get("email"),
            metadata={"user_id": str(user["id"]), "product": body.product, "tier": body.tier},
            subscription_data={"metadata": {"user_id": str(user["id"]), "product": body.product, "tier": body.tier}},
        )
    except stripe.StripeError as exc:
        log.error("Stripe checkout error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"url": session.url, "session_id": session.id}


@router.get("/subscription")
async def get_subscription(product: str = "copilot", user=Depends(get_current_user)):
    """Return the current user's active subscription for a given product."""
    user_id = user["id"]
    tier_info = get_user_tier(user_id, product)
    period_start, period_end = _month_period()

    with _db() as conn:
        # Usage this month
        row = conn.execute(
            """SELECT COALESCE(SUM(count), 0) as total FROM usage_counters
               WHERE user_id = ? AND product = ? AND period_start = ?""",
            (user_id, product, period_start),
        ).fetchone()
        usage = row["total"] if row else 0

        # Subscription details
        sub = conn.execute(
            """SELECT tier, status, current_period_end FROM subscriptions
               WHERE user_id = ? AND product = ? ORDER BY id DESC LIMIT 1""",
            (user_id, product),
        ).fetchone()

    return {
        "product": product,
        "tier": tier_info["tier"],
        "status": sub["status"] if sub else "none",
        "current_period_end": sub["current_period_end"] if sub else None,
        "limits": tier_info,
        "usage_this_month": usage,
        "upgrade_url": f"https://www.elabsai.com/#pricing",
    }


@router.post("/portal")
async def create_portal_session(user=Depends(get_current_user)):
    """Create a Stripe Customer Portal session for managing/canceling subscriptions."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")

    user_id = user["id"]
    with _db() as conn:
        row = conn.execute(
            "SELECT stripe_customer_id FROM subscriptions WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row or not row["stripe_customer_id"]:
        raise HTTPException(status_code=404, detail="No active subscription found")

    try:
        portal = stripe.billing_portal.Session.create(
            customer=row["stripe_customer_id"],
            return_url=f"https://{_PUBLIC_DOMAIN}/",
        )
    except stripe.StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"url": portal.url}


@router.post("/webhooks")
async def stripe_webhook(request: Request):
    """
    Receive Stripe webhook events. Stripe signs each event — we verify the signature
    before trusting the payload (OWASP: validate all inputs at system boundary).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not _WEBHOOK_SECRET:
        log.warning("STRIPE_WEBHOOK_SECRET not set — skipping signature verification (dev mode)")
        try:
            event = stripe.Event.construct_from(
                stripe.util.convert_to_stripe_object(stripe.util.json.loads(payload)),
                stripe.api_key,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, _WEBHOOK_SECRET)
        except stripe.SignatureVerificationError as exc:
            log.warning("Webhook signature verification failed: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid signature") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    _handle_webhook_event(event)
    return {"status": "ok"}


def _handle_webhook_event(event):
    """Process Stripe events and update the subscriptions table accordingly."""
    etype = event["type"]
    data = event["data"]["object"]

    if etype == "checkout.session.completed":
        meta = data.get("metadata", {})
        user_id = int(meta.get("user_id", 0))
        product = meta.get("product", "copilot")
        tier = meta.get("tier", "pro")
        customer_id = data.get("customer")
        sub_id = data.get("subscription")
        if user_id:
            _upsert_subscription(user_id, product, tier, customer_id, sub_id, "active")

    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        sub_id = data["id"]
        customer_id = data.get("customer")
        status = data.get("status", "active")
        period_end = data.get("current_period_end")
        meta = data.get("metadata", {})
        user_id = int(meta.get("user_id", 0))
        product = meta.get("product", "copilot")
        tier = meta.get("tier", "pro")
        if user_id:
            _upsert_subscription(user_id, product, tier, customer_id, sub_id, status, period_end)

    elif etype == "customer.subscription.deleted":
        sub_id = data["id"]
        with _db() as conn:
            conn.execute(
                "UPDATE subscriptions SET status = 'canceled' WHERE stripe_subscription_id = ?",
                (sub_id,),
            )
        log.info("Subscription canceled: %s", sub_id)

    elif etype == "invoice.payment_failed":
        customer_id = data.get("customer")
        sub_id = data.get("subscription")
        if sub_id:
            with _db() as conn:
                conn.execute(
                    "UPDATE subscriptions SET status = 'past_due' WHERE stripe_subscription_id = ?",
                    (sub_id,),
                )
            log.warning("Payment failed for subscription %s (customer %s)", sub_id, customer_id)
    else:
        log.debug("Unhandled Stripe event: %s", etype)


def _upsert_subscription(
    user_id: int,
    product: str,
    tier: str,
    customer_id: Optional[str],
    sub_id: Optional[str],
    status: str,
    period_end: Optional[int] = None,
):
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM subscriptions WHERE user_id = ? AND product = ? LIMIT 1",
            (user_id, product),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE subscriptions
                   SET tier=?, stripe_customer_id=?, stripe_subscription_id=?,
                       status=?, current_period_end=?
                   WHERE id=?""",
                (tier, customer_id, sub_id, status, period_end, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO subscriptions
                   (user_id, product, tier, stripe_customer_id, stripe_subscription_id, status, current_period_end)
                   VALUES(?,?,?,?,?,?,?)""",
                (user_id, product, tier, customer_id, sub_id, status, period_end),
            )
    log.info("Subscription upserted: user=%s product=%s tier=%s status=%s", user_id, product, tier, status)
