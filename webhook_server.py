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
        # CHECKOUT SESSION COMPLETED (FREE + PAID)
        # ============================================================
        if event_type == "checkout.session.completed":
            # Retrieve full session with line_items expanded
            session = stripe.checkout.Session.retrieve(
                data["id"],
                expand=["line_items"]
            )
            session_data = session.to_dict()

            stripe_sub_id = session_data.get("subscription")
            stripe_customer_id = session_data.get("customer")

            # If no subscription ID, skip insert
            if not stripe_sub_id:
                print("[STRIPE] checkout.session.completed without subscription_id — skipping")
                return {"status": "ok"}

            # Verify subscription actually exists in Stripe
            try:
                sub = stripe.Subscription.retrieve(stripe_sub_id)
            except stripe.error.InvalidRequestError:
                print(f"[STRIPE] Subscription {stripe_sub_id} does not exist — skipping insert")
                return {"status": "ok"}

            # Extract price_id correctly
            price_id = session_data["line_items"]["data"][0]["price"]["id"]

            # Metadata
            metadata = session_data.get("metadata", {}) or {}

            vendor_id = metadata.get("vendor_id")
            guild_id = metadata.get("guild_id")
            admin_id = metadata.get("admin_id")

            # Convert metadata → integers
            if vendor_id is not None:
                vendor_id = int(vendor_id)

            if guild_id is not None:
                guild_id = int(guild_id)

            if admin_id is not None:
                admin_id = int(admin_id)

            # Fetch price to determine free vs paid
            price = stripe.Price.retrieve(price_id)
            amount = price["unit_amount"]  # cents

            # Prevent duplicate subscription rows
            existing = await db.fetchrow("""
                SELECT subscription_id
                FROM subscriptions
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            if existing:
                print(f"[STRIPE] Subscription {stripe_sub_id} already exists — skipping insert")
            else:
                # Mark any previous subscriptions for this guild as canceled
                await db.execute("""
                    UPDATE subscriptions
                    SET status = 'canceled'
                    WHERE guild_id = $1
                      AND stripe_subscription_id != $2
                """, guild_id, stripe_sub_id)

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

            if not sub_row:
                print(f"[STRIPE] No DB row found for subscription {stripe_sub_id} after insert — skipping guild_settings")
                return {"status": "ok"}

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
            data = raw_data.to_dict()

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
        # PAYMENT SUCCEEDED (PAID ONLY)
        # ============================================================
        elif event_type == "invoice.payment_succeeded":
            data = raw_data.to_dict()

            stripe_sub_id = data.get("subscription")

            if not stripe_sub_id:
                print("[STRIPE] invoice.payment_succeeded without subscription_id — skipping")
                return {"status": "ok"}

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
        # PAYMENT FAILED (PAID ONLY)
        # ============================================================
        elif event_type == "invoice.payment_failed":
            data = raw_data.to_dict()

            stripe_sub_id = data.get("subscription")

            if not stripe_sub_id:
                print("[STRIPE] invoice.payment_failed without subscription_id — skipping")
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
