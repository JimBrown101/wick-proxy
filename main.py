from fastapi import FastAPI, HTTPException, Query, Body, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
import httpx
import os
import hmac
import hashlib

app = FastAPI()

# Allow requests from anywhere (your app, browsers, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

TWELVE_DATA_KEY   = os.environ.get("TWELVE_DATA_KEY", "")
ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_KEY", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")       # e.g. https://xxxx.supabase.co
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")       # the "secret key" from Supabase API settings
WHOP_API_KEY = os.environ.get("WHOP_API_KEY", "")  # signing secret from Whop webhook settings

# Maps Whop product identifiers to plan details.
# Includes product IDs (most reliable), slugs and title fallbacks.
PLAN_MAP = {
    "prod_QziWZaoMPPzhr": {"tier": "starter", "limit": 100},  # Wick Starter
    "prod_U4MBsuhmSRXQp": {"tier": "pro",     "limit": 300},  # Wick Pro
    "wick-starter":        {"tier": "starter", "limit": 100},  # slug fallback
    "wick-pro":            {"tier": "pro",     "limit": 300},  # slug fallback
    "wick starter":        {"tier": "starter", "limit": 100},  # title fallback
    "wick pro":            {"tier": "pro",     "limit": 300},  # title fallback
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
            url = f"{SUPABASE_URL}/rest/v1/subscribers?email=eq.{email}"
            r = await client.patch(url, headers=_supabase_headers(), json=payload)
        else:
            url = f"{SUPABASE_URL}/rest/v1/subscribers"
            r = await client.post(url, headers={**_supabase_headers(), "Prefer": "return=representation"}, json=payload)

    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Supabase write failed ({r.status_code}): {r.text}")
    return r

async def deactivate_subscriber(email: str):
    url = f"{SUPABASE_URL}/rest/v1/subscribers?email=eq.{email}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.patch(url, headers=_supabase_headers(), json={"unlocked": False})


async def patch_subscriber(email: str, fields: dict):
    url = f"{SUPABASE_URL}/rest/v1/subscribers?email=eq.{email}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.patch(url, headers=_supabase_headers(), json=fields)
    return r

async def check_and_increment_usage(email: str):
    """
    Called before every paid analysis. Enforces the subscriber's real
    monthly limit (100 for Starter, 300 for Pro) and resets usage
    automatically once 30 days have passed since their period started —
    no manual housekeeping or scheduled job needed.
    """
    sub = await get_subscriber_by_email(email)
    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found for that email.")
    if not sub.get("unlocked"):
        raise HTTPException(status_code=403, detail="This subscription is no longer active.")

    used  = sub.get("analyses_used", 0)
    limit = sub.get("analyses_limit", 100)
    tier  = sub.get("tier", "starter")

    # Automatic monthly reset — lazy-checked on use, so no cron job is needed.
    period_start_raw = sub.get("period_start")
    if period_start_raw:
        try:
            period_start = datetime.fromisoformat(period_start_raw.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - period_start > timedelta(days=30):
                used = 0
                await patch_subscriber(email, {
                    "analyses_used": 0,
                    "period_start": datetime.now(timezone.utc).isoformat(),
                })
        except (ValueError, TypeError):
            pass  # if the date is malformed, skip the reset rather than block usage

    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"You've used all {limit} analyses included in your {tier.capitalize()} plan this month. "
                   f"It resets automatically next billing period, or upgrade for a higher limit."
        )

    new_used = used + 1
    await patch_subscriber(email, {"analyses_used": new_used})
    return {"analyses_used": new_used, "analyses_limit": limit, "tier": tier}


# ─── Whop webhook ──────────────────────────────────────────────────────────────
@app.post("/webhook/whop")
async def whop_webhook(request: Request):
    """
    Whop calls this when someone subscribes, cancels or their membership
    changes. Payload structure per Whop API v1 docs:
    {
      "action": "membership.went_valid",
      "data": {
        "email": "user@example.com",          # top-level email
        "user": { "email": "...", "id": "..." },
        "product": { "id": "prod_xxx", "title": "Wick Starter" },
        "plan": { "id": "plan_xxx" }
      }
    }
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Whop-Signature", "")

    if WHOP_API_KEY and signature:
        expected = hmac.new(WHOP_API_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    action  = payload.get("action", "") or payload.get("type", "")
    data    = payload.get("data", {})

    # Email — try multiple locations Whop may use
    email = (
        data.get("email") or
        (data.get("user") or {}).get("email") or
        data.get("user_email")
    )

    # Product identification — try product title and ID
    product_obj   = data.get("product") or {}
    product_title = (product_obj.get("title") or "").lower()
    product_id    = product_obj.get("id") or ""
    plan_id       = (data.get("plan") or {}).get("id") or ""

    if not email:
        return {"status": "ignored", "reason": "no email in payload", "action": action}

    if action in ("membership.went_valid", "membership.created", "membership_activated",
                  "membership.activated", "payment.succeeded", "payment_succeeded"):

        # Match by product ID first (most reliable), then title
        plan = PLAN_MAP.get(product_id) or PLAN_MAP.get(plan_id)
        if not plan:
            # Fall back to title matching
            if "starter" in product_title:
                plan = PLAN_MAP["wick-starter"]
            elif "pro" in product_title:
                plan = PLAN_MAP["wick-pro"]

        if plan:
            try:
                await upsert_subscriber(email, plan["tier"], plan["limit"])
                return {"status": "ok", "action": "upserted", "email": email, "tier": plan["tier"]}
            except HTTPException as e:
                return {"status": "error", "detail": e.detail}
        return {"status": "ignored", "reason": f"unrecognised product: {product_title or product_id}", "action": action}

    if action in ("membership.went_invalid", "membership.cancelled", "membership.expired",
                  "membership_deactivated", "membership.deactivated", "membership_cancel_at_period_end_chan"):
        await deactivate_subscriber(email)
        return {"status": "ok", "action": "deactivated", "email": email}

    return {"status": "ignored", "reason": f"unhandled action: {action}"}


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
    If the request includes a subscriber email, it's checked and metered
    against their real plan limit (100 Starter / 300 Pro) with automatic
    monthly reset. If no email is present, the free-tier daily IP limit
    is the only protection — unchanged from before.
    """
    email = payload.get("email")
    usage_info = None

    if email:
        try:
            usage_info = await check_and_increment_usage(email)
        except HTTPException:
            raise  # re-raise limit/auth errors as-is
        except Exception as e:
            # Supabase connection issue — log it but don't block the analysis
            print(f"Usage check failed for {email}: {e}")
    else:
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

    result = response.json()

    # Sonnet 5 uses adaptive thinking which adds 'thinking' content blocks.
    # Strip these out server-side so the client only receives clean text blocks
    # and our JSON parsing never fails on thinking output.
    if "content" in result:
        result["content"] = [b for b in result["content"] if b.get("type") != "thinking"]

    if usage_info:
        result["_usage"] = usage_info
    return result


@app.get("/usage")
async def usage():
    """See today's request counts per visitor — useful for spotting abuse."""
    today = date.today().isoformat()
    today_usage = {ip: v["count"] for ip, v in _usage_log.items() if v["date"] == today}
    return {"date": today, "daily_limit": DAILY_LIMIT, "usage_by_ip": today_usage}


@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "twelve_data_key_set": bool(TWELVE_DATA_KEY),
        "anthropic_key_set": bool(ANTHROPIC_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "whop_webhook_secret_set": bool(WHOP_API_KEY),
    }


@app.get("/")
async def root():
    return {"service": "Wick market data proxy", "endpoints": ["/candles", "/analyse", "/webhook/whop", "/check-subscriber", "/health"]}
