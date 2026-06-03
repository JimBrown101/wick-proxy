# Wick Market Data Proxy

A tiny FastAPI server that fetches real OHLCV candle data from Twelve Data
and returns it to the Wick app — bypassing browser CORS restrictions.

## Deploy to Railway (5 minutes)

1. Go to railway.app and sign up (free $5 trial, no card needed)
2. Click "New Project" → "Deploy from GitHub repo"
   - OR click "New Project" → "Empty project" → drag this folder
3. Add environment variable:
   - Key:   TWELVE_DATA_KEY
   - Value: your Twelve Data API key (from twelvedata.com dashboard)
4. Railway gives you a public URL like: https://wick-proxy-production.up.railway.app
5. Copy that URL and paste it into the Wick app where it says "Proxy URL"

## Test it works

Open your browser and visit:
  https://YOUR-RAILWAY-URL.up.railway.app/candles?symbol=AAPL&interval=1day&outputsize=30

You should see real AAPL price data as JSON.

## Symbols

- US stocks:  AAPL, TSLA, NVDA, SPY
- LSE stocks: BARC:LSE, BP:LSE, LLOY:LSE
- Forex:      EUR/USD, GBP/USD
- Crypto:     BTC/USD, ETH/USD

## Cost

Railway Hobby plan: $5/month
Twelve Data free tier: 800 API credits/day (plenty for testing)
Twelve Data Grow:     $29/month (for production with real users)
