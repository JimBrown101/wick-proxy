from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
from datetime import date, datetime, timezone
import httpx
import os
import hmac
import hashlib

app = FastAPI()

# Allow requests from anywhere (your app, browsers, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

TWELVE_DATA_KEY   = os.environ.get("TWELVE_DATA_KEY", "")
ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_KEY", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")       # e.g. https://xxxx.supabase.co
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")       # the "secret key" from Supabase API settings
LS_WEBHOOK_SECRET = os.environ.get("LS_WEBHOOK_SECRET", "")  # signing secret from Lemon Squeezy webhook settings

# Maps Lemon Squeezy variant IDs to plan details. Variant IDs are stable
# numbers that never change, unlike product name text (which can have
# inconsistent spacing and broke our matching before).
PLAN_MAP = {
    1831416: {"tier": "starter", "limit": 100},  # Wick Starter
    1831445: {"tier": "pro",     "limit": 300},  # Wick Pro
}

# ─── Safety net: daily limit per visitor ──────────────────────────────────────
# Stopgap cost protection — independent of the subscriber database below.
DAILY_LIMIT = 20
_usage_log = defaultdict(lambda: {"date": None, "count": 0})

def enforce_daily_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    today = date.today().isoformat()
    entry = _usage_log[ip]
    if entry["date"] != today:
        entry["date"] = today
        entry["count"] = 0
    entry["count"] += 1
    if entry["count"] > DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {DAILY_LIMIT} analyses reached. Resets at midnight UTC."
        )


# ─── Supabase helpers ──────────────────────────────────────────────────────────
def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

async def get_subscriber_by_email(email: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/subscribers"
    params = {"email": f"eq.{email}", "select": "*"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, headers=_supabase_headers(), params=params)
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None

async def upsert_subscriber(email: str, tier: str, limit: int):
    existing = await get_subscriber_by_email(email)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "email": email,
        "tier": tier,
        "analyses_used": 0,
        "analyses_limit": limit,
        "period_start": now,
        "unlocked": True,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        if existing:
            url = f"{SUPABASE_URL}/rest/v1/subscribers?id=eq.{existing['id']}"
            await client.patch(url, headers=_supabase_headers(), json=payload)
        else:
            url = f"{SUPABASE_URL}/rest/v1/subscribers"
            await client.post(url, headers=_supabase_headers(), json=payload)

async def deactivate_subscriber(email: str):
    existing = await get_subscriber_by_email(email)
    if not existing:
        return
    url = f"{SUPABASE_URL}/rest/v1/subscribers?id=eq.{existing['id']}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.patch(url, headers=_supabase_headers(), json={"unlocked": False})


# ─── Lemon Squeezy webhook ─────────────────────────────────────────────────────
@app.post("/webhook/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request):
    """
    Lemon Squeezy calls this automatically whenever someone subscribes,
    cancels, or their subscription changes. We verify the signature so
    only genuine Lemon Squeezy events can update the database, then
    upsert the subscriber's record in Supabase.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Signature", "")

    if LS_WEBHOOK_SECRET:
        expected = hmac.new(LS_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    event_name = payload.get("meta", {}).get("event_name", "")
    data = payload.get("data", {}).get("attributes", {})
    email = data.get("user_email") or data.get("customer_email")
    variant_id = data.get("variant_id")

    if not email:
        return {"status": "ignored", "reason": "no email in payload"}

    if event_name in ("subscription_created", "subscription_updated", "subscription_resumed", "order_created"):
        plan = PLAN_MAP.get(variant_id)
        if plan:
            await upsert_subscriber(email, plan["tier"], plan["limit"])
            return {"status": "ok", "action": "upserted", "email": email, "tier": plan["tier"]}
        return {"status": "ignored", "reason": f"unrecognised variant_id: {variant_id}"}

    if event_name in ("subscription_cancelled", "subscription_expired"):
        await deactivate_subscriber(email)
        return {"status": "ok", "action": "deactivated", "email": email}

    return {"status": "ignored", "reason": f"unhandled event: {event_name}"}


@app.get("/check-subscriber")
async def check_subscriber(email: str = Query(...)):
    """The app will call this to check whether an email is an active subscriber."""
    sub = await get_subscriber_by_email(email)
    if not sub:
        return {"found": False}
    return {
        "found": True,
        "tier": sub.get("tier"),
        "unlocked": sub.get("unlocked"),
        "analyses_used": sub.get("analyses_used"),
        "analyses_limit": sub.get("analyses_limit"),
    }


@app.get("/candles")
async def get_candles(
    request: Request,
    symbol:     str = Query(..., description="e.g. AAPL or BARC:LSE or EUR/USD"),
    interval:   str = Query("1day", description="e.g. 5min, 1h, 1day, 1week"),
    outputsize: int = Query(60,    description="Number of candles to return"),
):
    """Fetch OHLCV candle data from Twelve Data and return it to the app."""
    enforce_daily_limit(request)

    if not TWELVE_DATA_KEY:
        raise HTTPException(status_code=500, detail="TWELVE_DATA_KEY not set in environment")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVE_DATA_KEY,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params)

    data = response.json()

    if data.get("status") == "error":
        raise HTTPException(status_code=400, detail=data.get("message", "Twelve Data error"))

    values = data.get("values", [])
    if not values:
        raise HTTPException(status_code=404, detail=f"No data found for {symbol}")

    candles = []
    for v in reversed(values):
        candles.append({
            "t": int(datetime.fromisoformat(v["datetime"]).timestamp()),
            "o": float(v["open"]),
            "h": float(v["high"]),
            "l": float(v["low"]),
            "c": float(v["close"]),
            "v": int(float(v.get("volume", 0))),
        })

    return {
        "symbol":   symbol,
        "interval": interval,
        "candles":  candles,
        "count":    len(candles),
        "source":   "twelvedata",
    }


@app.post("/analyse")
async def analyse(request: Request, payload: dict = Body(...)):
    """
    Securely calls Anthropic's API on behalf of the app.
    """
    enforce_daily_limit(request)

    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_KEY not set in environment")

    system_prompt = payload.get("system", "")
    messages      = payload.get("messages", [])
    max_tokens    = payload.get("max_tokens", 3000)

    if not messages:
        raise HTTPException(status_code=400, detail="messages field is required")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=body)

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    return response.json()


@app.get("/usage")
async def usage():
    """See today's request counts per visitor — useful for spotting abuse."""
    today = date.today().isoformat()
    today_usage = {ip: v["count"] for ip, v in _usage_log.items() if v["date"] == today}
    return {"date": today, "daily_limit": DAILY_LIMIT, "usage_by_ip": today_usage}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "twelve_data_key_set": bool(TWELVE_DATA_KEY),
        "anthropic_key_set": bool(ANTHROPIC_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "ls_webhook_secret_set": bool(LS_WEBHOOK_SECRET),
    }


@app.get("/")
async def root():
    return {"service": "Wick market data proxy", "endpoints": ["/candles", "/analyse", "/webhook/lemonsqueezy", "/check-subscriber", "/health"]}
