import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    raw = await request.body()
    print("======================================")
    print("RAW PAYLOAD RECEIVED FROM STRIPE:")
    print(raw)
    print("======================================")

    # Try to decode JSON (if any)
    try:
        import json
        decoded = json.loads(raw)
        print("DECODED JSON:")
        print(decoded)
    except Exception as e:
        print("JSON DECODE ERROR:", e)

    return JSONResponse({"status": "ok"})
