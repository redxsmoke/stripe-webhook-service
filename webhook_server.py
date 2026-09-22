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

        # Stripe IDs
        stripe_sub_id = data.get("subscription")
        stripe_customer_id = data.get("customer")

        # Metadata passed from Checkout Session
        metadata = data.get("metadata", {}) or {}
        guild_id = metadata.get("guild_id")
        admin_id = metadata.get("admin_id")
        vendor_id = metadata.get("vendor_id")

        # Stripe billing period (invoice lines)
        period_start = None
        period_end = None

        if "lines" in data and "data" in data["lines"] and data["lines"]["data"]:
            line = data["lines"]["data"][0]
            period_start = line["period"]["start"]
            period_end = line["period"]["end"]

        # ============================================================
        # ENSURE SUBSCRIPTION ROW EXISTS
        # ============================================================
        existing_sub = await db.fetchrow("""
            SELECT subscription_id
            FROM subscriptions
            WHERE stripe_subscription_id = $1
        """, stripe_sub_id)

        if not existing_sub:
            await db.execute("""
                INSERT INTO subscriptions (
                    vendor_id,
                    guild_id,
                    stripe_subscription_id,
                    stripe_customer_id,
                    status,
                    cancel_at_period_end,
                    current_period_start,
                    current_period_end,
                    created_at,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4,
                    'active',
                    FALSE,
                    to_timestamp($5),
                    to_timestamp($6),
                    NOW(),
                    NOW()
                )
            """, vendor_id, guild_id, stripe_sub_id, stripe_customer_id, period_start, period_end)

            existing_sub = await db.fetchrow("""
                SELECT subscription_id
                FROM subscriptions
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

        subscription_id = existing_sub["subscription_id"]

        # ============================================================
        # ENSURE GUILD SETTINGS ROW EXISTS
        # ============================================================
        existing_guild = await db.fetchrow("""
            SELECT guild_id
            FROM guild_settings
            WHERE guild_id = $1
        """, guild_id)

        if not existing_guild:
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
                    to_timestamp($5),
                    NOW(),
                    $6
                )
            """, guild_id, admin_id, subscription_id, vendor_id, period_end, json.dumps(metadata))

        # ============================================================
        # PAYMENT SUCCEEDED
        # ============================================================
        if event_type == "invoice.payment_succeeded":
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
                    license_last_checked = NOW(),
                    metadata = $3
                WHERE guild_id = $1
            """, guild_id, period_end, json.dumps(metadata))

            print(f"[STRIPE] Subscription {stripe_sub_id} renewed.")

        # ============================================================
        # PAYMENT FAILED
        # ============================================================
        elif event_type == "invoice.payment_failed":
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
                    license_last_checked = NOW(),
                    metadata = $2
                WHERE guild_id = $1
            """, guild_id, json.dumps(metadata))

            print(f"[STRIPE] Subscription {stripe_sub_id} payment failed.")

    finally:
        await db.close()

    return {"status": "ok"}
