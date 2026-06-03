from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os

app = FastAPI()

# Allow requests from anywhere (your app, browsers, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")


@app.get("/candles")
async def get_candles(
    symbol:     str = Query(..., description="e.g. AAPL or BARC:LSE or EUR/USD"),
    interval:   str = Query("1day", description="e.g. 5min, 1h, 1day, 1week"),
    outputsize: int = Query(60,    description="Number of candles to return"),
):
    """Fetch OHLCV candle data from Twelve Data and return it to the app."""
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


@app.get("/health")
async def health():
    return {"status": "ok", "key_set": bool(TWELVE_DATA_KEY)}


@app.get("/")
async def root():
    return {"service": "Wick market data proxy", "endpoints": ["/candles", "/health"]}
