import os
import json
import stripe
import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI()


async def get_db():
    return await asyncpg.connect(DATABASE_URL)


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        return JSONResponse({"error": "Invalid signature"}, status_code=400)

    db = await get_db()

    try:
        event_type = event["type"]
        data = event["data"]["object"]

        # Common metadata
        metadata = data.get("metadata", {}) or {}
        guild_id = metadata.get("guild_id")
        admin_id = metadata.get("admin_id")
        vendor_id = metadata.get("vendor_id")

        # ============================================================
        # CHECKOUT SESSION COMPLETED (FREE + PAID)
        # ============================================================
        if event_type == "checkout.session.completed":
            stripe_sub_id = data.get("subscription")
            stripe_customer_id = data.get("customer")
            price_id = metadata.get("price_id") or data.get("price")

            # Fetch price to determine free vs paid
            price = stripe.Price.retrieve(price_id)
            amount = price["unit_amount"]  # cents

            # Insert subscription row
            await db.execute("""
                INSERT INTO subscriptions (
                    vendor_id,
                    guild_id,
                    stripe_subscription_id,
                    stripe_customer_id,
                    price_id,
                    status,
                    cancel_at_period_end,
                    current_period_start,
                    current_period_end,
                    created_at,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    'active',
                    FALSE,
                    NOW(),
                    NOW(),
                    NOW(),
                    NOW()
                )
                ON CONFLICT (stripe_subscription_id) DO NOTHING
            """, vendor_id, guild_id, stripe_sub_id, stripe_customer_id, price_id)

            # Fetch subscription_id
            sub_row = await db.fetchrow("""
                SELECT subscription_id
                FROM subscriptions
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            subscription_pk = sub_row["subscription_id"]

            # Insert guild_settings row if missing
            await db.execute("""
                INSERT INTO guild_settings (
                    guild_id,
                    admin_id,
                    subscription_id,
                    vendor_id,
                    license_active,
                    license_expires_at,
                    license_last_checked,
                    metadata
                )
                VALUES (
                    $1, $2, $3, $4,
                    TRUE,
                    NOW(),
                    NOW(),
                    $5
                )
                ON CONFLICT (guild_id) DO NOTHING
            """, guild_id, admin_id, subscription_pk, vendor_id, json.dumps(metadata))

            print(f"[STRIPE] Checkout completed → subscription created ({stripe_sub_id})")

        # ============================================================
        # SUBSCRIPTION UPDATED (CANCEL, PAUSE, PLAN CHANGE)
        # ============================================================
        elif event_type == "customer.subscription.updated":
            stripe_sub_id = data["id"]
            status = data["status"]
            cancel_at_period_end = data["cancel_at_period_end"]

            await db.execute("""
                UPDATE subscriptions
                SET status = $2,
                    cancel_at_period_end = $3,
                    updated_at = NOW()
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id, status, cancel_at_period_end)

            # Disable license if canceled or unpaid
            if status in ("canceled", "unpaid", "past_due"):
                await db.execute("""
                    UPDATE guild_settings
                    SET license_active = FALSE,
                        license_last_checked = NOW()
                    WHERE subscription_id = (
                        SELECT subscription_id
                        FROM subscriptions
                        WHERE stripe_subscription_id = $1
                    )
                """, stripe_sub_id)

            print(f"[STRIPE] Subscription updated ({stripe_sub_id}) → {status}")

        # ============================================================
        # PAYMENT SUCCEEDED (PAID TIERS ONLY)
        # ============================================================
        elif event_type == "invoice.payment_succeeded":
            stripe_sub_id = data.get("subscription")

            period_start = data["lines"]["data"][0]["period"]["start"]
            period_end = data["lines"]["data"][0]["period"]["end"]

            await db.execute("""
                UPDATE subscriptions
                SET status = 'active',
                    cancel_at_period_end = FALSE,
                    current_period_start = to_timestamp($2),
                    current_period_end = to_timestamp($3),
                    updated_at = NOW()
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id, period_start, period_end)

            await db.execute("""
                UPDATE guild_settings
                SET license_active = TRUE,
                    license_expires_at = to_timestamp($2),
                    license_last_checked = NOW()
                WHERE subscription_id = (
                    SELECT subscription_id
                    FROM subscriptions
                    WHERE stripe_subscription_id = $1
                )
            """, stripe_sub_id, period_end)

            print(f"[STRIPE] Payment succeeded → subscription renewed ({stripe_sub_id})")

        # ============================================================
        # PAYMENT FAILED (PAID TIERS ONLY)
        # ============================================================
        elif event_type == "invoice.payment_failed":
            stripe_sub_id = data.get("subscription")

            await db.execute("""
                UPDATE subscriptions
                SET status = 'past_due',
                    cancel_at_period_end = TRUE,
                    updated_at = NOW()
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            await db.execute("""
                UPDATE guild_settings
                SET license_active = FALSE,
                    license_last_checked = NOW()
                WHERE subscription_id = (
                    SELECT subscription_id
                    FROM subscriptions
                    WHERE stripe_subscription_id = $1
                )
            """, stripe_sub_id)

            print(f"[STRIPE] Payment failed ({stripe_sub_id})")

    finally:
        await db.close()

    return {"status": "ok"}
