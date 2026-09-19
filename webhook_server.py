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
        if event["type"] == "invoice.payment_succeeded":
            data = event["data"]["object"]
            stripe_sub_id = data["subscription"]

            await db.execute("""
                UPDATE subscriptions
                SET status = 'active',
                    current_period_end = NOW() + INTERVAL '30 days'
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            print(f"[STRIPE] Subscription {stripe_sub_id} renewed.")

        elif event["type"] == "invoice.payment_failed":
            data = event["data"]["object"]
            stripe_sub_id = data["subscription"]

            await db.execute("""
                UPDATE subscriptions
                SET status = 'past_due'
                WHERE stripe_subscription_id = $1
            """, stripe_sub_id)

            print(f"[STRIPE] Subscription {stripe_sub_id} payment failed.")

    finally:
        await db.close()

    return {"status": "ok"}
