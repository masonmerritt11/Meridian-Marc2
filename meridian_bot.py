"""
MERIDIAN v3.0 — Tiered Futures Trading Bot
Strategy: Gold panning trickle-down + NY session breakout
Assets: Gold, Silver, Oil, BTC, ETH, XRP on Coinbase Derivatives
"""
import os, time, json, math, logging, datetime, uuid, hmac, hashlib
import statistics, threading, base64
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

# ══════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meridian")

# Credentials
CB_API_KEY  = os.environ.get("CB_API_KEY", "")
CB_API_SECRET = os.environ.get("CB_API_SECRET", "")
AV_KEY      = os.environ.get("AV_KEY", "")
EMAIL_FROM  = os.environ.get("EMAIL_FROM", "")
EMAIL_TO    = os.environ.get("EMAIL_TO", "")
EMAIL_PASS  = os.environ.get("EMAIL_PASS", "")
PORT        = int(os.environ.get("PORT", 8080))

# Dual portfolio balances
CRYPTO_START    = float(os.environ.get("CRYPTO_BALANCE",    "250"))
COMMODITY_START = float(os.environ.get("COMMODITY_BALANCE", "250"))

# ══════════════════════════════════════════════════════════
# ASSETS
# ══════════════════════════════════════════════════════════
ASSETS = {
    "BTC-USD": {"label":"Bitcoin",   "color":"🟠","type":"crypto",  "leverage":10,"vol":0.015},
    "ETH-USD": {"label":"Ethereum",  "color":"🔵","type":"crypto",  "leverage":10,"vol":0.020},
    "XRP-USD": {"label":"XRP",       "color":"💧","type":"crypto",  "leverage":10,"vol":0.022},
    "XAU-USD": {"label":"Gold",      "color":"🟡","type":"macro",   "leverage":20,"vol":0.006},
    "XAG-USD": {"label":"Silver",    "color":"⚪","type":"macro",   "leverage":20,"vol":0.012},
    "OIL-USD": {"label":"Crude Oil", "color":"🛢️","type":"energy",  "leverage":25,"vol":0.020},
}

CRYPTO_ASSETS    = {"BTC-USD","ETH-USD","XRP-USD"}
COMMODITY_ASSETS = {"XAU-USD","XAG-USD","OIL-USD"}

# ══════════════════════════════════════════════════════════
# TRADE TIERS
# ══════════════════════════════════════════════════════════
TIERS = {
    1: {"name":"Small",        "score_min":50, "score_max":64, "risk_pct":0.03,  "label":"Tier 1 — Small"},
    2: {"name":"Medium",       "score_min":65, "score_max":79, "risk_pct":0.06,  "label":"Tier 2 — Medium"},
    3: {"name":"High Conv.",   "score_min":80, "score_max":100,"risk_pct":0.12,  "label":"Tier 3 — High Conviction"},
}

def get_tier(score: int) -> Optional[dict]:
    for t in TIERS.values():
        if t["score_min"] <= score <= t["score_max"]:
            return t
    return None

# ══════════════════════════════════════════════════════════
# RISK / SAFETY CONFIG
# ══════════════════════════════════════════════════════════
SAFETY = {
    "paper_mode":           True,
    "max_trades_per_day":   6,
    "max_open_positions":   3,
    "max_daily_loss_pct":   0.20,   # halt if down 20% on day
    "max_drawdown_pct":     0.40,   # halt if down 40% from peak
    "min_rr_ratio":         1.3,    # minimum risk:reward — 1.3 for ranging, higher for trends
    "breakeven_at_r":       1.0,    # move stop to BE after 1R profit
    "partial_tp_at_r":      1.5,    # take 50% off at 1.5R
    "trail_remaining":      True,   # trail the other 50%
    "trail_atr_mult":       0.8,    # trail at 0.8x ATR
    "target_trades_per_day":3,      # aim for 3 trades/day
    "check_interval_secs":  900,    # 15-min scan
    "vol_circuit_breaker":  3.0,    # pause if vol spikes 3x
}

# ══════════════════════════════════════════════════════════
# FEES & MINIMUM PROFIT THRESHOLDS
# ══════════════════════════════════════════════════════════
# Coinbase futures: $0.20 per contract per side minimum
# Round trip (entry + exit) = $0.40 minimum per trade
# A trade is not worth taking if expected profit < fees

FEES = {
    "per_side":     4.71,   # $4.71 per contract per side (confirmed Gold rate)
    "round_trip":   9.42,   # total cost to open and close (2 x $4.71)
    "min_profit":   5.00,   # minimum NET profit after fees to be worth trading
}

# Note: fees may vary slightly by asset — $4.71 confirmed for Gold.
# Using same rate for all assets as conservative estimate.
# Real fee = 0.05% of notional + NFA/exchange fees

# Fixed dollar profit targets per asset — matches real trading style
# "I try to make about $20 before exiting"
# Stop is set at half the target distance for 2:1 R:R
# Targets set so net profit after $9.42 round-trip fees is meaningful
# Minimum target = fees ($9.42) + min_profit ($5) = $14.42 minimum
# Targets below are what you actually aim for — bot checks net profit automatically
TRADE_TARGETS = {
    #          target   stop     net after fees
    "BTC-USD": (30.00,  15.00),  # net $20.58 after fees
    "ETH-USD": (25.00,  12.50),  # net $15.58 after fees
    "XRP-USD": (25.00,  12.50),  # net $15.58 after fees
    "XAU-USD": (25.00,  12.50),  # net $15.58 after fees — Gold confirmed $4.71/side
    "XAG-USD": (30.00,  15.00),  # net $20.58 after fees — Silver higher notional
    "OIL-USD": (25.00,  12.50),  # net $15.58 after fees
}

# Units per contract — to convert dollar targets to price levels
UNITS_PER_CONTRACT = {
    "BTC-USD": 0.01,  "ETH-USD": 0.10,  "XRP-USD": 500,
    "XAU-USD": 1,     "XAG-USD": 50,    "OIL-USD": 10,
}

# ══════════════════════════════════════════════════════════
# SCORING WEIGHTS
# ══════════════════════════════════════════════════════════
# Total possible = 100 points
# Adjust these to tune the bot
SCORE_WEIGHTS = {
    "macro_clean":      15,  # no bad macro, aligned with asset
    "trend_clear":      15,  # clear directional trend
    "macd_confirms":    15,  # MACD histogram agrees
    "rsi_confirms":     10,  # RSI not overextended
    "ema_confirms":     15,  # price relative to EMAs
    "sr_fib_confirms":  10,  # at S/R or Fibonacci level
    "volume_confirms":  10,  # volatility/volume expansion
    "rr_clean":         10,  # R:R >= 2:1
}

# ══════════════════════════════════════════════════════════
# MACRO RULES
# ══════════════════════════════════════════════════════════
MACRO_RULES = {
    "XAU-USD": {"long": {"inflation_trend":["rising","stable"]},
                "short": {"dxy_trend":["rising"],"inflation_trend":["falling"]}},
    "XAG-USD": {"long": {"inflation_trend":["rising","stable"]},
                "short": {"dxy_trend":["rising"],"inflation_trend":["falling"]}},
    "OIL-USD": {"long": {"dxy_trend":["falling","neutral"]},
                "short": {"dxy_trend":["rising","neutral"]}},
    "BTC-USD": {"long": {"fed_stance":["dovish","neutral"]},
                "short": {"fed_stance":["hawkish","neutral"]}},
    "ETH-USD": {"long": {"fed_stance":["dovish","neutral"]},
                "short": {"fed_stance":["hawkish","neutral"]}},
    "XRP-USD": {"long": {"fed_stance":["dovish","neutral"]},
                "short": {"fed_stance":["hawkish","neutral"]}},
}

def macro_allows(symbol: str, direction: str, macro: dict) -> tuple[bool, str]:
    rules = MACRO_RULES.get(symbol, {}).get(direction, {})
    for factor, allowed in rules.items():
        actual = macro.get(factor, "neutral")
        if actual not in allowed:
            return False, f"{factor}={actual}"
    return True, "ok"

# ══════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════
state = {
    "account_balance":    CRYPTO_START + COMMODITY_START,
    "crypto_balance":     CRYPTO_START,
    "commodity_balance":  COMMODITY_START,
    "peak_balance":       CRYPTO_START + COMMODITY_START,
    "daily_start_bal":    CRYPTO_START + COMMODITY_START,
    "total_pnl":          0.0,
    "crypto_pnl":         0.0,
    "commodity_pnl":      0.0,
    "trades_today":       0,
    "wins":               0,
    "losses":             0,
    "total_trades":       0,
    "open_positions":     {},
    "trade_log":          [],
    "last_reset":         None,
    "global_halt":        False,
    "halt_reason":        "",
    "macro_context":      {},
    "last_macro_fetch":   None,
    "ny_session_range":   {},   # {symbol: {"high": x, "low": x, "set": bool}}
    "last_prices":        {},
}

# ══════════════════════════════════════════════════════════
# COINBASE API
# ══════════════════════════════════════════════════════════
CB_BASE = "https://api.coinbase.com"

def cb_sign(method: str, path: str, body: str = "") -> dict:
    ts  = str(int(time.time()))
    msg = ts + method.upper() + path + body
    sig = hmac.new(
        CB_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()
    return {
        "CB-ACCESS-KEY":       CB_API_KEY,
        "CB-ACCESS-SIGN":      sig,
        "CB-ACCESS-TIMESTAMP": ts,
        "Content-Type":        "application/json",
    }

def cb_get(path: str) -> dict:
    headers = cb_sign("GET", path)
    r = requests.get(CB_BASE + path, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

def cb_post(path: str, body: dict) -> dict:
    b = json.dumps(body)
    headers = cb_sign("POST", path, b)
    r = requests.post(CB_BASE + path, headers=headers, data=b, timeout=10)
    r.raise_for_status()
    return r.json()

# ══════════════════════════════════════════════════════════
# FUTURES CONTRACT RESOLUTION
# ══════════════════════════════════════════════════════════
# Confirmed format from coinbase.com URL: GOL-27MAR26-CDE
FUTURES_BASE = {"XAU-USD":"GOL","XAG-USD":"SLR","OIL-USD":"NOL"}
MON_NAMES    = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
# Approximate expiry days per asset per month
EXPIRY_DAYS  = {
    "GOL": {2:19,4:28,6:26,8:19,10:16,12:18},
    "SLR": {1:16,2:19,3:27,4:28,5:18,6:26,7:17,8:19,9:25,10:16,11:19,12:18},
    "NOL": {1:16,2:19,3:19,4:17,5:18,6:18,7:17,8:19,9:18,10:16,11:18,12:18},
}
GOLD_MONTHS  = {2,4,6,8,10,12}  # Gold only trades even months

def get_futures_ids(base: str) -> list:
    """Build list of candidate product IDs to try."""
    now = datetime.datetime.now()
    ids = []
    for delta in range(8):
        m = now.month + delta
        y = now.year + (m-1)//12
        m = ((m-1)%12)+1
        if base == "GOL" and m not in GOLD_MONTHS:
            continue
        exp = EXPIRY_DAYS.get(base,{}).get(m, 18)
        if delta == 0 and now.day >= exp:
            continue
        ids.append(f"{base}-{exp:02d}{MON_NAMES[m]}{str(y)[-2:]}-CDE")
        if len(ids) >= 4:
            break
    return ids

# ══════════════════════════════════════════════════════════
# PRICE & CANDLES
# ══════════════════════════════════════════════════════════
def get_price(symbol: str) -> Optional[float]:
    # Crypto — public spot price
    if symbol in CRYPTO_ASSETS:
        try:
            sym = symbol.replace("-USD","")
            r   = requests.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=8)
            return float(r.json()["data"]["amount"])
        except Exception:
            pass
        try:
            d = cb_get(f"/api/v3/brokerage/market/products/{symbol}")
            p = float(d.get("price",0))
            if p > 0: return p
        except Exception:
            pass
        return state["last_prices"].get(symbol)

    # Commodities — futures contracts
    base = FUTURES_BASE.get(symbol)
    if not base:
        return None
    for pid in get_futures_ids(base):
        try:
            d = cb_get(f"/api/v3/brokerage/market/products/{pid}")
            p = float(d.get("price",0))
            if p > 0:
                log.info(f"  {symbol}: ${p:.4f} via {pid}")
                return p
        except Exception:
            continue
    # AV fallback for gold/silver (cached 6h)
    if symbol in {"XAU-USD","XAG-USD"}:
        cache_k = f"av_{symbol}"; cache_t = f"av_t_{symbol}"
        cp = state.get(cache_k); ct = state.get(cache_t)
        if cp and ct:
            age = (datetime.datetime.now()-datetime.datetime.fromisoformat(ct)).total_seconds()
            if age < 21600:
                return cp
        fx = "XAU" if symbol=="XAU-USD" else "XAG"
        try:
            url = (f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
                   f"&from_currency={fx}&to_currency=USD&apikey={AV_KEY}")
            r = requests.get(url, timeout=10)
            p = float(r.json()["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
            if p > 0:
                state[cache_k] = p
                state[cache_t] = datetime.datetime.now().isoformat()
                return p
        except Exception:
            pass
    cached = state["last_prices"].get(symbol)
    if cached:
        return cached
    # Absolute last resort for Gold — use known approximate price
    # This prevents Gold from being completely dead
    if symbol == "XAU-USD":
        log.warning("  XAU-USD: using fallback estimate $3300")
        return 3300.0  # approximate — will be replaced when AV cache refreshes
    return None

def get_candles(symbol: str, granularity: str = "auto", limit: int = 100) -> list:
    """Fetch OHLC candles. Auto-selects timeframe by asset type."""
    if granularity == "auto":
        granularity = "FIVE_MINUTE" if symbol in CRYPTO_ASSETS else "ONE_HOUR"
    limit = min(limit, 300)

    # Crypto — direct product
    if symbol in CRYPTO_ASSETS:
        try:
            path = (f"/api/v3/brokerage/market/products/{symbol}/candles"
                    f"?granularity={granularity}&limit={limit}")
            raw  = cb_get(path).get("candles",[])
            if raw:
                return [{"o":float(c["open"]),"h":float(c["high"]),
                         "l":float(c["low"]),"c":float(c["close"])}
                        for c in reversed(raw)]
        except Exception:
            pass
        # Public fallback
        try:
            gran_secs = {"FIVE_MINUTE":300,"FIFTEEN_MINUTE":900,"THIRTY_MINUTE":1800,"ONE_HOUR":3600,"TWO_HOUR":7200,"SIX_HOUR":21600,"ONE_DAY":86400}.get(granularity,300)
            url = (f"https://api.exchange.coinbase.com/products/{symbol}/candles"
                   f"?granularity={gran_secs}&limit={limit}")
            r   = requests.get(url, timeout=10)
            raw = r.json()
            if isinstance(raw,list) and raw:
                return [{"o":float(c[3]),"h":float(c[2]),"l":float(c[1]),"c":float(c[4])}
                        for c in reversed(raw)]
        except Exception:
            pass
        return []

    # Commodities — futures contract
    base = FUTURES_BASE.get(symbol,"")
    for pid in get_futures_ids(base):
        try:
            gran = "ONE_HOUR" if symbol in COMMODITY_ASSETS else granularity
            path = (f"/api/v3/brokerage/market/products/{pid}/candles"
                    f"?granularity={gran}&limit={limit}")
            raw  = cb_get(path).get("candles",[])
            if raw:
                log.info(f"  {symbol}: {len(raw)} candles via {pid}")
                return [{"o":float(c["open"]),"h":float(c["high"]),
                         "l":float(c["low"]),"c":float(c["close"])}
                        for c in reversed(raw)]
        except Exception:
            continue
    return []

# ══════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════
def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period+1: return 50.0
    g = l = 0.0
    for i in range(len(closes)-period, len(closes)):
        d = closes[i]-closes[i-1]
        if d > 0: g += d
        else: l += abs(d)
    return 100-100/(1+(g/(l or 1e-9)))

def calc_ema(closes: list, period: int) -> float:
    k = 2/(period+1); e = closes[0]
    for c in closes[1:]: e = c*k+e*(1-k)
    return e

def calc_ema_list(closes: list, period: int) -> list:
    k = 2/(period+1); e = closes[0]; r = [e]
    for c in closes[1:]: e = c*k+e*(1-k); r.append(e)
    return r

def calc_macd(closes: list) -> dict:
    if len(closes) < 26: return {"hist":0,"prev_hist":0,"bullish":False,"bearish":False}
    e12 = calc_ema_list(closes, 12)
    e26 = calc_ema_list(closes, 26)
    ml  = [e12[i]-e26[i] for i in range(len(closes))]
    sig = calc_ema_list(ml, 9)
    hist     = [ml[i]-sig[i] for i in range(len(ml))]
    h_now    = hist[-1]
    h_prev   = hist[-2] if len(hist)>1 else 0
    return {
        "hist":     h_now,
        "prev_hist":h_prev,
        "bullish":  h_now > 0 and h_now > h_prev,
        "bearish":  h_now < 0 and h_now < h_prev,
        "crossover_bull": h_now > 0 and h_prev <= 0,
        "crossover_bear": h_now < 0 and h_prev >= 0,
    }

def calc_atr(candles: list, period: int = 14) -> float:
    if len(candles) < 2: return 0
    trs = [max(candles[i]["h"]-candles[i]["l"],
               abs(candles[i]["h"]-candles[i-1]["c"]),
               abs(candles[i]["l"]-candles[i-1]["c"]))
           for i in range(1, len(candles))]
    return sum(trs[-period:])/period if len(trs)>=period else sum(trs)/max(len(trs),1)

def calc_bb(closes: list, period: int = 20) -> dict:
    sl  = closes[-period:]; sma = sum(sl)/len(sl)
    std = math.sqrt(sum((x-sma)**2 for x in sl)/len(sl)) if len(sl)>1 else 0
    return {"upper":sma+2*std,"lower":sma-2*std,"mid":sma,"width":4*std/sma*100 if sma>0 else 0}

def calc_sr(candles: list, price: float) -> tuple[float,float]:
    """
    Find significant support and resistance levels.
    Uses ALL available candles for better level detection.
    Minimum distance enforced so stop/target are always meaningful.
    """
    H = [c["h"] for c in candles]
    L = [c["l"] for c in candles]

    def cluster(arr, tol=0.005):
        s = sorted(arr); cs = []
        for v in s:
            if cs and abs(v-cs[-1][-1])/v < tol: cs[-1].append(v)
            else: cs.append([v])
        return [sum(c)/len(c) for c in cs]

    sups = [v for v in cluster(L) if v < price * 0.999]
    ress = [v for v in cluster(H) if v > price * 1.001]

    # Use nearest significant level
    sup = sups[-1] if sups else min(L)
    res = ress[0]  if ress else max(H)

    # Enforce minimum distance — at least 0.5% from price
    # This ensures stop and target are always tradeable
    min_dist = price * 0.005
    if price - sup < min_dist:
        sup = price - min_dist * 2
    if res - price < min_dist:
        res = price + min_dist * 2

    return sup, res

def calc_fib_levels(high: float, low: float) -> dict:
    rng = high - low
    return {
        "0.236": low + rng*0.236,
        "0.382": low + rng*0.382,
        "0.500": low + rng*0.500,
        "0.618": low + rng*0.618,
        "0.786": low + rng*0.786,
    }

def near_fib(price: float, fibs: dict, tolerance: float = 0.005) -> bool:
    return any(abs(price-v)/price < tolerance for v in fibs.values())

def detect_regime(closes: list) -> str:
    if len(closes) < 30: return "ranging"
    pv   = closes[-30:]
    bull = sum(1 for i in range(2,len(pv)) if pv[i]>pv[i-2])
    bear = len(pv)-2-bull
    br   = bull/(bull+bear) if (bull+bear)>0 else 0.5
    e20  = calc_ema(closes[-20:], 20)
    e50  = calc_ema(closes[-50:] if len(closes)>=50 else closes, 50)
    sp   = (e20-e50)/closes[-1]*100
    if br > 0.60 and sp > 0.3:  return "trending_up"
    if br < 0.40 and sp < -0.3: return "trending_down"
    # Volatility check
    if len(closes) >= 20:
        rv = statistics.stdev(closes[-10:])/closes[-1]
        bv = statistics.stdev(closes[-30:-10])/closes[-30] if len(closes)>=30 else rv
        if rv/bv > 3.0: return "volatile"
    return "ranging"

# ══════════════════════════════════════════════════════════
# NY SESSION BREAKOUT
# ══════════════════════════════════════════════════════════
def update_ny_range(symbol: str, candles: list):
    """
    Track the NY session opening range (9:30-9:45 AM ET).
    For commodities — use London open (8:00-8:15 AM ET).
    The first 15-min candle after session open sets the range.
    """
    now_et = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4)
    hour   = now_et.hour
    minute = now_et.minute

    # Define session open window
    if symbol in CRYPTO_ASSETS:
        session_hour = 9; session_min = 30  # NY open
    else:
        session_hour = 8; session_min = 0   # London/NY commodity open

    # Check if we're in the first 15 minutes of session
    in_window = (hour == session_hour and session_min <= minute <= session_min+15)

    if in_window and len(candles) >= 1:
        # Set or update the session range from recent candles
        recent = candles[-3:]  # last 3 candles cover 15 min at 5-min intervals
        session_high = max(c["h"] for c in recent)
        session_low  = min(c["l"] for c in recent)
        state["ny_session_range"][symbol] = {
            "high":    session_high,
            "low":     session_low,
            "set":     True,
            "date":    now_et.date().isoformat(),
        }
        log.info(f"  {symbol}: NY range set — H:{session_high:.4f} L:{session_low:.4f}")

def check_ny_breakout(symbol: str, price: float, direction: str) -> tuple[bool, str]:
    """
    Check if price is breaking out of or retesting the NY session range.
    Returns (is_breakout_setup, note)
    """
    rng = state["ny_session_range"].get(symbol, {})
    if not rng.get("set"):
        return False, "No NY range set yet"

    # Only use today's range
    today = (datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=4)).date().isoformat()
    if rng.get("date") != today:
        return False, "Range from different day"

    high    = rng["high"]
    low     = rng["low"]
    rng_size= high - low
    tol     = rng_size * 0.15  # 15% of range = tolerance for retest

    if direction == "long":
        # Bullish breakout: price broke above range high and is retesting
        if price > high and price < high + tol:
            return True, f"NY breakout long — retesting {high:.4f}"
        # Or price bounced off range low (range support)
        if abs(price - low) < tol:
            return True, f"NY range support long — at {low:.4f}"

    elif direction == "short":
        # Bearish breakdown: price broke below range low
        if price < low and price > low - tol:
            return True, f"NY breakdown short — retesting {low:.4f}"
        # Or price rejected at range high
        if abs(price - high) < tol:
            return True, f"NY range resistance short — at {high:.4f}"

    return False, "Not at NY range level"

# ══════════════════════════════════════════════════════════
# MASTER SCORING ENGINE
# ══════════════════════════════════════════════════════════
def score_setup(symbol: str, candles: list, price: float,
                macro: dict, direction: str,
                candles_htf: list = None) -> dict:
    """
    Score a potential trade from 0-100.
    Uses weighted indicators — no single indicator is required.
    Returns full score breakdown.
    """
    if len(candles) < 20:
        return {"score":0,"tier":None,"reason":"Insufficient candles"}

    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    rsi_val  = calc_rsi(closes)
    macd     = calc_macd(closes)
    bb       = calc_bb(closes)
    atr_val  = calc_atr(candles)   # entry TF ATR — for BB/vol scoring
    # Use higher TF ATR for stop/target sizing — much more meaningful
    if candles_htf and len(candles_htf) >= 14:
        atr_htf = calc_atr(candles_htf)  # 1h ATR — wider, avoids noise stops
    else:
        atr_htf = atr_val * 12  # rough 1h estimate from 5-min (12 x 5min = 1h)
    e20      = calc_ema(closes[-20:], 20)
    e50      = calc_ema(closes[-50:] if len(closes)>=50 else closes, 50)
    e200     = calc_ema(closes[-100:] if len(closes)>=100 else closes, 100)
    sup, res = calc_sr(candles, price)
    regime   = detect_regime(closes)
    fibs     = calc_fib_levels(max(highs[-50:]), min(lows[-50:]))

    score       = 0
    breakdown   = {}
    notes       = []

    # ── 1. MACRO CLEAN (+15) ──────────────────────────────
    macro_ok, macro_reason = macro_allows(symbol, direction, macro)
    if macro_ok:
        score += SCORE_WEIGHTS["macro_clean"]
        breakdown["macro"] = SCORE_WEIGHTS["macro_clean"]
        notes.append("macro ✓")
    else:
        breakdown["macro"] = 0
        notes.append(f"macro ✗ ({macro_reason})")

    # ── 2. TREND CLEAR (+15) ─────────────────────────────
    trend_pts = 0
    if direction == "long":
        if regime == "trending_up":  trend_pts = 15
        elif regime == "ranging":    trend_pts = 8
        else:                        trend_pts = 0
    else:
        if regime == "trending_down": trend_pts = 15
        elif regime == "ranging":     trend_pts = 8
        else:                         trend_pts = 0
    score += trend_pts
    breakdown["trend"] = trend_pts
    notes.append(f"trend={regime}({trend_pts})")

    # ── 3. MACD CONFIRMS (+15) ───────────────────────────
    macd_pts = 0
    if direction == "long":
        if macd["crossover_bull"]: macd_pts = 15
        elif macd["bullish"]:      macd_pts = 10
        elif macd["hist"] > 0:     macd_pts = 5
    else:
        if macd["crossover_bear"]: macd_pts = 15
        elif macd["bearish"]:      macd_pts = 10
        elif macd["hist"] < 0:     macd_pts = 5
    score += macd_pts
    breakdown["macd"] = macd_pts
    notes.append(f"macd={macd_pts}")

    # ── 4. RSI CONFIRMS (+10) ────────────────────────────
    rsi_pts = 0
    if direction == "long":
        if 25 < rsi_val < 50:      rsi_pts = 10   # ideal — room to run
        elif 50 <= rsi_val < 60:   rsi_pts = 6    # ok
        elif rsi_val <= 25:        rsi_pts = 8    # oversold bounce
    else:
        if 50 < rsi_val < 75:      rsi_pts = 10
        elif 40 < rsi_val <= 50:   rsi_pts = 6
        elif rsi_val >= 75:        rsi_pts = 8    # overbought
    score += rsi_pts
    breakdown["rsi"] = rsi_pts
    notes.append(f"rsi={rsi_val:.0f}({rsi_pts})")

    # ── 5. EMA CHANNEL CONFIRMS (+15) ────────────────────
    ema_pts = 0
    if direction == "long":
        if price > e20 and e20 > e50:   ema_pts = 15  # above both, aligned
        elif price > e20:               ema_pts = 8
        elif price < e20 and e20 < e50: ema_pts = 5   # pullback to EMA
    else:
        if price < e20 and e20 < e50:   ema_pts = 15
        elif price < e20:               ema_pts = 8
        elif price > e20 and e20 > e50: ema_pts = 5
    score += ema_pts
    breakdown["ema"] = ema_pts
    notes.append(f"ema={ema_pts}")

    # ── 6. S/R or FIBONACCI CONFIRMS (+10) ──────────────
    sr_pts = 0
    dist_s = abs(price-sup)/price
    dist_r = abs(price-res)/price
    at_fib = near_fib(price, fibs)
    if direction == "long":
        if dist_s < 0.008:   sr_pts = 10  # tight to support
        elif dist_s < 0.015: sr_pts = 7
        elif at_fib:         sr_pts = 8
    else:
        if dist_r < 0.008:   sr_pts = 10
        elif dist_r < 0.015: sr_pts = 7
        elif at_fib:         sr_pts = 8
    score += sr_pts
    breakdown["sr_fib"] = sr_pts
    notes.append(f"sr/fib={sr_pts}")

    # ── 7. VOLUME / VOLATILITY CONFIRMS (+10) ────────────
    vol_pts = 0
    bb_width = bb["width"]
    # Futures have different vol profiles — wider thresholds
    if 0.5 < bb_width < 12.0:   vol_pts = 10   # healthy vol range for futures
    elif 0.2 < bb_width <= 0.5: vol_pts = 6    # low vol — squeeze coming
    elif bb_width > 12.0:       vol_pts = 3    # very high vol — careful
    score += vol_pts
    breakdown["vol"] = vol_pts
    notes.append(f"vol={vol_pts}")

    # ── 8. FIXED DOLLAR TARGETS + FEE CHECK ─────────────
    # Fixed profit targets matching real trading style (~$20/trade)
    # Stop = half target = always 2:1 R:R
    # Net profit must exceed $0.40 round-trip fees
    tgt_d, stop_d = TRADE_TARGETS.get(symbol, (20.00, 10.00))
    units = UNITS_PER_CONTRACT.get(symbol, 1)
    target_move = tgt_d  / units
    stop_move   = stop_d / units
    if direction == "long":
        stop   = price - stop_move
        target = price + target_move
    else:
        stop   = price + stop_move
        target = price - target_move
    net_profit = tgt_d - FEES["round_trip"]
    if net_profit < FEES["min_profit"]:
        return {"score":0,"tier":None,"direction":direction,
                "reason":f"Fee kill — net ${net_profit:.2f} < min ${FEES['min_profit']}",
                "stop":stop,"target":target,"rr":2.0,"atr":atr_val,
                "atr_entry":atr_val,"sup":sup,"res":res,"breakdown":breakdown}
    rr     = abs(target-price) / abs(price-stop) if abs(price-stop) > 0 else 2.0
    rr_pts = 10  # always full points — 2:1 R:R guaranteed with fixed targets
    score += rr_pts
    breakdown["rr"] = rr_pts
    notes.append(f"target=${tgt_d}(net_${net_profit:.2f})")


    # ── NY SESSION BONUS (+10 bonus, not in base 100) ────
    ny_bonus = 0
    ny_hit, ny_note = check_ny_breakout(symbol, price, direction)
    if ny_hit:
        ny_bonus = 10
        notes.append(f"NY_breakout +10")
    score = min(100, score + ny_bonus)
    breakdown["ny_bonus"] = ny_bonus

    # ── REGIME VETO ──────────────────────────────────────
    if regime == "volatile":
        return {"score":0,"tier":None,"direction":direction,
                "reason":"Volatile regime — standing aside",
                "stop":stop,"target":target,"rr":rr,"atr":atr_val,
                "sup":sup,"res":res,"breakdown":breakdown}

    # Downtrend blocks longs, uptrend blocks shorts (unless at extreme S/R)
    if regime == "trending_down" and direction == "long" and sr_pts < 7:
        score = min(score, 55)  # cap at Tier 1 max — don't allow T2/T3 longs in downtrend
    if regime == "trending_up" and direction == "short" and sr_pts < 7:
        score = min(score, 55)

    tier   = get_tier(score)
    reason = " | ".join(notes)

    return {
        "score":     score,
        "tier":      tier,
        "direction": direction,
        "reason":    reason,
        "stop":      stop,
        "target":    target,
        "rr":        rr,
        "atr":       atr_htf if candles_htf else atr_val,
        "atr_entry": atr_val,
        "target_dollars": TRADE_TARGETS.get(symbol,(20,10))[0],
        "stop_dollars":   TRADE_TARGETS.get(symbol,(20,10))[1],
        "sup":       sup,
        "res":       res,
        "regime":    regime,
        "rsi":       rsi_val,
        "macd":      macd,
        "breakdown": breakdown,
        "ny_hit":    ny_hit,
        "ny_note":   ny_note if ny_hit else "",
    }

# ══════════════════════════════════════════════════════════
# MACRO DATA
# ══════════════════════════════════════════════════════════
def fetch_macro() -> dict:
    """Fetch macro context every 6 hours."""
    last = state.get("last_macro_fetch")
    if last:
        age = (datetime.datetime.now()-datetime.datetime.fromisoformat(last)).total_seconds()
        if age < 21600 and state.get("macro_context"):
            return state["macro_context"]
    try:
        url = (f"https://www.alphavantage.co/query?function=FEDERAL_FUNDS_RATE"
               f"&interval=monthly&apikey={AV_KEY}")
        r   = requests.get(url, timeout=10)
        d   = r.json().get("data",[])
        rate_now  = float(d[0]["value"]) if d else 5.25
        rate_prev = float(d[1]["value"]) if len(d)>1 else rate_now
        if rate_now > rate_prev + 0.1:   fed = "hawkish"
        elif rate_now < rate_prev - 0.1: fed = "dovish"
        else:                             fed = "neutral"
    except Exception:
        fed = "hawkish"  # current default

    try:
        url = (f"https://www.alphavantage.co/query?function=CPI"
               f"&interval=monthly&apikey={AV_KEY}")
        r   = requests.get(url, timeout=10)
        d   = r.json().get("data",[])
        c1  = float(d[0]["value"]) if d else 3.5
        c2  = float(d[1]["value"]) if len(d)>1 else c1
        if c1 > c2:   inflation = "rising"
        elif c1 < c2: inflation = "falling"
        else:          inflation = "stable"
    except Exception:
        inflation = "rising"

    try:
        url = (f"https://www.alphavantage.co/query?function=REAL_GDP"
               f"&interval=quarterly&apikey={AV_KEY}")
        r   = requests.get(url, timeout=10)
        d   = r.json().get("data",[])
        emp = "strong" if d and float(d[0]["value"])>2.0 else "weakening"
    except Exception:
        emp = "strong"

    macro = {
        "fed_stance":      fed,
        "inflation_trend": inflation,
        "employment":      emp,
        "dxy_trend":       "rising",  # current default — override if AV has DXY
        "risk_appetite":   "neutral",
    }
    state["macro_context"]    = macro
    state["last_macro_fetch"] = datetime.datetime.now().isoformat()
    log.info(f"Macro: {macro}")
    return macro

# ══════════════════════════════════════════════════════════
# ORDER EXECUTION
# ══════════════════════════════════════════════════════════
def place_order(symbol: str, side: str, size: float, price: float) -> str:
    oid = str(uuid.uuid4())[:8]
    if SAFETY["paper_mode"]:
        log.info(f"  [PAPER] {side.upper()} {symbol} size={size:.4f} @ ${price:.4f}")
        return oid
    try:
        body = {
            "client_order_id": oid,
            "product_id":      symbol,
            "side":            side.upper(),
            "order_configuration": {
                "market_market_ioc": {"quote_size": str(round(size*price,2))}
            }
        }
        r = cb_post("/api/v3/brokerage/orders", body)
        return r.get("order_id", oid)
    except Exception as e:
        log.error(f"Order failed {symbol}: {e}")
        return oid

def get_balance(portfolio: str = "total") -> float:
    if SAFETY["paper_mode"]:
        if portfolio == "crypto":    return state["crypto_balance"]
        if portfolio == "commodity": return state["commodity_balance"]
        return state["account_balance"]
    try:
        data = cb_get("/api/v3/brokerage/accounts")
        for a in data.get("accounts",[]):
            if a.get("currency") == "USDC":
                bal = float(a["available_balance"]["value"])
                state["account_balance"] = bal
                return bal
    except Exception:
        pass
    return state["account_balance"]

# ══════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════
def open_position(symbol: str, score_data: dict, price: float):
    """Open a new position based on score data and tier."""
    tier = score_data["tier"]
    if not tier:
        return

    direction = score_data["direction"]
    portfolio = "crypto" if symbol in CRYPTO_ASSETS else "commodity"
    balance   = get_balance(portfolio)

    # Size based on tier risk %
    risk_pct  = tier["risk_pct"]
    risk_usd  = balance * risk_pct
    stop_dist = abs(price - score_data["stop"])
    if stop_dist <= 0:
        return
    size = risk_usd / stop_dist

    # ── POSITION SIZING — 1 CONTRACT PER TRADE ──────────────
    # Trade exactly like manual trading:
    # Always 1 contract. Stop loss defines the dollar risk.
    # No complex margin math — Coinbase handles margin automatically.
    # The account just needs enough to cover the margin hold.
    #
    # Contract sizes (units per contract):
    #   BTC-USD: 0.01 BTC  | ETH-USD: 0.10 ETH  | XRP-USD: 500 XRP
    #   XAU-USD: 1 troy oz | XAG-USD: 50 troy oz | OIL-USD: 10 barrels
    UNITS_PER_CONTRACT = {
        "BTC-USD": 0.01, "ETH-USD": 0.10, "XRP-USD": 500,
        "XAU-USD": 1,    "XAG-USD": 50,   "OIL-USD": 10,
    }
    units_per = UNITS_PER_CONTRACT.get(symbol, 1)
    size      = units_per  # always exactly 1 contract

    # Dollar risk on this trade = stop distance × contract size
    dollar_risk = abs(price - score_data["stop"]) * units_per
    log.info(f"  {symbol}: 1 contract | "
             f"stop distance ${abs(price-score_data['stop']):.4f} | "
             f"dollar risk ~${dollar_risk:.2f}")

    # Check drawdown limits
    daily_loss = state["daily_start_bal"] - state["account_balance"]
    if daily_loss / state["daily_start_bal"] > SAFETY["max_daily_loss_pct"]:
        log.warning("Daily loss limit hit — no new trades")
        return
    dd = (state["peak_balance"] - state["account_balance"]) / state["peak_balance"]
    if dd > SAFETY["max_drawdown_pct"]:
        log.warning(f"Max drawdown {dd:.1%} — halting")
        state["global_halt"] = True
        state["halt_reason"] = f"Drawdown {dd:.1%}"
        return

    oid = place_order(symbol, "buy" if direction=="long" else "sell", size, price)

    state["open_positions"][symbol] = {
        "side":         direction,
        "entry":        price,
        "stop":         score_data["stop"],
        "target":       score_data["target"],
        "target1":      price + abs(score_data["target"]-price)*0.5 if direction=="long" \
                        else price - abs(score_data["target"]-price)*0.5,
        "size":         size,
        "size_remaining": size,
        "score":        score_data["score"],
        "tier":         tier["name"],
        "tier_num":     next(k for k,v in TIERS.items() if v["name"]==tier["name"]),
        "rr":           score_data["rr"],
        "atr":          score_data["atr"],
        "stop_moved_be":False,
        "partial_done": False,
        "reason":       score_data["reason"],
        "ny_hit":       score_data.get("ny_hit",False),
        "order_id":     oid,
        "opened_at":    datetime.datetime.now().isoformat(),
        "portfolio":    portfolio,
    }
    state["trades_today"] += 1

    icon = "🟢" if direction=="long" else "🔴"
    log.info(f"\n{'*'*55}")
    log.info(f"{icon} {tier['label'].upper()} {symbol} {direction.upper()}")
    log.info(f"   Score:  {score_data['score']}/100")
    log.info(f"   Entry:  ${price:.4f} | Stop: ${score_data['stop']:.4f} | "
             f"Target: ${score_data['target']:.4f}")
    log.info(f"   R:R:    {score_data['rr']:.1f}:1 | Risk: ${risk_usd:.2f} ({risk_pct*100:.0f}%)")
    log.info(f"   Setup:  {score_data['reason'][:80]}")
    if score_data.get("ny_hit"):
        log.info(f"   NY:     {score_data['ny_note']}")
    log.info(f"{'*'*55}\n")

def close_position(symbol: str, pos: dict, price: float, reason: str):
    """Close a position and record the trade."""
    side   = pos["side"]
    entry  = pos["entry"]
    size   = pos.get("size_remaining", pos["size"])
    pnl    = (price-entry)*size if side=="long" else (entry-price)*size

    # Update balances
    state["account_balance"] += pnl
    state["total_pnl"]       += pnl
    state["peak_balance"]     = max(state["peak_balance"], state["account_balance"])
    if pos["portfolio"] == "crypto":
        state["crypto_balance"] += pnl
        state["crypto_pnl"]     += pnl
    else:
        state["commodity_balance"] += pnl
        state["commodity_pnl"]     += pnl

    if pnl > 0: state["wins"] += 1
    else:       state["losses"] += 1
    state["total_trades"] += 1

    # Log trade
    trade_record = {
        "symbol":     symbol,
        "side":       side,
        "entry":      entry,
        "exit":       price,
        "stop":       pos["stop"],
        "target":     pos["target"],
        "size":       pos["size"],
        "pnl":        pnl,
        "score":      pos["score"],
        "tier":       pos["tier"],
        "rr":         pos["rr"],
        "reason_in":  pos["reason"][:60],
        "reason_out": reason,
        "result":     "WIN" if pnl>0 else "LOSS",
        "ny_trade":   pos.get("ny_hit",False),
        "opened_at":  pos["opened_at"],
        "closed_at":  datetime.datetime.now().isoformat(),
    }
    state["trade_log"].append(trade_record)
    del state["open_positions"][symbol]

    icon = "✅" if pnl>0 else "❌"
    log.info(f"{icon} CLOSED {symbol} | P&L: ${pnl:+.2f} | Reason: {reason} | "
             f"Balance: ${state['account_balance']:,.2f}")

def check_exits(prices: dict):
    """Check all open positions for exit conditions."""
    for symbol, pos in list(state["open_positions"].items()):
        try:
            price = prices.get(symbol)
            if not price: continue

            side   = pos["side"]
            entry  = pos["entry"]
            stop   = pos["stop"]
            target = pos["target"]
            atr    = pos.get("atr", price * 0.01)

            # Original stop distance — fixed reference for R calculations
            if not pos.get("orig_stop_dist"):
                pos["orig_stop_dist"] = abs(entry - stop) or atr

            osd = pos["orig_stop_dist"]
            r_moved = (price-entry)/osd if side=="long" else (entry-price)/osd

            # Unrealized P&L
            sz = pos.get("size_remaining", pos["size"])
            upnl = (price-entry)*sz if side=="long" else (entry-price)*sz
            log.info(f"  {symbol} {side.upper()}: ${price:.4f} | "
                     f"stop=${stop:.4f} target=${target:.4f} | "
                     f"R={r_moved:.2f} | P&L=${upnl:+.2f}")

            # Stop hit
            if (side=="long" and price<=stop) or (side=="short" and price>=stop):
                close_position(symbol, pos, price, "Stop loss")
                continue

            # Target hit
            if (side=="long" and price>=target) or (side=="short" and price<=target):
                close_position(symbol, pos, price, "Target hit")
                continue

            # Move stop to break-even at 1R
            if not pos.get("stop_moved_be") and r_moved >= SAFETY["breakeven_at_r"]:
                pos["stop"] = entry
                pos["stop_moved_be"] = True
                log.info(f"  {symbol}: ✅ BE stop set ${entry:.4f}")

            # Partial take-profit at 1.5R
            if not pos.get("partial_done") and r_moved >= SAFETY.get("partial_tp_at_r", 1.5):
                half = pos["size"] / 2
                pnl  = (price-entry)*half if side=="long" else (entry-price)*half
                state["account_balance"] += pnl
                state["total_pnl"] += pnl
                key = "crypto_pnl" if pos.get("portfolio")=="crypto" else "commodity_pnl"
                state[key] += pnl
                pos["size_remaining"] = half
                pos["partial_done"] = True
                log.info(f"  {symbol}: 💰 Partial TP ${pnl:+.2f} at {r_moved:.1f}R")

            # Trail stop after partial
            if pos.get("partial_done") and SAFETY.get("trail_remaining"):
                trail = atr * SAFETY.get("trail_atr_mult", 0.8)
                ns = (price-trail) if side=="long" else (price+trail)
                if side=="long" and ns > pos["stop"]:
                    pos["stop"] = ns
                    log.info(f"  {symbol}: 📈 Trail → ${ns:.4f}")
                elif side=="short" and ns < pos["stop"]:
                    pos["stop"] = ns
                    log.info(f"  {symbol}: 📉 Trail → ${ns:.4f}")

        except Exception as e:
            log.error(f"  Exit error {symbol}: {e}", exc_info=True)


def scan():
    """One full scan cycle — fetch prices, score setups, manage positions."""
    macro  = fetch_macro()
    prices = {}

    # Fetch all prices
    log.info("Fetching prices...")
    for symbol in ASSETS:
        try:
            p = get_price(symbol)
            if p and p > 0:
                prices[symbol] = p
                state["last_prices"][symbol] = p
                log.info(f"  {ASSETS[symbol]['color']} {symbol}: ${p:,.4f}")
            else:
                log.warning(f"  {symbol}: price fetch returned None/zero")
        except Exception as e:
            log.warning(f"  {symbol}: price fetch error — {e}")

    # Check exits first
    check_exits(prices)

    # Count open positions
    n_open = len(state["open_positions"])
    if n_open >= SAFETY["max_open_positions"]:
        log.info(f"Max positions open ({n_open}) — skipping entries")
        return
    if state["trades_today"] >= SAFETY["max_trades_per_day"]:
        log.info(f"Daily trade limit hit ({state['trades_today']}) — done for today")
        return
    if state["global_halt"]:
        log.info(f"HALTED: {state['halt_reason']}")
        return

    log.info("Scanning for setups...")

    # Score every asset in both directions
    best_setups = []
    for symbol in ASSETS:
        if symbol in state["open_positions"]:
            continue
        price = prices.get(symbol)
        if not price:
            continue

        # Fetch candles — 5min for entry signals, 1h for risk sizing
        if symbol in CRYPTO_ASSETS:
            candles_entry = get_candles(symbol, granularity="FIVE_MINUTE",  limit=100)
            candles_1h    = get_candles(symbol, granularity="ONE_HOUR",     limit=60)
        else:
            candles_entry = get_candles(symbol, granularity="ONE_HOUR",     limit=80)
            candles_1h    = get_candles(symbol, granularity="SIX_HOUR",     limit=40)
        candles = candles_entry  # primary for scoring
        if candles_1h:
            log.info(f"  {symbol}: {len(candles_entry)} entry candles + {len(candles_1h)} HTF candles")

        if len(candles) < 20:
            log.info(f"  {symbol}: Insufficient candles")
            continue

        # Update NY session range
        update_ny_range(symbol, candles)

        # Score both directions
        for direction in ("long","short"):
            s = score_setup(symbol, candles, price, macro, direction, candles_1h)
            if s["tier"]:  # score >= 50
                best_setups.append((symbol, price, s, candles))
                log.info(f"  {symbol} {direction.upper()}: score={s['score']}/100 "
                         f"tier={s['tier']['name']} rr={s['rr']:.1f} | {s['reason'][:60]}")
            else:
                log.info(f"  {symbol} {direction.upper()}: score={s['score']}/100 "
                         f"— {s.get('reason','below threshold')[:50]}")

    # Sort by score — best first
    best_setups.sort(key=lambda x: x[2]["score"], reverse=True)

    # Take trades — prioritize higher tiers
    taken = 0
    for symbol, price, s, candles in best_setups:
        if symbol in state["open_positions"]:
            continue
        if len(state["open_positions"]) >= SAFETY["max_open_positions"]:
            break
        if state["trades_today"] >= SAFETY["max_trades_per_day"]:
            break

        # Minimum R:R check
        if s["rr"] < SAFETY["min_rr_ratio"] - 0.01:  # allow exactly at minimum
            log.info(f"  {symbol}: SKIP — R:R {s['rr']:.1f} < {SAFETY['min_rr_ratio']}")
            continue

        open_position(symbol, s, price)
        taken += 1

    if taken == 0 and not best_setups:
        log.info("No qualifying setups this scan")

# ══════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════
def build_dashboard() -> str:
    bal     = state["account_balance"]
    pnl     = state["total_pnl"]
    wins    = state["wins"]
    losses  = state["losses"]
    tot     = wins + losses
    wr      = wins/tot*100 if tot>0 else 0
    dd      = (state["peak_balance"]-bal)/state["peak_balance"]*100
    cb      = state["crypto_balance"]
    cob     = state["commodity_balance"]
    cp      = state["crypto_pnl"]
    cop     = state["commodity_pnl"]
    mode    = "📄 PAPER" if SAFETY["paper_mode"] else "💰 LIVE"
    now     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open_pos= state["open_positions"]
    recent  = list(reversed(state["trade_log"][-20:]))

    pnl_col = "#00d17a" if pnl >= 0 else "#ff4757"
    cp_col  = "#00d17a" if cp  >= 0 else "#ff4757"
    cop_col = "#00d17a" if cop >= 0 else "#ff4757"

    pos_rows = ""
    for sym, pos in open_pos.items():
        sc = "#00d17a" if pos["side"]=="long" else "#ff4757"
        pnl_unr = state["last_prices"].get(sym,pos["entry"])
        unr = (pnl_unr-pos["entry"])*pos["size"] if pos["side"]=="long" \
              else (pos["entry"]-pnl_unr)*pos["size"]
        unr_col = "#00d17a" if unr>=0 else "#ff4757"
        pos_rows += f"""<tr>
          <td>{ASSETS[sym]['color']} {sym}</td>
          <td style="color:{sc};font-weight:700">{pos['side'].upper()}</td>
          <td>${pos['entry']:,.4f}</td>
          <td>${pos['stop']:,.4f}</td>
          <td>${pos['target']:,.4f}</td>
          <td>{pos['score']}/100</td>
          <td>{pos['tier']}</td>
          <td style="color:{unr_col}">${unr:+.2f}</td>
        </tr>"""
    if not pos_rows:
        pos_rows = '<tr><td colspan="8" style="text-align:center;color:#475569;padding:20px">No open positions</td></tr>'

    trade_rows = ""
    for t in recent:
        pc = "#00d17a" if t["pnl"]>=0 else "#ff4757"
        ic = "✅" if t["result"]=="WIN" else "❌"
        ts = t["closed_at"][:16].replace("T"," ")
        ny = "⚡NY" if t.get("ny_trade") else ""
        trade_rows += f"""<tr>
          <td>{ic}</td>
          <td>{t['symbol']}</td>
          <td style="color:{'#00d17a' if t['side']=='long' else '#ff4757'}">{t['side'].upper()}</td>
          <td>${t['entry']:,.3f}</td><td>${t['exit']:,.3f}</td>
          <td style="color:{pc}">${t['pnl']:+.2f}</td>
          <td>{t['score']}/100</td>
          <td>{t['tier']} {ny}</td>
          <td>{ts}</td>
        </tr>"""
    if not trade_rows:
        trade_rows = '<tr><td colspan="9" style="text-align:center;color:#475569;padding:20px">No trades yet</td></tr>'

    # NY ranges
    ny_rows = ""
    for sym, rng in state["ny_session_range"].items():
        if rng.get("set"):
            ny_rows += f"<span style='margin:4px;padding:4px 10px;background:#1a2130;border-radius:4px;font-size:11px'>{ASSETS[sym]['color']} {sym}: {rng['low']:.3f}–{rng['high']:.3f}</span>"

    macro = state.get("macro_context", {})
    macro_html = " ".join(
        f'<span style="background:{"#00d17a22" if v in ("dovish","falling","rising") else "#ff475722"};'
        f'color:{"#00d17a" if v in ("dovish","falling","rising") else "#ff4757"};'
        f'border:1px solid #333;padding:3px 10px;border-radius:20px;font-size:11px">'
        f'{k.replace("_"," ").title()}: {v}</span>'
        for k,v in macro.items() if k != "signal_strength"
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="15">
<title>Meridian v3</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#08090b;color:#e2e8f0;font-family:'IBM Plex Mono',monospace;font-size:12px}}
.hdr{{background:#0e1117;border-bottom:1px solid rgba(255,255,255,.07);padding:12px 20px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-size:18px;font-weight:700;color:#ff6b00}}
.badge{{background:rgba(255,184,0,.1);border:1px solid rgba(255,184,0,.3);color:#ffb800;padding:3px 10px;border-radius:3px;font-size:10px;font-weight:700}}
.dot{{width:7px;height:7px;border-radius:50%;background:#00d17a;animation:pulse 2s infinite;display:inline-block;margin-right:5px}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.grid5{{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:rgba(255,255,255,.05)}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:rgba(255,255,255,.05);border-bottom:1px solid rgba(255,255,255,.07)}}
.stat{{background:#0e1117;padding:12px 16px}}
.sl{{font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}}
.sv{{font-size:18px;font-weight:700}}
.body{{padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.card{{background:#0e1117;border:1px solid rgba(255,255,255,.07);border-radius:6px;overflow:hidden}}
.ch{{padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.07);font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em}}
table{{width:100%;border-collapse:collapse}}
th{{padding:6px 10px;text-align:left;color:#475569;font-size:9px;text-transform:uppercase;background:#141920;border-bottom:1px solid rgba(255,255,255,.07)}}
td{{padding:6px 10px;border-bottom:1px solid rgba(255,255,255,.04);color:#94a3b8}}
tr:hover td{{background:#141920}}
.macro{{padding:10px 14px}}
.ny{{padding:8px 14px;font-size:11px}}
.tier1{{color:#64748b}}.tier2{{color:#ffb800}}.tier3{{color:#00d17a}}
@media(max-width:768px){{.grid5{{grid-template-columns:repeat(2,1fr)}}.body{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="hdr">
  <div style="display:flex;align-items:center;gap:10px">
    <div class="logo">⟡ Meridian v3</div>
    <div class="badge">{mode}</div>
  </div>
  <div style="color:#475569;font-size:10px"><span class="dot"></span>Live · {now} · refreshes 15s</div>
</div>
<div class="grid5">
  <div class="stat"><div class="sl">Total Balance</div><div class="sv" style="color:{'#00d17a' if bal>=500 else '#ff4757'}">${bal:,.2f}</div></div>
  <div class="stat"><div class="sl">Total P&L</div><div class="sv" style="color:{pnl_col}">${pnl:+.2f}</div></div>
  <div class="stat"><div class="sl">Win Rate</div><div class="sv" style="color:#3b8bff">{wr:.1f}%<span style="font-size:11px;color:#475569"> {wins}W/{losses}L</span></div></div>
  <div class="stat"><div class="sl">Drawdown</div><div class="sv" style="color:{'#ff4757' if dd>10 else '#ffb800'}">{dd:.1f}%</div></div>
  <div class="stat"><div class="sl">Today</div><div class="sv">{state['trades_today']}<span style="font-size:11px;color:#475569">/{SAFETY['max_trades_per_day']} trades</span></div></div>
</div>
<div class="grid2">
  <div class="stat"><div class="sl">🔵 Crypto Portfolio</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
      <span class="sv" style="color:{'#00d17a' if cb>=CRYPTO_START else '#ff4757'}">${cb:,.2f}</span>
      <span style="color:{cp_col}">${cp:+.2f}</span>
      <span style="font-size:10px;color:#475569">started ${CRYPTO_START:.0f}</span>
    </div>
  </div>
  <div class="stat"><div class="sl">🟡 Commodity Portfolio</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
      <span class="sv" style="color:{'#00d17a' if cob>=COMMODITY_START else '#ff4757'}">${cob:,.2f}</span>
      <span style="color:{cop_col}">${cop:+.2f}</span>
      <span style="font-size:10px;color:#475569">started ${COMMODITY_START:.0f}</span>
    </div>
  </div>
</div>
<div class="body">
  <div class="card">
    <div class="ch">Open Positions ({len(open_pos)})</div>
    <table><thead><tr><th>Asset</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>Score</th><th>Tier</th><th>Unreal P&L</th></tr></thead>
    <tbody>{pos_rows}</tbody></table>
  </div>
  <div class="card">
    <div class="ch">NY Session Ranges</div>
    <div class="ny">{ny_rows if ny_rows else '<span style="color:#475569">Session ranges update at market open</span>'}</div>
    <div class="ch" style="margin-top:8px">Macro Context</div>
    <div class="macro">{macro_html}</div>
  </div>
  <div class="card" style="grid-column:1/-1">
    <div class="ch">Recent Trades</div>
    <table><thead><tr><th></th><th>Asset</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Score</th><th>Tier</th><th>Closed</th></tr></thead>
    <tbody>{trade_rows}</tbody></table>
  </div>
</div>
<div style="padding:6px 20px;text-align:center;font-size:9px;color:#2d3748">
  Meridian v3.0 · {mode} · ${bal:,.2f} · Tier 1≥50 / Tier 2≥65 / Tier 3≥80
</div></body></html>"""

class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            html = build_dashboard()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())
    def log_message(self,*a): pass

# ══════════════════════════════════════════════════════════
# DAILY RESET & EMAIL
# ══════════════════════════════════════════════════════════
def daily_reset():
    today = datetime.date.today().isoformat()
    if state["last_reset"] == today:
        return
    state["trades_today"]    = 0
    state["daily_start_bal"] = state["account_balance"]
    state["last_reset"]      = today
    state["global_halt"]     = False
    state["halt_reason"]     = ""
    state["ny_session_range"] = {}
    log.info(f"── Daily reset: {today} | Balance: ${state['account_balance']:,.2f}")

def send_email():
    """Send daily summary email at 5pm CT."""
    now_ct = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
    if now_ct.hour != 22 or now_ct.minute > 15:
        return
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_PASS):
        return
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        wins = state["wins"]; losses = state["losses"]
        tot  = wins+losses
        wr   = wins/tot*100 if tot>0 else 0
        body = f"""
        <h2>Meridian v3 Daily Summary</h2>
        <p>Balance: <b>${state['account_balance']:,.2f}</b> | P&L: ${state['total_pnl']:+.2f}</p>
        <p>Win Rate: {wr:.1f}% ({wins}W/{losses}L) | Trades today: {state['trades_today']}</p>
        <p>Crypto: ${state['crypto_balance']:,.2f} ({state['crypto_pnl']:+.2f})</p>
        <p>Commodity: ${state['commodity_balance']:,.2f} ({state['commodity_pnl']:+.2f})</p>
        <p>Macro: {state.get('macro_context',{})}</p>
        """
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Meridian v3 — ${state['account_balance']:,.2f} | {datetime.date.today()}"
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body,"html"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.send_message(msg)
        log.info("Daily email sent")
    except Exception as e:
        log.warning(f"Email failed: {e}")

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def main():
    log.info("="*60)
    log.info("  MERIDIAN TRADING BOT v3.0")
    log.info(f"  Mode: {'PAPER (simulation)' if SAFETY['paper_mode'] else 'LIVE'}")
    log.info(f"  Assets: {', '.join(ASSETS[s]['label'] for s in ASSETS)}")
    log.info(f"  Tiers: T1≥50 (3% risk) | T2≥65 (6%) | T3≥80 (12%)")
    log.info(f"  Targets: {SAFETY['target_trades_per_day']} trades/day")
    log.info(f"  Exits: Partial TP at 1.5R | BE at 1R | Trail remainder")
    log.info(f"  Crypto:    ${CRYPTO_START:.0f} | Commodity: ${COMMODITY_START:.0f}")
    log.info("="*60)

    # Dashboard
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0",PORT),DashHandler).serve_forever(),
        daemon=True
    ).start()
    log.info(f"📊 Dashboard: http://0.0.0.0:{PORT}")

    # Prime Gold/Silver price cache at startup
    log.info("Priming Gold/Silver price cache...")
    for sym in ("XAU-USD","XAG-USD"):
        p = get_price(sym)
        if p:
            state["last_prices"][sym] = p
            log.info(f"  {ASSETS[sym]['color']} {sym}: ${p:,.4f} (primed)")
        else:
            log.warning(f"  {sym}: Could not prime price cache")

    # Main loop
    while True:
        try:
            daily_reset()
            scan()
            send_email()
            log.info(f"  Balance: ${state['account_balance']:,.2f} | "
                     f"P&L: ${state['total_pnl']:+.2f} | "
                     f"Trades today: {state['trades_today']}")
            log.info(f"  Sleeping {SAFETY['check_interval_secs']//60}min...\n")
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
        time.sleep(SAFETY["check_interval_secs"])

if __name__ == "__main__":
    main()
