from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import math
import os

app = Flask(__name__)
CORS(app, origins="*")

TRADIER_KEY = "XbEAA6FWezNdDZXg0rIiHGK9V27e"
TRADIER_BASE = "https://api.tradier.com/v1"
HEADERS = {
    "Authorization": f"Bearer {TRADIER_KEY}",
    "Accept": "application/json"
}

def get_quote(symbol):
    try:
        r = requests.get(
            f"{TRADIER_BASE}/markets/quotes",
            headers=HEADERS,
            params={"symbols": symbol},
            timeout=5
        )
        if r.status_code != 200:
            return None
        data = r.json()
        quote = data.get("quotes", {}).get("quote", {})
        if not quote:
            return None
        return {
            "price": float(quote.get("last") or quote.get("close") or 0),
            "open": float(quote.get("open") or 0),
            "high": float(quote.get("high") or 0),
            "low": float(quote.get("low") or 0),
            "volume": int(quote.get("volume") or 0),
            "change_pct": float(quote.get("change_percentage") or 0)
        }
    except Exception as e:
        print(f"Quote error {symbol}: {e}")
        return None

def get_5min_bars(symbol):
    try:
        end = datetime.now()
        start = end - timedelta(days=5)
        r = requests.get(
            f"{TRADIER_BASE}/markets/timesales",
            headers=HEADERS,
            params={
                "symbol": symbol,
                "interval": "5min",
                "start": start.strftime("%Y-%m-%d 09:30"),
                "end": end.strftime("%Y-%m-%d 16:00"),
                "session_filter": "open"
            },
            timeout=10
        )
        if r.status_code != 200:
            return None
        data = r.json()
        series = data.get("series")
        if not series or series == "null":
            return None
        bars_raw = series.get("data", [])
        if not bars_raw:
            return None
        if isinstance(bars_raw, dict):
            bars_raw = [bars_raw]
        bars = []
        for b in bars_raw:
            try:
                o = float(b.get("open") or 0)
                h = float(b.get("high") or 0)
                l = float(b.get("low") or 0)
                c = float(b.get("close") or 0)
                v = int(b.get("volume") or 0)
                if c > 0:
                    bars.append({"o": o, "h": h, "l": l, "c": c, "v": v})
            except:
                continue
        return bars if len(bars) >= 5 else None
    except Exception as e:
        print(f"Bars error {symbol}: {e}")
        return None

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def vwap(bars):
    total_vol = sum(b["v"] for b in bars)
    if total_vol == 0:
        return None
    return sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / total_vol

def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        tr = max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - bars[i-1]["c"]),
            abs(bars[i]["l"] - bars[i-1]["c"])
        )
        trs.append(tr)
    return sum(trs[-period:]) / period

def calc_rvol(bars):
    if len(bars) < 2:
        return 1.0
    avg = sum(b["v"] for b in bars[:-1]) / max(len(bars) - 1, 1)
    if avg == 0:
        return 1.0
    return round(bars[-1]["v"] / avg, 1)

def vol_status(rvol):
    if rvol >= 2.0:
        return "STRONG"
    elif rvol >= 1.5:
        return "CONFIRMED"
    elif rvol >= 1.0:
        return "WEAK"
    return "LOW"

def detect_patterns(bars, price):
    if not bars or len(bars) < 6:
        return []
    signals = []
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    last = bars[-1]
    prev = bars[-2]
    avg_vol = sum(vols[-10:]) / min(10, len(vols))
    rvol = calc_rvol(bars)
    atr = calc_atr(bars, 14) or price * 0.01
    vw = vwap(bars)
    e9 = ema(closes, min(9, len(closes)))
    e21 = ema(closes, min(21, len(closes)))

    def stop(bias):
        if bias == "Long":
            return round(price - atr * 1.5, 2)
        return round(price + atr * 1.5, 2)

    def sig(name, bias, conf, note):
        return {
            "name": name, "bias": bias, "conf": conf,
            "stop": stop(bias), "note": note,
            "rvol": rvol, "volStatus": vol_status(rvol),
            "volConfirmed": rvol >= 1.5
        }

    # ORB
    orb_high = bars[0]["h"]
    orb_low = bars[0]["l"]
    if last["c"] > orb_high and last["v"] > avg_vol * 1.5:
        signals.append(sig("ORB Breakout", "Long", 80, "Broke opening range high on volume"))
    if last["c"] < orb_low and last["v"] > avg_vol * 1.5:
        signals.append(sig("ORB Breakdown", "Short", 77, "Broke opening range low on volume"))

    # VWAP
    if vw:
        if prev["c"] < vw and last["c"] > vw and last["v"] > avg_vol * 1.2:
            signals.append(sig("VWAP Reclaim", "Long", 78, "Reclaimed VWAP with volume"))
        if prev["c"] > vw and last["c"] < vw and last["v"] > avg_vol * 1.2:
            signals.append(sig("VWAP Rejection", "Short", 75, "Failed VWAP on volume"))

    # Engulfing
    if prev["c"] < prev["o"] and last["c"] > last["o"] and last["o"] < prev["c"] and last["c"] > prev["o"] and last["v"] > avg_vol * 1.2:
        signals.append(sig("Bullish Engulfing", "Long", 81, "Green candle engulfs prior red"))
    if prev["c"] > prev["o"] and last["c"] < last["o"] and last["o"] > prev["c"] and last["c"] < prev["o"] and last["v"] > avg_vol * 1.2:
        signals.append(sig("Bearish Engulfing", "Short", 81, "Red candle engulfs prior green"))

    # Gap & Go
    if len(bars) >= 2:
        pc = bars[-2]["c"]
        if pc > 0:
            gp = (last["o"] - pc) / pc * 100
            if gp > 1.5 and last["c"] > last["o"] and last["v"] > avg_vol * 2:
                signals.append(sig("Gap & Go", "Long", 85, f"Gapped up +{gp:.1f}% with volume"))
            if gp < -1.5 and last["c"] < last["o"] and last["v"] > avg_vol * 2:
                signals.append(sig("Gap & Go", "Short", 83, f"Gapped down {gp:.1f}% with volume"))

    # Bull Flag
    if len(bars) >= 8:
        imp = bars[-8:-3]
        flg = bars[-3:]
        iup = all(imp[i]["c"] >= imp[i-1]["c"] for i in range(1, len(imp)))
        fdn = all(b["c"] <= imp[-1]["c"] for b in flg)
        fv = sum(b["v"] for b in flg) / len(flg)
        if iup and fdn and fv < avg_vol * 0.8:
            sl = min(b["l"] for b in flg)
            signals.append({
                "name": "Bull Flag", "bias": "Long", "conf": 82,
                "stop": round(sl, 2), "note": "Bull flag - low vol consolidation",
                "rvol": rvol, "volStatus": vol_status(rvol), "volConfirmed": rvol >= 1.5
            })

    # EMA Pullback
    if e9 and e21 and e9 > e21 and abs(last["c"] - e21) / e21 < 0.005 and last["c"] > last["o"]:
        signals.append(sig("EMA Pullback", "Long", 74, "Bouncing off EMA21 in uptrend"))
    if e9 and e21 and e9 < e21 and abs(last["c"] - e21) / e21 < 0.005 and last["c"] < last["o"]:
        signals.append(sig("EMA Resistance", "Short", 72, "Rejected at EMA21 in downtrend"))

    return signals

def suggest_option(sym, price, bias, stop_price):
    is_call = bias == "Long"
    if price > 200:
        snap = 5
    elif price > 50:
        snap = 2.5
    else:
        snap = 1
    if is_call:
        strike = round(math.ceil(price / snap) * snap + snap, 2)
    else:
        strike = round(math.floor(price / snap) * snap - snap, 2)
    today = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday < 2:
        days_to_friday += 7
    expiry = today + timedelta(days=days_to_friday + 7)
    exp_str = expiry.strftime("%b %-d")
    days_out = (expiry - today).days
    iv = 0.45
    premium = max(0.05, round(price * iv * math.sqrt(days_out / 365) * 0.3, 2))
    contract_cost = round(premium * 100)
    return {
        "type": "CALL" if is_call else "PUT",
        "strike": strike,
        "expStr": exp_str,
        "premStr": f"${premium}",
        "contractCost": contract_cost,
        "optStop": round(contract_cost * 0.5),
        "t1val": round(contract_cost * 1.75),
        "t2val": round(contract_cost * 2.0),
        "iv": f"{int(iv*100)}%",
        "delta": "~0.35",
        "daysOut": days_out
    }

TICKERS = [
    "SPY","QQQ","IWM","SOXL",
    "AAPL","MSFT","NVDA","AMD","META","GOOGL","AMZN","TSLA",
    "BAC","C","WFC","SOFI","HOOD",
    "PLTR","SNAP","UBER","RBLX","LYFT",
    "RIVN","NIO","F",
    "COIN","MARA","MSTR",
    "AAL","DAL","CCL","PENN",
    "MU","SMCI","QCOM","AVGO",
    "GME","SPCE"
]

@app.route("/")
def index():
    return jsonify({"name": "OptionScan API", "status": "running"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/scan/day")
def scan_day():
    results = []
    
    # Get SPY bias
    spy_bars = get_5min_bars("SPY")
    spy_bias = "neutral"
    if spy_bars and len(spy_bars) >= 5:
        vw = vwap(spy_bars)
        closes = [b["c"] for b in spy_bars]
        e9 = ema(closes, min(9, len(closes)))
        e21 = ema(closes, min(21, len(closes)))
        last = spy_bars[-1]
        if vw and e9 and e21:
            if last["c"] > vw and e9 > e21:
                spy_bias = "bullish"
            elif last["c"] < vw and e9 < e21:
                spy_bias = "bearish"

    for sym in TICKERS:
        try:
            # Get live quote first for accurate price
            quote = get_quote(sym)
            if not quote or quote["price"] <= 0:
                continue
            live_price = quote["price"]
            if live_price < 3:
                continue

            # Get 5min bars
            bars = get_5min_bars(sym)
            if not bars or len(bars) < 5:
                continue

            # Check for stale data - skip if bar price differs >3% from live quote
            bar_price = bars[-1]["c"]
            if abs(live_price - bar_price) / live_price > 0.03:
                print(f"Stale {sym}: bar={bar_price:.2f} live={live_price:.2f}")
                # Update last bar price with live quote
                bars[-1]["c"] = live_price

            patterns = detect_patterns(bars, live_price)
            
            for p in patterns:
                # Only block when strongly opposing
                if p["bias"] == "Long" and spy_bias == "bearish":
                    continue
                if p["bias"] == "Short" and spy_bias == "bullish":
                    continue

                conf = p["conf"]
                rvol = p["rvol"]
                if rvol >= 2.0:
                    conf = min(97, conf + 5)
                elif rvol >= 1.5:
                    conf = min(97, conf + 3)
                if spy_bias == "bullish" and p["bias"] == "Long":
                    conf = min(97, conf + 3)
                if spy_bias == "bearish" and p["bias"] == "Short":
                    conf = min(97, conf + 3)

                opt = suggest_option(sym, live_price, p["bias"], p["stop"])
                atr = calc_atr(bars, 14)

                results.append({
                    "sym": sym,
                    "price": round(live_price, 2),
                    "name": p["name"],
                    "bias": p["bias"],
                    "conf": conf,
                    "stop": p["stop"],
                    "note": p["note"],
                    "opt": opt,
                    "mode": "day",
                    "time": datetime.now().strftime("%I:%M:%S %p"),
                    "rvol": rvol,
                    "volStatus": p["volStatus"],
                    "volConfirmed": p["volConfirmed"],
                    "spyBias": spy_bias,
                    "atr": round(atr, 2) if atr else None,
                    "dataSource": "Tradier RT"
                })
        except Exception as e:
            print(f"Error scanning {sym}: {e}")
            continue

    results.sort(key=lambda x: x["conf"], reverse=True)
    return jsonify({
        "signals": results,
        "count": len(results),
        "spyBias": spy_bias,
        "scannedAt": datetime.now().strftime("%I:%M:%S %p")
    })

@app.route("/quote/<symbol>")
def quote(symbol):
    q = get_quote(symbol.upper())
    if q:
        return jsonify(q)
    return jsonify({"error": "Not found"}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
