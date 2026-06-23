from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
from datetime import date
import httpx
import os

app = FastAPI()

# Allow requests from anywhere (your app, browsers, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY", "")

# ─── Safety net: daily limit per visitor ──────────────────────────────────────
# This is NOT a substitute for real accounts and metered billing — it's a
# stopgap that caps the financial damage a single visitor can cause while
# the app has no login system. Resets at midnight UTC. Adjust DAILY_LIMIT
# as needed (20 is generous for genuine testing, low enough to block abuse).
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
            detail=f"Daily limit of {DAILY_LIMIT} analyses reached. This resets at midnight UTC. (This limit exists because the app has no accounts yet — it protects against runaway costs.)"
        )


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

    # Convert to simple OHLCV format, oldest first
    candles = []
    for v in reversed(values):
        candles.append({
            "t": int(__import__("datetime").datetime.fromisoformat(v["datetime"]).timestamp()),
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
    The app sends { system: "...", messages: [...] } — this endpoint
    attaches the real API key (which never reaches the browser) and
    forwards the request to Anthropic, then returns the response.
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
    }


@app.get("/")
async def root():
    return {"service": "Wick market data proxy", "endpoints": ["/candles", "/health"]}
