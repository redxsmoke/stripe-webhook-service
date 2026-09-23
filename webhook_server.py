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

    event_type = event["type"]
    raw_data = event["data"]["object"]
    data = raw_data.to_dict()

    db = await get_db()

    try:
        # ============================================================
        # CHECKOUT SESSION COMPLETED
        # ============================================================
        if event_type == "checkout.session.completed":

            session = stripe.checkout.Session.retrieve(
                data["id"],
                expand=["line_items"]
            )
            session_data = session.to_dict()

            stripe_sub_id = session_data.get("subscription")
            stripe_customer_id = session_data.get("customer")

            if not stripe_sub_id:
                print("[STRIPE] No subscription ID — skipping")
                return {"status": "ok"}

            try:
                sub = stripe.Subscription.retrieve(stripe_sub_id)
            except stripe.error.InvalidRequestError:
                print(f"[STRIPE] Subscription {stripe_sub_id} does not exist — skipping")
                return {"status": "ok"}

            price_id = session_data["line_items"]["data"][0]["price"]["id"]

            metadata = session_data.get("metadata", {}) or {}
            vendor_id = int(metadata.get("vendor_id"))
            guild_id = int(metadata.get("guild_id"))
            admin_id = int(metadata.get("admin_id"))

            price = stripe.Price.retrieve(price_id)
            amount = price["unit_amount"]

            existing = await db.fetchrow("""
                SELECT subscription_id
                FROM subscriptions
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            if not existing:
                await db.execute("""
                    UPDATE subscriptions
                    SET status = 'canceled'
                    WHERE guild_id = $1
                      AND stripe_subscription_id != $2
                """, guild_id, stripe_sub_id)

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
                        NULL,
                        NULL,
                        NOW(),
                        NOW()
                    )
                """, vendor_id, guild_id, stripe_sub_id, stripe_customer_id, price_id)

            sub_row = await db.fetchrow("""
                SELECT subscription_id
                FROM subscriptions
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            subscription_pk = sub_row["subscription_id"]

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
                    NULL,
                    NOW(),
                    $5
                )
                ON CONFLICT (guild_id)
                DO UPDATE SET
                    subscription_id = EXCLUDED.subscription_id,
                    vendor_id = EXCLUDED.vendor_id,
                    license_active = TRUE,
                    license_expires_at = NULL,
                    license_last_checked = NOW(),
                    metadata = EXCLUDED.metadata
            """, guild_id, admin_id, subscription_pk, vendor_id, json.dumps(metadata))

            print(f"[STRIPE] Subscription created ({stripe_sub_id})")

        # ============================================================
        # SUBSCRIPTION UPDATED
        # ============================================================
        elif event_type == "customer.subscription.updated":
            stripe_sub_id = data["id"]
            status = data["status"]
            cancel_at_period_end = data["cancel_at_period_end"]

            current_period_start = data.get("current_period_start")
            current_period_end = data.get("current_period_end")

            await db.execute("""
                UPDATE subscriptions
                SET status = $2,
                    cancel_at_period_end = $3,
                    current_period_start = CASE WHEN $4 IS NOT NULL THEN to_timestamp($4) ELSE current_period_start END,
                    current_period_end = CASE WHEN $5 IS NOT NULL THEN to_timestamp($5) ELSE current_period_end END,
                    updated_at = NOW()
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id, status, cancel_at_period_end, current_period_start, current_period_end)

            await db.execute("""
                UPDATE guild_settings
                SET license_active = CASE WHEN $2 = 'active' THEN TRUE ELSE FALSE END,
                    license_last_checked = NOW(),
                    license_expires_at = CASE
                        WHEN $2 = 'canceled' THEN NOW()
                        ELSE license_expires_at
                    END,
                    subscription_id = (
                        SELECT subscription_id
                        FROM subscriptions
                        WHERE stripe_subscription_id = $1
                    )
                WHERE guild_id = (
                    SELECT guild_id
                    FROM subscriptions
                    WHERE stripe_subscription_id = $1
                )
            """, stripe_sub_id, status)

            print(f"[STRIPE] Subscription updated ({stripe_sub_id}) → {status}")

        # ============================================================
        # PAYMENT SUCCEEDED
        # ============================================================
        elif event_type == "invoice.payment_succeeded":
            stripe_sub_id = data.get("subscription")

            if not stripe_sub_id:
                return {"status": "ok"}

            # Get the subscription object and use its current_period_* fields
            sub = stripe.Subscription.retrieve(stripe_sub_id)
            current_period_start = sub["current_period_start"]
            current_period_end = sub["current_period_end"]

            await db.execute("""
                UPDATE subscriptions
                SET status = 'active',
                    cancel_at_period_end = FALSE,
                    current_period_start = to_timestamp($2),
                    current_period_end = to_timestamp($3),
                    updated_at = NOW()
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id, current_period_start, current_period_end)

            await db.execute("""
                UPDATE guild_settings
                SET license_active = TRUE,
                    license_expires_at = to_timestamp($2),
                    license_last_checked = NOW(),
                    subscription_id = (
                        SELECT subscription_id
                        FROM subscriptions
                        WHERE stripe_subscription_id = $1
                    )
                WHERE guild_id = (
                    SELECT guild_id
                    FROM subscriptions
                    WHERE stripe_subscription_id = $1
                )
            """, stripe_sub_id, current_period_end)

            print(f"[STRIPE] Payment succeeded ({stripe_sub_id})")

        # ============================================================
        # PAYMENT FAILED
        # ============================================================
        elif event_type == "invoice.payment_failed":
            stripe_sub_id = data.get("subscription")

            if not stripe_sub_id:
                return {"status": "ok"}

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
                    subscription_id = (
                        SELECT subscription_id
                        FROM subscriptions
                        WHERE stripe_subscription_id = $1
                    )
                WHERE guild_id = (
                    SELECT guild_id
                    FROM subscriptions
                    WHERE stripe_subscription_id = $1
                )
            """, stripe_sub_id)

            print(f"[STRIPE] Payment failed ({stripe_sub_id})")
    finally:
        await db.close()

    return {"status": "ok"}
