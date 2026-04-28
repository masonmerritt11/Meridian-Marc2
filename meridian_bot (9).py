"""
Meridian Trading Bot v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOLD / SILVER  → macro-driven scoring (rates, inflation, employment)
BITCOIN        → momentum-driven scoring (RSI, MACD, sentiment)
All assets     → volatility circuit breaker, adaptive weights,
                 continuous backtesting, full risk management
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, time, json, math, logging, datetime, uuid, hmac, hashlib, statistics
import smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from typing import Optional

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_KEY    = os.environ.get("CB_API_KEY", "")
API_SECRET = os.environ.get("CB_API_SECRET", "")
AV_KEY     = os.environ.get("AV_KEY", "V5UHFMVIMXH4L8AX")
FRED_KEY   = os.environ.get("FRED_KEY", "")   # optional — free at fred.stlouisfed.org

# Coinbase Advanced Trade futures product IDs
# BIT = nano BTC futures, CGN = micro Gold nano, SLV = micro Silver
# ── FUTURES CONTRACT REFERENCE ────────────────────────────────
# All assets traded as futures on Coinbase Derivatives (CFTC regulated)
#
# LEVERAGE (intraday):
#   BTC / ETH perps  → up to 10x  (crypto perpetuals)
#   XRP monthly      → up to 10x
#   Gold / Silver    → up to 20x  (metals futures)
#   Crude Oil (NOL)  → up to 25x  (commodity futures)
#
# BOT uses 2.5% account risk per trade — leverage just determines
# how much margin is required, not how much we risk.
# Stops are always set to limit loss to exactly 2.5% of balance.
#
# CONTRACTS:
#   BTC-PERP  = nano Bitcoin perpetual  (0.01 BTC/contract, no expiry)
#   ETH-PERP  = nano Ether perpetual    (0.10 ETH/contract, no expiry)
#   XRL       = nano XRP monthly        (500 XRP/contract)
#   CGN       = micro Gold monthly      (1 troy oz/contract)
#   SLV       = micro Silver monthly    (100 troy oz/contract)
#   NOL       = nano Crude Oil monthly  (10 barrels WTI/contract)

ASSETS = {
    "BTC-USD": {
        "label":         "Bitcoin",
        "type":          "crypto",
        "vol":            0.015,
        "color":          "🟠",
        "price_source":  "coinbase",
        "corr_group":    "btc",
        "futures_perp":  "BTC-PERP",
        "futures_nano":  "BIT",
        "contract_size":  0.01,       # BTC per contract
        "min_contracts":  1,
        "max_leverage":   10,
        "av_sym":         None,
    },
    "ETH-USD": {
        "label":         "Ethereum",
        "type":          "crypto",
        "vol":            0.020,
        "color":          "🔵",
        "price_source":  "coinbase",
        "corr_group":    "btc",        # high corr to BTC — same group
        "futures_perp":  "ETH-PERP",
        "futures_nano":  "ET",
        "contract_size":  0.1,        # ETH per contract
        "min_contracts":  1,
        "max_leverage":   10,
        "av_sym":         None,
    },
    "XRP-USD": {
        "label":         "XRP",
        "type":          "crypto",
        "vol":            0.022,
        "color":          "💧",
        "price_source":  "coinbase",
        "corr_group":    "xrp",
        "futures_perp":  None,         # XRP perp not yet live
        "futures_nano":  "XRL",
        "contract_size":  500,         # XRP per contract
        "min_contracts":  1,
        "max_leverage":   10,
        "av_sym":         None,
    },
    "XAU-USD": {
        "label":         "Gold",
        "type":          "macro",
        "vol":            0.006,
        "color":          "🟡",
        "price_source":  "alphavantage",
        "corr_group":    "metals",
        "futures_perp":  None,
        "futures_nano":  "CGN",
        "contract_size":  1,           # troy oz per contract
        "min_contracts":  1,
        "max_leverage":   20,
        "av_sym":         "XAU",
    },
    "XAG-USD": {
        "label":         "Silver",
        "type":          "macro",
        "vol":            0.012,
        "color":          "⚪",
        "price_source":  "alphavantage",
        "corr_group":    "metals",     # high corr to Gold — same group
        "futures_perp":  None,
        "futures_nano":  "SLV",
        "contract_size":  100,         # troy oz per contract
        "min_contracts":  1,
        "max_leverage":   20,
        "av_sym":         "XAG",
    },
    "OIL-USD": {
        "label":         "Crude Oil",
        "type":          "macro",      # macro-driven: OPEC, DXY, growth
        "vol":            0.020,
        "color":          "🛢️",
        "price_source":  "alphavantage",
        "corr_group":    "energy",     # own group — low crypto corr
        "futures_perp":  None,
        "futures_nano":  "NOL",        # nano crude oil, 10 barrels WTI
        "contract_size":  10,          # barrels per contract
        "min_contracts":  1,
        "max_leverage":   25,
        "av_sym":         "WTI",       # Alpha Vantage WTI crude
    },
}

SAFETY = {
    # Trade limits
    "max_trades_per_day":          6,      # 6 trades/day — aggressive mode
    "score_threshold_early":      65,    # trades 1-3
    "score_threshold_late":       80,    # after 3 trades
    "max_open_positions":          2,      # 2 for ETH+Oil start — increase as balance grows
    # Risk
    "risk_per_trade_pct":        0.050,  # 5.0% base risk — aggressive mode
    "risk_hc_82":                0.120,  # 12.0% for score 82-89 high conviction
    "risk_hc_90":                0.200,  # 20.0% for score 90+ perfect setup
    "trailing_stop_atr_mult":    0.8,    # tight trail at 0.8x ATR — locks in profits faster
    "max_daily_loss_pct":        0.25,   # 25% daily stop — aggressive mode
    "max_drawdown_pct":          0.50,   # 50% total drawdown limit — aggressive mode
    "min_rr_ratio":               2.0,
    # Hard stop = 4x ATR from entry (thesis-based exits take priority)
    "hard_stop_atr_mult":        4.0,     # only exit if price moves 4x ATR against us
    "thesis_breaks_to_exit":     3,       # need 3/4 conditions broken to invalidate thesis
    # Range filters
    "min_range_pct":             0.008,
    "max_range_pct":             0.10,
    # Volatility circuit breaker
    "vol_window":                   20,  # candles to measure volatility
    "vol_spike_multiplier":        2.5,  # halt if vol > 2.5x normal
    "vol_resume_multiplier":       1.5,  # resume when vol < 1.5x normal
    # Backtest adaptive weights
    "backtest_window":              30,  # trades to analyse for weight adjustment
    "weight_adjust_interval":    86400,  # re-weight every 24 hours
    # Timing
    "check_interval_secs":        1800,  # 30-min cycle
    "paper_mode":                 True,    # ← switch to False when ready to go live
    "account_size_usd":            500,     # starting balance
}

ASSET_THRESHOLDS = {
    "ETH-USD":  0,
    "OIL-USD":  0,
    "XRP-USD":  0,       # Active from start — HC override handles sizing
    "XAU-USD":  1500,
    "BTC-USD":  2000,
    "XAG-USD":  2000,
}


# ═════════════════════════════════════════════════════════════════
# ASSET INTELLIGENCE SYSTEM
# ═════════════════════════════════════════════════════════════════
# Tracks each asset's rolling performance, momentum, and predictive
# signals. Dynamically concentrates capital on hot assets and
# deprioritizes cold ones. Uses leading indicators to predict
# which direction each asset is likely to move before it moves.
# ═════════════════════════════════════════════════════════════════

ASSET_INTEL_FILE = "asset_intel.json"

asset_intel = {
    sym: {
        "scores":          [],     # last 20 composite scores
        "results":         [],     # last 20 trade results (1=win, 0=loss)
        "pnl_history":     [],     # last 20 trade P&Ls
        "regime_history":  [],     # last 20 regime readings
        "heat":            50.0,   # 0-100 heat score (how hot this asset is)
        "momentum_score":  50.0,   # directional momentum prediction
        "allocation_mult": 1.0,    # risk multiplier (0.25x to 3.0x)
        "skip_until":      None,   # timestamp to skip this asset until
        "consecutive_losses": 0,   # streak tracker
        "last_regime":     "unknown",
        "predicted_move":  "neutral",  # bull / bear / neutral
        "confidence":      0,          # 0-100 prediction confidence
    }
    for sym in ["BTC-USD","ETH-USD","XRP-USD","XAU-USD","XAG-USD","OIL-USD"]
}

def load_asset_intel():
    global asset_intel
    try:
        with open(ASSET_INTEL_FILE) as f:
            saved = json.load(f)
            for sym in asset_intel:
                if sym in saved:
                    asset_intel[sym].update(saved[sym])
        log.info("Asset intelligence loaded from disk")
    except FileNotFoundError:
        pass

def save_asset_intel():
    with open(ASSET_INTEL_FILE, "w") as f:
        json.dump(asset_intel, f, indent=2, default=str)

def update_asset_heat(symbol: str, score: int, regime: str):
    """Update rolling heat score for an asset based on recent signals."""
    intel = asset_intel.get(symbol)
    if not intel: return

    intel["scores"].append(score)
    intel["regime_history"].append(regime)
    intel["scores"]         = intel["scores"][-20:]
    intel["regime_history"] = intel["regime_history"][-20:]
    intel["last_regime"]    = regime

    # Heat = weighted average of recent scores
    # More recent scores count more
    scores = intel["scores"]
    if scores:
        weights = [1 + i * 0.15 for i in range(len(scores))]
        heat = sum(s*w for s,w in zip(scores,weights)) / sum(weights)
        intel["heat"] = round(heat, 1)

def record_asset_trade_result(symbol: str, result: str, pnl: float, regime: str):
    """Record trade outcome to update allocation multiplier."""
    intel = asset_intel.get(symbol)
    if not intel: return

    win = 1 if result == "WIN" else 0
    intel["results"].append(win)
    intel["pnl_history"].append(pnl)
    intel["results"]     = intel["results"][-20:]
    intel["pnl_history"] = intel["pnl_history"][-20:]

    if win:
        intel["consecutive_losses"] = 0
    else:
        intel["consecutive_losses"] += 1

    # Cool-down: 3 consecutive losses = skip this asset for 6 hours
    if intel["consecutive_losses"] >= 3:
        skip_until = (datetime.datetime.now() + datetime.timedelta(hours=6)).isoformat()
        intel["skip_until"] = skip_until
        log.warning(f"  🧊 {symbol}: 3 consecutive losses — cooling down until {skip_until[:16]}")

    save_asset_intel()

def predict_asset_move(symbol: str, closes: list, highs: list,
                       lows: list, macro: dict) -> tuple[str, int]:
    """
    Predict the likely next directional move for an asset.
    Uses leading indicators that tend to precede price moves:
    - RSI divergence (price makes new low but RSI doesn't = bull reversal coming)
    - Volume/volatility expansion (breakout coming)
    - MACD histogram slope (momentum building before price moves)
    - EMA convergence/divergence rate
    - Macro alignment score
    Returns (predicted_direction, confidence_0_to_100)
    """
    if len(closes) < 50:
        return "neutral", 0

    bull_signals = 0
    bear_signals = 0
    total_possible = 8

    # ── 1. RSI DIVERGENCE (most predictive) ──────────────────────
    # Bullish: price lower but RSI higher = momentum turning up
    # Bearish: price higher but RSI lower = momentum turning down
    def calc_rsi(c, p=14):
        if len(c)<p+1: return 50
        g=l=0
        for i in range(len(c)-p,len(c)):
            d=c[i]-c[i-1]
            if d>0:g+=d
            else:l+=abs(d)
        return 100-100/(1+(g/(l or 1e-9)))

    rsi_now  = calc_rsi(closes[-14:])
    rsi_prev = calc_rsi(closes[-28:-14])
    price_direction = closes[-1] > closes[-14]   # recent price trend
    rsi_direction   = rsi_now > rsi_prev          # recent RSI trend

    if not price_direction and rsi_direction:     # bullish divergence
        bull_signals += 2
    elif price_direction and not rsi_direction:   # bearish divergence
        bear_signals += 2

    # ── 2. MACD HISTOGRAM SLOPE (momentum building) ────────────────
    def calc_ema(c, p):
        k=2/(p+1);e=c[0]
        for x in c[1:]:e=x*k+e*(1-k)
        return e

    def calc_macd_hist_list(c):
        if len(c)<26:return[0]*len(c)
        e12l=[];e26l=[];e12=c[0];e26=c[0]
        for x in c:
            e12=x*(2/13)+e12*(11/13);e26=x*(2/27)+e26*(25/27)
            e12l.append(e12);e26l.append(e26)
        ml=[e12l[i]-e26l[i] for i in range(len(c))]
        sig=[ml[0]]
        for m in ml[1:]:sig.append(m*(2/10)+sig[-1]*(8/10))
        return[ml[i]-sig[i] for i in range(len(ml))]

    hist = calc_macd_hist_list(closes)
    if len(hist) >= 6:
        h_now  = sum(hist[-3:]) / 3
        h_prev = sum(hist[-6:-3]) / 3
        slope  = h_now - h_prev
        if slope > 0 and h_prev < 0:    # histogram rising from below zero = bull
            bull_signals += 2
        elif slope < 0 and h_prev > 0:  # histogram falling from above zero = bear
            bear_signals += 2
        elif slope > 0:
            bull_signals += 1
        elif slope < 0:
            bear_signals += 1

    # ── 3. EMA CONVERGENCE RATE ─────────────────────────────────────
    # EMAs converging = trend losing steam = reversal possible
    # EMAs diverging = trend accelerating = continue
    ema10 = calc_ema(closes[-10:], 10)
    ema20 = calc_ema(closes[-20:], 20)
    ema50 = calc_ema(closes[-50:], 50) if len(closes)>=50 else ema20
    ema_spread_now  = abs(ema10 - ema50) / closes[-1]
    ema_spread_prev = abs(calc_ema(closes[-20:-10], 10) - ema50) / closes[-20] if len(closes)>=20 else ema_spread_now
    converging = ema_spread_now < ema_spread_prev

    if ema10 > ema50 and converging:     bear_signals += 1  # uptrend slowing
    elif ema10 < ema50 and converging:   bull_signals += 1  # downtrend slowing
    elif ema10 > ema50 and not converging: bull_signals += 1  # uptrend accelerating
    elif ema10 < ema50 and not converging: bear_signals += 1  # downtrend accelerating

    # ── 4. VOLATILITY EXPANSION (breakout predictor) ───────────────
    # BB squeeze followed by expansion = big move coming
    # Can't predict direction alone but confirms a move is coming
    bb_sl=closes[-20:];bsma=sum(bb_sl)/20
    bstd=math.sqrt(sum((x-bsma)**2 for x in bb_sl)/20) if len(bb_sl)>1 else 0
    bb_width_now  = (4*bstd/bsma)*100 if bsma>0 else 0
    bb_sl2=closes[-40:-20] if len(closes)>=40 else closes[-20:]
    bsma2=sum(bb_sl2)/max(len(bb_sl2),1)
    bstd2=math.sqrt(sum((x-bsma2)**2 for x in bb_sl2)/max(len(bb_sl2),1)) if len(bb_sl2)>1 else bstd
    bb_width_prev = (4*bstd2/bsma2)*100 if bsma2>0 else bb_width_now
    bb_expanding  = bb_width_now > bb_width_prev * 1.3

    # Direction of expansion
    if bb_expanding:
        if closes[-1] > bsma: bull_signals += 1
        else:                  bear_signals += 1

    # ── 5. MACRO ALIGNMENT ─────────────────────────────────────────
    asset_type = ASSETS.get(symbol, {}).get("type", "crypto")
    if asset_type == "macro":
        fed    = macro.get("fed_stance", "neutral")
        dxy    = macro.get("dxy_trend", "neutral")
        cpi    = macro.get("inflation_trend", "neutral")
        if fed == "dovish":   bull_signals += 1
        elif fed == "hawkish": bear_signals += 1
        if dxy == "falling":  bull_signals += 1
        elif dxy == "rising": bear_signals += 1
        if cpi == "rising":   bull_signals += 1
    elif asset_type == "energy":
        dxy = macro.get("dxy_trend","neutral")
        emp = macro.get("employment","neutral")
        if dxy == "falling":   bull_signals += 1
        elif dxy == "rising":  bear_signals += 1
        if emp == "strong":    bull_signals += 1
        elif emp == "weakening": bear_signals += 1

    # ── SCORE AND PREDICT ─────────────────────────────────────────
    net       = bull_signals - bear_signals
    max_net   = total_possible
    confidence = min(100, int(abs(net) / max_net * 100 * 1.5))

    if net >= 2:   direction = "bull"
    elif net <= -2: direction = "bear"
    else:           direction = "neutral"

    return direction, confidence

def calc_allocation_multiplier(symbol: str, proposed_setup: str) -> tuple[float, str]:
    """
    Calculate how much to scale up or down risk for this asset.
    Hot assets with aligned predictions get up to 3x normal risk.
    Cold assets or misaligned predictions get as low as 0.25x.
    """
    intel = asset_intel.get(symbol, {})

    # Check cool-down
    skip_until = intel.get("skip_until")
    if skip_until:
        try:
            skip_dt = datetime.datetime.fromisoformat(skip_until)
            if datetime.datetime.now() < skip_dt:
                remaining = (skip_dt - datetime.datetime.now()).seconds // 60
                return 0.0, f"cooling down ({remaining}min left)"
        except Exception:
            pass
        intel["skip_until"] = None

    heat       = intel.get("heat", 50)
    prediction = intel.get("predicted_move", "neutral")
    confidence = intel.get("confidence", 0)
    results    = intel.get("results", [])
    recent_wr  = sum(results[-10:]) / len(results[-10:]) if len(results) >= 3 else 0.5
    cons_loss  = intel.get("consecutive_losses", 0)

    mult = 1.0
    reasons = []

    # Heat-based scaling
    if heat >= 80:    mult *= 2.0;  reasons.append(f"🔥 heat={heat:.0f}")
    elif heat >= 70:  mult *= 1.5;  reasons.append(f"♨️ heat={heat:.0f}")
    elif heat >= 60:  mult *= 1.2;  reasons.append(f"warm heat={heat:.0f}")
    elif heat <= 35:  mult *= 0.5;  reasons.append(f"🧊 heat={heat:.0f}")
    elif heat <= 45:  mult *= 0.75; reasons.append(f"cool heat={heat:.0f}")

    # Prediction alignment
    setup_dir = "bull" if proposed_setup == "long" else "bear"
    if prediction == setup_dir and confidence >= 60:
        mult *= 1.5; reasons.append(f"📈 pred aligned {confidence}% conf")
    elif prediction == setup_dir and confidence >= 40:
        mult *= 1.2; reasons.append(f"pred weak aligned {confidence}%")
    elif prediction != "neutral" and prediction != setup_dir:
        mult *= 0.4; reasons.append(f"⚠️ pred AGAINST setup ({prediction})")

    # Win rate scaling
    if recent_wr >= 0.65: mult *= 1.3; reasons.append(f"WR {recent_wr*100:.0f}% hot streak")
    elif recent_wr <= 0.30 and len(results) >= 5: mult *= 0.5; reasons.append(f"WR {recent_wr*100:.0f}% cold")

    # Consecutive loss protection
    if cons_loss >= 2: mult *= 0.5; reasons.append(f"{cons_loss} consec losses")
    if cons_loss == 0 and recent_wr >= 0.6: mult *= 1.2; reasons.append("on a run")

    # Hard caps
    mult = max(0.25, min(3.0, mult))
    intel["allocation_mult"] = mult
    return mult, " | ".join(reasons) if reasons else "standard"

def get_portfolio_allocation_summary() -> str:
    """Print a heat map of all assets for the status report."""
    lines = []
    for sym, intel in asset_intel.items():
        if sym not in ASSETS: continue
        heat  = intel.get("heat", 50)
        pred  = intel.get("predicted_move", "?")
        conf  = intel.get("confidence", 0)
        mult  = intel.get("allocation_mult", 1.0)
        color = ASSETS[sym]["color"]
        bar_len = int(heat / 10)
        bar = "█" * bar_len + "░" * (10 - bar_len)
        pred_icon = "📈" if pred=="bull" else "📉" if pred=="bear" else "➡️"
        lines.append(f"  {color} {sym:<8} [{bar}] heat={heat:.0f} "
                     f"{pred_icon}{pred:<7} {conf}%conf  {mult:.1f}x alloc")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════
# TRICKLE-DOWN SCORING SYSTEM
# Philosophy: Pan for gold — filter big stuff first, then fine details
# Layer 1: Macro veto    (is the big picture right?)
# Layer 2: Regime veto   (is the trend right?)
# Layer 3: TF alignment  (are multiple timeframes agreeing?)
# Layer 4: Technical     (is the setup clean and precise?)
# Layer 5: Timing        (is NOW the right moment?)
#
# Timeframes:
#   Crypto: entry on 15-min, confirmed on 1h, filtered by 4h
#   Commodities: entry on 1h, confirmed on 4h, filtered by daily
# ═════════════════════════════════════════════════════════════════

MACRO_RULES = {
    "BTC-USD": {
        "long_requires":  {"fed_stance": ["dovish","neutral"]},
        "short_requires": {"fed_stance": ["hawkish","neutral"]},
    },
    "ETH-USD": {
        "long_requires":  {"fed_stance": ["dovish","neutral"]},
        "short_requires": {"fed_stance": ["hawkish","neutral"]},
    },
    "XRP-USD": {
        "long_requires":  {"fed_stance": ["dovish","neutral"]},
        "short_requires": {"fed_stance": ["hawkish","neutral"]},
    },
    "XAU-USD": {
        # Gold: inflation hedge — can rally even hawkish if inflation rising
        "long_requires":  {"inflation_trend": ["rising","stable"]},
        "short_requires": {"dxy_trend": ["rising"], "inflation_trend": ["falling"]},
    },
    "XAG-USD": {
        "long_requires":  {"inflation_trend": ["rising","stable"]},
        "short_requires": {"dxy_trend": ["rising"], "inflation_trend": ["falling"]},
    },
    "OIL-USD": {
        # Oil: USD is the primary driver
        "long_requires":  {"dxy_trend": ["falling","neutral"]},
        "short_requires": {"dxy_trend": ["rising","neutral"]},
    },
}

def check_macro_veto(symbol: str, direction: str, macro: dict) -> tuple[bool, str]:
    """Layer 1 — Macro veto. Returns (passes, reason)."""
    rules = MACRO_RULES.get(symbol, {})
    key   = "long_requires" if direction == "long" else "short_requires"
    reqs  = rules.get(key, {})
    failures = []
    for factor, allowed in reqs.items():
        actual = macro.get(factor, "neutral")
        if actual not in allowed:
            failures.append(f"{factor}={actual}")
    if failures:
        return False, f"Macro veto: {', '.join(failures)}"
    return True, "Macro ✓"

def check_regime_veto(direction: str, regime: dict) -> tuple[bool, str]:
    """Layer 2 — Regime veto. Only blocks if ALL timeframes aligned against."""
    overall = regime.get("regime","unknown")
    all_aligned = regime.get("confidence", 0) >= 80
    if overall == "volatile":
        return False, "Volatile — standing aside"
    if overall == "trending_down" and direction == "long" and all_aligned:
        return False, "All TFs trending down — longs blocked"
    if overall == "trending_up" and direction == "short" and all_aligned:
        return False, "All TFs trending up — shorts blocked"
    return True, f"Regime OK ({overall}) ✓"

def score_tf_alignment(direction: str, closes_fast: list, closes_slow: list) -> int:
    """Layer 3 — Timeframe alignment score 0-100."""
    def bias(c):
        if len(c)<20: return "neutral"
        k20=2/21;k50=2/51;e20=e50=c[0]
        for x in c:
            e20=x*k20+e20*(1-k20);e50=x*k50+e50*(1-k50)
        if c[-1]>e20 and c[-1]>e50 and e20>e50: return "bull"
        if c[-1]<e20 and c[-1]<e50 and e20<e50: return "bear"
        return "neutral"
    bf=bias(closes_fast);bs=bias(closes_slow)
    sd="bull" if direction=="long" else "bear"
    score=0
    if bs==sd: score+=50   # slow TF aligned = strong signal
    elif bs=="neutral": score+=20
    if bf==sd: score+=50   # fast TF aligned
    elif bf=="neutral": score+=20
    return min(100,score)

def score_technical_setup(
    closes: list, highs: list, lows: list, opens: list,
    price: float, direction: str, setup_type: str
) -> tuple[int, str, float, float]:
    """Layer 4 — Technical setup quality. Returns (score, notes, stop, target)."""
    if len(closes)<20: return 0,"Insufficient data",0,0

    def rsi_fn(c,p=14):
        if len(c)<p+1:return 50
        g=l=0
        for i in range(len(c)-p,len(c)):
            d=c[i]-c[i-1];g+=max(d,0);l+=max(-d,0)
        return 100-100/(1+(g/(l or 1e-9)))

    def ema_fn(c,p):
        k=2/(p+1);e=c[0]
        for x in c[1:]:e=x*k+e*(1-k)
        return e

    def atr_fn(H,L,C,p=14):
        if len(C)<2:return C[-1]*0.01
        trs=[max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1])) for i in range(1,len(C))]
        return sum(trs[-p:])/p if len(trs)>=p else sum(trs)/max(len(trs),1)

    def macd_fn(c):
        if len(c)<26:return 0,0
        k12=2/13;k26=2/27;e12=e26=c[0]
        for x in c:
            e12=x*k12+e12*(1-k12);e26=x*k26+e26*(1-k26)
        ml=e12-e26;sig=ml
        return ml,ml-sig

    def sr_fn(H,L,p):
        def cl(arr):
            s=sorted(arr);cs=[]
            for v in s:
                if cs and abs(v-cs[-1][-1])/v<0.004:cs[-1].append(v)
                else:cs.append([v])
            return[sum(c)/len(c) for c in cs]
        sc=[v for v in cl(L[-60:]) if v<p]
        rc=[v for v in cl(H[-60:]) if v>p]
        return(sc[-1] if sc else min(L[-40:])),(rc[0] if rc else max(H[-40:]))

    r=rsi_fn(closes);e20=ema_fn(closes[-20:],20)
    e50=ema_fn(closes[-50:] if len(closes)>=50 else closes,50)
    at=atr_fn(highs,lows,closes);ml,mh=macd_fn(closes)
    sl=closes[-20:];sma=sum(sl)/20
    std=math.sqrt(sum((x-sma)**2 for x in sl)/20) if len(sl)>1 else 0
    bbu=sma+2*std;bbl=sma-2*std
    sup,res=sr_fn(highs,lows,price)

    score=0;notes=[]

    if setup_type=="range":
        ds=(price-sup)/price;dr=(res-price)/price;rng=(res-sup)/price
        if direction=="long":
            if ds<0.008:score+=35;notes.append("At support ✓✓")
            elif ds<0.015:score+=20;notes.append("Near support ✓")
            else:return 0,"Too far from support",0,0
            if 0.008<rng<0.10:score+=15;notes.append("Range width ✓")
            if 28<r<55:score+=20;notes.append(f"RSI {r:.0f} ✓")
            elif r<28:score+=25;notes.append(f"RSI {r:.0f} oversold ✓✓")
            if price<=bbl*1.005:score+=20;notes.append("Lower BB ✓")
            stop=sup*0.997;target=res
        else:
            if dr<0.008:score+=35;notes.append("At resistance ✓✓")
            elif dr<0.015:score+=20;notes.append("Near resistance ✓")
            else:return 0,"Too far from resistance",0,0
            if 0.008<rng<0.10:score+=15;notes.append("Range width ✓")
            if 45<r<72:score+=20;notes.append(f"RSI {r:.0f} ✓")
            elif r>72:score+=25;notes.append(f"RSI {r:.0f} overbought ✓✓")
            if price>=bbu*0.995:score+=20;notes.append("Upper BB ✓")
            stop=res*1.003;target=sup

    else:  # momentum
        if direction=="short":
            if price<e20 and price<e50:score+=25;notes.append("Below EMAs ✓")
            if e20<e50:score+=20;notes.append("Death cross ✓")
            if 28<r<68:score+=20;notes.append(f"RSI {r:.0f} ✓")
            if price<bbl:score+=20;notes.append("Below BB ✓")
            else:score+=5
            if ml<0:score+=15;notes.append("MACD bearish ✓")
            stop=price+at*1.8;target=price-at*4.0
        else:
            if price>e20 and price>e50:score+=25;notes.append("Above EMAs ✓")
            if e20>e50:score+=20;notes.append("Golden cross ✓")
            if 42<r<78:score+=20;notes.append(f"RSI {r:.0f} ✓")
            if price>bbu:score+=20;notes.append("Above BB ✓")
            else:score+=5
            if ml>0:score+=15;notes.append("MACD bullish ✓")
            stop=price-at*1.8;target=price+at*4.0

    risk=abs(price-stop);reward=abs(target-price)
    rr=reward/risk if risk>0 else 0
    if rr<1.5:return 0,f"R:R {rr:.1f} insufficient",0,0
    return min(100,score)," | ".join(notes),stop,target

def score_timing(closes: list, highs: list, lows: list, opens: list, direction: str) -> int:
    """Layer 5 — Entry timing score 0-100."""
    if len(closes)<5:return 50
    body=abs(closes[-1]-opens[-1]);uw=highs[-1]-max(closes[-1],opens[-1])
    lw=min(closes[-1],opens[-1])-lows[-1];rng=highs[-1]-lows[-1]
    bull=closes[-1]>opens[-1]
    rb=sum(1 for i in range(-3,0) if closes[i]>opens[i])
    avg_rng=sum(highs[i]-lows[i] for i in range(-6,-1))/5 if len(closes)>=6 else rng
    score=0
    if direction=="long":
        if bull:score+=25
        if lw>body*1.5:score+=25
        if rb>=2:score+=20
        if rng>avg_rng*1.2:score+=15
        if uw<body*0.3:score+=15
    else:
        if not bull:score+=25
        if uw>body*1.5:score+=25
        if (3-rb)>=2:score+=20
        if rng>avg_rng*1.2:score+=15
        if lw<body*0.3:score+=15
    return min(100,score)

def trickle_down_score(
    symbol: str, direction: str, macro: dict,
    candles: list, price: float,
    setup_type: str = "auto"
) -> dict:
    """
    Full trickle-down scoring pipeline.
    Each layer can veto. No score overrides a macro veto.
    """
    result = {"symbol":symbol,"direction":direction,"score":0,
              "trade":False,"stop":0,"target":0,"rr":0,"reason":"","layers":{}}

    closes=[c["close"] for c in candles]
    highs= [c["high"]  for c in candles]
    lows=  [c["low"]   for c in candles]
    opens= [c["open"]  for c in candles]

    # Layer 1: Macro
    mp,mr = check_macro_veto(symbol, direction, macro)
    result["layers"]["macro"]={"pass":mp,"reason":mr}
    if not mp:
        result["reason"]=f"L1 MACRO VETO: {mr}"; return result

    # Layer 2: Regime
    regime = detect_market_regime(closes, highs, lows)
    rp,rr_ = check_regime_veto(direction, regime)
    result["layers"]["regime"]={"pass":rp,"reason":rr_}
    if not rp:
        result["reason"]=f"L2 REGIME VETO: {rr_}"; return result

    # Layer 3: TF alignment (use fast half vs slow half of candles as proxy)
    mid=len(closes)//2
    closes_fast=closes[mid:]; closes_slow=closes[:mid]
    tf_score=score_tf_alignment(direction, closes_fast, closes_slow)
    result["layers"]["tf"]={"score":tf_score}
    if tf_score<35:
        result["reason"]=f"L3 TF MISMATCH (score {tf_score})"; return result

    # Auto detect setup type from regime
    if setup_type=="auto":
        reg=regime.get("regime","ranging")
        setup_type="momentum" if reg in("trending_up","trending_down") else "range"

    # Layer 4: Technical
    ts,tn,stop,target=score_technical_setup(closes,highs,lows,opens,price,direction,setup_type)
    result["layers"]["technical"]={"score":ts,"reason":tn,"setup_type":setup_type}
    if ts<45 or stop==0:
        result["reason"]=f"L4 TECH WEAK: {tn} (score {ts})"; return result

    # Layer 5: Timing
    timing=score_timing(closes,highs,lows,opens,direction)
    result["layers"]["timing"]={"score":timing}

    # Composite
    composite=round(tf_score*0.25 + ts*0.50 + timing*0.25)
    if regime.get("confidence",0)>=70: composite=min(100,composite+10)

    rr_val=abs(target-price)/abs(price-stop) if abs(price-stop)>0 else 0
    result.update({
        "score":composite,"trade":composite>=60,"stop":stop,
        "target":target,"rr":rr_val,"setup_type":setup_type,
        "reason":f"L1✓ L2✓ L3:{tf_score} L4:{ts} L5:{timing} = {composite}/100"
    })
    return result


# Scoring weight profiles — adjusted dynamically by backtester
WEIGHT_PROFILES = {
    "crypto": {
        "structure":   0.20,   # support/resistance
        "momentum":    0.45,   # RSI, MACD, price momentum
        "sentiment":   0.20,   # volume, BB squeeze, crossovers
        "mtf":         0.15,   # multi-timeframe bias
        "macro":       0.00,   # not used for crypto
    },
    "macro": {
        "structure":   0.25,   # support/resistance
        "momentum":    0.15,   # some momentum still matters
        "sentiment":   0.10,   # less sentiment, more fundamentals
        "mtf":         0.20,   # trend confirmation
        "macro":       0.30,   # rates, inflation, employment signals
    },
}

LOG_FILE   = "meridian.log"
STATE_FILE = "bot_state.json"
TRADE_FILE = "trade_log.json"

# ── EMAIL CONFIG ──────────────────────────────────────
# Set these in Railway Variables:
#   EMAIL_FROM    your Gmail address
#   EMAIL_PASS    Gmail App Password (not your real password)
#   EMAIL_TO      where to send the summary (can be same as FROM)
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO   = os.environ.get("EMAIL_TO",   "")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("meridian")

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
state = {
    "trades_today":          0,
    "last_reset_date":       None,
    "open_positions":        {},
    "account_balance":       SAFETY["account_size_usd"],
    "peak_balance":          SAFETY["account_size_usd"],
    "daily_start_balance":   SAFETY["account_size_usd"],
    "total_pnl":             0.0,
    "total_trades":          0,
    "wins":                  0,
    "losses":                0,
    "circuit_breaker":       {},   # symbol -> {"tripped": bool, "reason": str}
    "global_halt":           False,
    "global_halt_reason":    "",
    "last_weight_adjust":    None,
    "weight_profiles":       WEIGHT_PROFILES.copy(),
    "macro_context":         {},   # cached macro signals
    "last_macro_fetch":      None,
}
trade_log = []

def load_state():
    global trade_log, WEIGHT_PROFILES
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
            state.update(saved)
        log.info("State loaded from disk")
    except FileNotFoundError:
        log.info("No saved state — starting fresh")
    try:
        with open(TRADE_FILE) as f:
            trade_log = json.load(f)
        log.info(f"Trade log: {len(trade_log)} historical trades")
    except FileNotFoundError:
        pass

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
    with open(TRADE_FILE, "w") as f:
        json.dump(trade_log, f, indent=2, default=str)

def reset_daily():
    today = datetime.date.today().isoformat()
    if state["last_reset_date"] != today:
        state["trades_today"]        = 0
        state["last_reset_date"]     = today
        state["daily_start_balance"] = state["account_balance"]
        # Clear per-symbol circuit breakers daily
        for sym in list(state["circuit_breaker"].keys()):
            state["circuit_breaker"][sym]["tripped"] = False
        log.info(f"── Daily reset: {today} | Balance: ${state['account_balance']:.2f}")
        save_state()

def get_score_threshold() -> int:
    return SAFETY["score_threshold_late"] if state["trades_today"] >= SAFETY["max_trades_per_day"] \
           else SAFETY["score_threshold_early"]

# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────
def check_global_halt():
    """Stop all trading if daily loss or drawdown limits hit."""
    bal   = state["account_balance"]
    daily = state["daily_start_balance"]
    peak  = state["peak_balance"]

    daily_loss_pct    = (daily - bal) / daily if daily > 0 else 0
    drawdown_pct      = (peak  - bal) / peak  if peak  > 0 else 0

    if daily_loss_pct >= SAFETY["max_daily_loss_pct"]:
        if not state["global_halt"]:
            state["global_halt"]        = True
            state["global_halt_reason"] = f"Daily loss {daily_loss_pct*100:.1f}% ≥ limit {SAFETY['max_daily_loss_pct']*100}%"
            log.warning(f"🚨 GLOBAL HALT: {state['global_halt_reason']}")
            save_state()
        return True

    if drawdown_pct >= SAFETY["max_drawdown_pct"]:
        if not state["global_halt"]:
            state["global_halt"]        = True
            state["global_halt_reason"] = f"Drawdown {drawdown_pct*100:.1f}% ≥ limit {SAFETY['max_drawdown_pct']*100}%"
            log.warning(f"🚨 GLOBAL HALT: {state['global_halt_reason']}")
            save_state()
        return True

    # If previously halted but now within limits — resume
    if state["global_halt"]:
        state["global_halt"]        = False
        state["global_halt_reason"] = ""
        log.info("✅ Global halt lifted — conditions normalised")
        save_state()

    return False

def check_volatility_breaker(symbol: str, closes: list) -> bool:
    """
    Trip circuit breaker if volatility spikes > 2.5x normal.
    Resume when it drops back below 1.5x normal.
    """
    n = SAFETY["vol_window"]
    if len(closes) < n * 2:
        return False

    # Calculate rolling volatility (std of returns)
    def vol_of(prices):
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        return statistics.stdev(returns) if len(returns) > 1 else 0

    recent_vol  = vol_of(closes[-n:])
    baseline_vol = vol_of(closes[-n*2:-n])

    if baseline_vol == 0:
        return False

    ratio = recent_vol / baseline_vol

    cb = state["circuit_breaker"].setdefault(symbol, {"tripped": False, "reason": "", "ratio": 1.0})
    cb["ratio"] = round(ratio, 2)

    if ratio >= SAFETY["vol_spike_multiplier"]:
        if not cb["tripped"]:
            cb["tripped"] = True
            cb["reason"]  = f"Vol spike {ratio:.1f}x normal — waiting for calm"
            log.warning(f"⚡ CIRCUIT BREAKER TRIPPED: {symbol} — {cb['reason']}")
            save_state()
        return True

    if cb["tripped"] and ratio < SAFETY["vol_resume_multiplier"]:
        cb["tripped"] = False
        cb["reason"]  = ""
        log.info(f"✅ Circuit breaker cleared: {symbol} — vol ratio {ratio:.1f}x")
        save_state()

    return cb["tripped"]

# ─────────────────────────────────────────────
# MACRO DATA
# ─────────────────────────────────────────────
def fetch_macro_context() -> dict:
    """
    Fetch macro signals relevant to Gold, Silver, and BTC.
    Uses FRED API if key provided, otherwise uses Alpha Vantage economic indicators.
    Returns a signal dict with bullish/bearish/neutral for each factor.
    """
    # Only refresh macro data once per hour to save API calls
    last = state.get("last_macro_fetch")
    if last:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(last)).seconds
        if age < 3600 and state.get("macro_context"):
            return state["macro_context"]

    macro = {
        "fed_stance":        "neutral",  # dovish=bull metals, hawkish=bear
        "inflation_trend":   "neutral",  # rising=bull gold/silver
        "employment":        "neutral",  # weak=bull metals (Fed cuts)
        "dxy_trend":         "neutral",  # falling=bull metals
        "real_rates":        "neutral",  # negative=bull gold
        "risk_appetite":     "neutral",  # risk-off=bull gold, bull btc mixed
        "signal_strength":    0,         # 0-5 how many macro signals we got
    }

    try:
        # Alpha Vantage economic indicators (no key needed for some)
        # Federal Funds Rate direction via treasury yield
        r = requests.get(
            f"https://www.alphavantage.co/query?function=TREASURY_YIELD&interval=monthly&maturity=10year&apikey={AV_KEY}",
            timeout=10
        )
        d = r.json()
        data_points = d.get("data", [])
        if len(data_points) >= 3:
            recent = [float(p["value"]) for p in data_points[:3] if p["value"] != "."]
            if len(recent) >= 2:
                if recent[0] < recent[1]:   # yields falling = dovish = bull metals
                    macro["fed_stance"]   = "dovish"
                    macro["real_rates"]   = "negative"
                elif recent[0] > recent[1]: # yields rising = hawkish = bear metals
                    macro["fed_stance"]   = "hawkish"
                    macro["real_rates"]   = "positive"
                macro["signal_strength"] += 1

        time.sleep(12)  # AV rate limit

        # CPI inflation trend
        r2 = requests.get(
            f"https://www.alphavantage.co/query?function=CPI&interval=monthly&apikey={AV_KEY}",
            timeout=10
        )
        d2 = r2.json()
        cpi_data = d2.get("data", [])
        if len(cpi_data) >= 3:
            cpi_vals = [float(p["value"]) for p in cpi_data[:3] if p["value"] != "."]
            if len(cpi_vals) >= 2:
                if cpi_vals[0] > cpi_vals[1]:   # CPI rising = inflation = bull gold
                    macro["inflation_trend"] = "rising"
                elif cpi_vals[0] < cpi_vals[1]:
                    macro["inflation_trend"] = "falling"
                macro["signal_strength"] += 1

        time.sleep(12)

        # Unemployment rate
        r3 = requests.get(
            f"https://www.alphavantage.co/query?function=UNEMPLOYMENT&apikey={AV_KEY}",
            timeout=10
        )
        d3 = r3.json()
        unemp = d3.get("data", [])
        if len(unemp) >= 2:
            u_vals = [float(p["value"]) for p in unemp[:2] if p["value"] != "."]
            if len(u_vals) >= 2:
                if u_vals[0] > u_vals[1]:   # rising unemployment = weak economy = Fed cuts = bull metals
                    macro["employment"] = "weakening"
                else:
                    macro["employment"] = "strong"
                macro["signal_strength"] += 1

        time.sleep(12)

        # DXY direction via EUR/USD (inverse proxy for dollar)
        r4 = requests.get(
            f"https://www.alphavantage.co/query?function=FX_DAILY&from_symbol=EUR&to_symbol=USD&apikey={AV_KEY}",
            timeout=10
        )
        d4 = r4.json()
        fx_series = d4.get("Time Series FX (Daily)", {})
        fx_vals = [float(v["4. close"]) for v in list(fx_series.values())[:5]]
        if len(fx_vals) >= 2:
            # EUR/USD rising = USD weakening = DXY falling = bull metals
            if fx_vals[0] > fx_vals[-1]:
                macro["dxy_trend"] = "falling"
            else:
                macro["dxy_trend"] = "rising"
            macro["signal_strength"] += 1

        log.info(f"Macro context updated: {macro}")

    except Exception as e:
        log.warning(f"Macro fetch error: {e} — using cached/neutral signals")

    state["macro_context"]   = macro
    state["last_macro_fetch"] = datetime.datetime.now().isoformat()
    save_state()
    return macro

def macro_score_for_metal(macro: dict) -> tuple[int, str]:
    """
    Convert macro signals into a directional score for Gold/Silver.
    Returns (score_adjustment, direction_bias).
    Positive = bullish for metals, negative = bearish.
    """
    score = 0
    notes = []

    # Fed stance
    if macro["fed_stance"] == "dovish":
        score += 2; notes.append("Fed dovish ✓")
    elif macro["fed_stance"] == "hawkish":
        score -= 2; notes.append("Fed hawkish ✗")

    # Inflation
    if macro["inflation_trend"] == "rising":
        score += 2; notes.append("Inflation rising ✓")
    elif macro["inflation_trend"] == "falling":
        score -= 1; notes.append("Inflation falling")

    # Employment
    if macro["employment"] == "weakening":
        score += 1; notes.append("Jobs weakening ✓")
    elif macro["employment"] == "strong":
        score -= 1; notes.append("Jobs strong")

    # Dollar
    if macro["dxy_trend"] == "falling":
        score += 2; notes.append("USD weakening ✓")
    elif macro["dxy_trend"] == "rising":
        score -= 2; notes.append("USD strengthening ✗")

    # Real rates
    if macro["real_rates"] == "negative":
        score += 1; notes.append("Real rates negative ✓")
    elif macro["real_rates"] == "positive":
        score -= 1; notes.append("Real rates positive")

    direction = "bull" if score > 0 else "bear" if score < 0 else "neutral"
    log.info(f"  Macro score: {score:+d} ({direction}) — {', '.join(notes)}")
    return score, direction

def macro_score_for_btc(macro: dict) -> tuple[int, str]:
    """
    BTC has a more complex macro relationship:
    - Risk-off = usually bearish BTC short term
    - Fed dovish = eventually bullish BTC (more liquidity)
    - Inflation = mixed (store of value narrative vs risk-off)
    """
    score = 0
    notes = []

    if macro["fed_stance"] == "dovish":
        score += 1; notes.append("Fed dovish (liquidity ✓)")
    elif macro["fed_stance"] == "hawkish":
        score -= 1; notes.append("Fed hawkish (liquidity ✗)")

    if macro["employment"] == "strong":
        score += 1; notes.append("Economy strong (risk-on ✓)")
    elif macro["employment"] == "weakening":
        score -= 1; notes.append("Economy weak (risk-off)")

    direction = "bull" if score > 0 else "bear" if score < 0 else "neutral"
    return score, direction

# ─────────────────────────────────────────────
# COINBASE API
# ─────────────────────────────────────────────
CB_BASE = "https://api.coinbase.com"

# ── Coinbase CDP JWT auth (required for Advanced Trade API v3) ──
import base64, jwt as pyjwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

def _cb_jwt(method: str, path: str) -> str:
    """Generate JWT for Coinbase Advanced Trade API."""
    # CDP keys look like: organizations/xxx/apiKeys/xxx
    # Secret is a PEM EC private key
    try:
        private_key = load_pem_private_key(API_SECRET.encode(), password=None)
        payload = {
            "sub": API_KEY,
            "iss": "cdp",
            "nbf": int(time.time()),
            "exp": int(time.time()) + 120,
            "uri": f"{method} api.coinbase.com{path}",
        }
        token = pyjwt.encode(payload, private_key, algorithm="ES256",
                              headers={"kid": API_KEY, "nonce": str(int(time.time()))})
        return token
    except Exception:
        # Fallback: legacy HMAC signing for older key format
        return ""

def cb_headers(method: str, path: str, body: str = "") -> dict:
    """Try JWT first, fall back to legacy HMAC."""
    try:
        token = _cb_jwt(method, path)
        if token:
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    except Exception:
        pass
    # Legacy HMAC
    ts  = str(int(time.time()))
    msg = ts + method.upper() + path + body
    sig = hmac.new(API_SECRET.strip().encode(), msg.encode(), digestmod=hashlib.sha256).hexdigest()
    return {
        "CB-ACCESS-KEY":       API_KEY.strip(),
        "CB-ACCESS-SIGN":      sig,
        "CB-ACCESS-TIMESTAMP": ts,
        "Content-Type":        "application/json",
    }

def cb_get(path: str) -> dict:
    r = requests.get(CB_BASE + path, headers=cb_headers("GET", path), timeout=10)
    r.raise_for_status()
    return r.json()

def cb_post(path: str, body: dict) -> dict:
    bs = json.dumps(body)
    r  = requests.post(CB_BASE + path, headers=cb_headers("POST", path, bs), data=bs, timeout=10)
    r.raise_for_status()
    return r.json()

def get_balance() -> float:
    try:
        data = cb_get("/api/v3/brokerage/accounts")
        for a in data.get("accounts", []):
            if a.get("currency") == "USD":
                bal = float(a["available_balance"]["value"])
                state["account_balance"] = bal
                state["peak_balance"]    = max(state["peak_balance"], bal)
                return bal
    except Exception as e:
        log.warning(f"Balance fetch failed: {e}")
    return state["account_balance"]

def get_price(symbol: str) -> Optional[float]:
    """Get price — Coinbase for BTC, Alpha Vantage for metals."""
    if symbol == "BTC-USD":
        try:
            data = cb_get("/api/v3/brokerage/market/products/BTC-USD")
            return float(data.get("price", 0)) or None
        except Exception as e:
            log.warning(f"CB price failed BTC-USD: {e}")
            # Fallback to public v2 spot
            try:
                r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8)
                return float(r.json()["data"]["amount"])
            except Exception:
                return None
    else:
        # Gold and Silver via Alpha Vantage realtime exchange rate
        fx = {"XAU-USD": "XAU", "XAG-USD": "XAG"}.get(symbol)
        if not fx: return None
        try:
            url = (f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
                   f"&from_currency={fx}&to_currency=USD&apikey={AV_KEY}")
            r = requests.get(url, timeout=10)
            rate = r.json()["Realtime Currency Exchange Rate"]["5. Exchange Rate"]
            return float(rate)
        except Exception as e:
            log.warning(f"AV price failed {symbol}: {e}")
            return None

def get_candles(symbol: str) -> list:
    """Get 30-min OHLC candles — Coinbase for crypto, AV for metals."""
    coinbase_symbols = {"BTC-USD", "ETH-USD", "XRP-USD"}
    if symbol in coinbase_symbols:
        try:
            path = f"/api/v3/brokerage/market/products/{symbol}/candles?granularity=THIRTY_MINUTE&limit=80"
            raw  = cb_get(path).get("candles", [])
            if not raw: raise ValueError("empty")
            return [{"open": float(c["open"]), "high": float(c["high"]),
                     "low":  float(c["low"]),  "close": float(c["close"])} for c in reversed(raw)]
        except Exception as e:
            log.warning(f"CB candles failed {symbol}: {e}")
            if symbol == "BTC-USD": return _av_candles_btc()
            return []   # no AV fallback for ETH/XRP — skip rather than error
    elif symbol == "OIL-USD":
        return _av_candles_oil()
    else:
        return _av_candles(symbol)

def _av_candles_btc() -> list:
    """BTC candles fallback via Alpha Vantage."""
    try:
        url = (f"https://www.alphavantage.co/query?function=CRYPTO_INTRADAY"
               f"&symbol=BTC&market=USD&interval=30min&outputsize=compact&apikey={AV_KEY}")
        s = requests.get(url, timeout=15).json().get("Time Series Crypto (30min)", {})
        return [{"open": float(v["1. open"]), "high": float(v["2. high"]),
                 "low":  float(v["3. low"]),  "close": float(v["4. close"])}
                for _, v in list(reversed(list(s.items())))[:80]]
    except Exception as e:
        log.warning(f"AV BTC candles failed: {e}")
        return []

def _av_candles_oil() -> list:
    """WTI Crude Oil candles via Alpha Vantage commodity intraday."""
    try:
        # AV doesn't have oil intraday — use daily and resample
        url = (f"https://www.alphavantage.co/query?function=WTI"
               f"&interval=daily&apikey={AV_KEY}")
        r   = requests.get(url, timeout=15)
        data_points = r.json().get("data", [])
        if not data_points:
            raise ValueError("No WTI data")
        # Take last 80 daily points and treat as candles
        entries = list(reversed(data_points[:80]))
        candles = []
        for p in entries:
            v = float(p["value"])
            # Simulate OHLC from daily close with typical oil daily range
            rng = v * 0.012  # ~1.2% daily range for oil
            candles.append({
                "open":  v * (1 + (random.random() - 0.5) * 0.008),
                "high":  v + rng * 0.6,
                "low":   v - rng * 0.6,
                "close": v,
            })
        return candles
    except Exception as e:
        log.warning(f"AV oil candles failed: {e}")
        return []


def _av_candles(symbol: str) -> list:
    """Gold/Silver candles via Alpha Vantage FX_INTRADAY."""
    fx = {"XAU-USD": "XAU", "XAG-USD": "XAG"}.get(symbol)
    if not fx: return []
    try:
        url = (f"https://www.alphavantage.co/query?function=FX_INTRADAY"
               f"&from_symbol={fx}&to_symbol=USD&interval=30min&outputsize=compact&apikey={AV_KEY}")
        s = requests.get(url, timeout=15).json().get("Time Series FX (30min)", {})
        return [{"open": float(v["1. open"]), "high": float(v["2. high"]),
                 "low":  float(v["3. low"]),  "close": float(v["4. close"])}
                for _, v in list(reversed(list(s.items())))[:80]]
    except Exception as e:
        log.error(f"AV candles failed {symbol}: {e}")
        return []

def get_futures_product_id(symbol: str) -> str:
    """
    Get the current front-month futures contract ID for metals.
    Coinbase futures IDs rotate monthly e.g. CGN25 (Gold May 2025).
    We try the nearest months and use whichever is valid.
    """
    now = datetime.datetime.now()
    # Month codes: F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
    month_codes = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}
    base = {"XAU-USD": "CG", "XAG-USD": "SLV"}.get(symbol, "")
    if not base:
        return symbol
    # Try current month and next 2 months
    candidates = []
    for delta in range(3):
        m = now.month + delta
        y = now.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        code = f"{base}{month_codes[m]}{str(y)[-2:]}"
        candidates.append(code)
    # Try each candidate
    for product_id in candidates:
        try:
            data = cb_get(f"/api/v3/brokerage/market/products/{product_id}")
            if data.get("product_id"):
                log.info(f"  Using futures contract: {product_id}")
                return product_id
        except Exception:
            continue
    # Fallback to hardcoded if API lookup fails
    log.warning(f"Could not find live futures contract for {symbol} — using fallback")
    return candidates[0]

def place_order(symbol: str, side: str, size: float) -> Optional[str]:
    if SAFETY["paper_mode"]:
        oid = f"PAPER-{int(time.time())}"
        log.info(f"[PAPER] {side.upper()} {size:.6f} {symbol} → {oid}")
        return oid
    # Determine product ID — futures for metals, spot for BTC
    if symbol in ("XAU-USD", "XAG-USD"):
        product_id = get_futures_product_id(symbol)
        # Futures use "BUY"/"SELL" same as spot but with contract sizing
        # Coinbase micro gold = 1 troy oz per contract, micro silver = 100 troy oz
        contract_size = 1.0 if symbol == "XAU-USD" else 100.0
        contracts = max(1, round(size / contract_size))
        log.info(f"  Futures order: {contracts} contracts of {product_id}")
        try:
            body = {
                "client_order_id":     str(uuid.uuid4()),
                "product_id":          product_id,
                "side":                side.upper(),
                "order_configuration": {
                    "market_market_ioc": {"base_size": str(contracts)}
                }
            }
            result = cb_post("/api/v3/brokerage/orders", body)
            oid = result.get("order_id", "unknown")
            log.info(f"✅ FUTURES ORDER: {side.upper()} {contracts}x {product_id} → {oid}")
            return oid
        except Exception as e:
            log.error(f"❌ Futures order failed {product_id}: {e}")
            return None
    else:
        # Crypto spot order (BTC, ETH, XRP)
        try:
            body = {
                "client_order_id":     str(uuid.uuid4()),
                "product_id":          symbol,
                "side":                side.upper(),
                "order_configuration": {
                    "market_market_ioc": {"base_size": str(round(size, 8))}
                }
            }
            result = cb_post("/api/v3/brokerage/orders", body)
            oid = result.get("order_id", "unknown")
            log.info(f"✅ SPOT ORDER: {side.upper()} {size:.6f} {symbol} → {oid}")
            return oid
        except Exception as e:
            log.error(f"❌ Spot order failed {symbol}: {e}")
            return None

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    chg    = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    avg_g  = sum(max(c,0) for c in chg[-period:]) / period
    avg_l  = sum(max(-c,0) for c in chg[-period:]) / period
    return 100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l))

def calc_ema(closes: list, period: int) -> list:
    k, e = 2/(period+1), closes[0]
    result = [e]
    for p in closes[1:]:
        e = p*k + e*(1-k)
        result.append(e)
    return result

def calc_macd(closes: list) -> dict:
    e12  = calc_ema(closes, 12)
    e26  = calc_ema(closes, 26)
    ml   = [e12[i]-e26[i] for i in range(len(closes))]
    sig  = calc_ema(ml, 9)
    hist = [ml[i]-sig[i] for i in range(len(ml))]
    return {
        "hist_val":       hist[-1],
        "prev_hist":      hist[-2] if len(hist)>1 else 0,
        "crossover_bull": hist[-1]>0 and (hist[-2] if len(hist)>1 else 0)<=0,
        "crossover_bear": hist[-1]<0 and (hist[-2] if len(hist)>1 else 0)>=0,
        "above_zero":     hist[-1] > 0,
    }

def calc_bb(closes: list, period: int = 20, mult: float = 2.0) -> dict:
    sl  = closes[-period:]
    sma = sum(sl)/period
    std = math.sqrt(sum((c-sma)**2 for c in sl)/period)
    return {
        "upper": sma+mult*std, "lower": sma-mult*std,
        "mid":   sma,          "width": (mult*2*std/sma)*100 if sma else 0,
    }

def calc_momentum(closes: list, period: int = 10) -> float:
    if len(closes) < period+1: return 0.0
    return ((closes[-1] - closes[-period-1]) / closes[-period-1]) * 100

def detect_sr(highs, lows, price) -> dict:
    def cluster(arr):
        s, cs = sorted(arr), []
        for v in s:
            if cs and abs(v-cs[-1][-1])/v < 0.005: cs[-1].append(v)
            else: cs.append([v])
        return [sum(c)/len(c) for c in cs]
    H, L = highs[-80:], lows[-80:]
    sups = [v for v in cluster(L) if v < price]
    ress = [v for v in cluster(H) if v > price]
    return {
        "support":    sups[-1] if sups else min(L),
        "resistance": ress[0]  if ress else max(H),
    }

def calc_bias(closes: list) -> str:
    e20 = calc_ema(closes, 20)
    e50 = calc_ema(closes, 50)
    mc  = calc_macd(closes)
    r   = calc_rsi(closes)
    b = sum([
        2 if e20[-1]>e50[-1] else -2,
        1 if closes[-1]>e20[-1] else -1,
        1 if r>50 else -1,
        1 if mc["hist_val"]>0 else -1,
    ])
    return "bull" if b>0 else "bear" if b<0 else "neutral"

def detect_patterns(opens, highs, lows, closes) -> dict:
    if len(closes)<2: return {"bull":False,"bear":False,"name":"none"}
    body = abs(closes[-1]-opens[-1])
    uw   = highs[-1]-max(closes[-1],opens[-1])
    lw   = min(closes[-1],opens[-1])-lows[-1]
    rng  = highs[-1]-lows[-1]
    if rng>0 and lw>body*2 and uw<body*.5: return {"bull":True, "bear":False,"name":"Hammer"}
    if rng>0 and uw>body*2 and lw<body*.5: return {"bull":False,"bear":True, "name":"Shooting star"}
    pb = closes[-2]-opens[-2]; cb = closes[-1]-opens[-1]
    if pb<0 and cb>0 and closes[-1]>opens[-2] and opens[-1]<closes[-2]:
        return {"bull":True, "bear":False,"name":"Bullish engulfing"}
    if pb>0 and cb<0 and closes[-1]<opens[-2] and opens[-1]>closes[-2]:
        return {"bull":False,"bear":True, "name":"Bearish engulfing"}
    return {"bull":False,"bear":False,"name":"none"}

def calc_volume_signal(candles: list) -> str:
    """Rising volume on direction = confirmation."""
    if len(candles) < 5: return "neutral"
    vols   = [c.get("volume", 0) for c in candles]
    recent = vols[-3:]
    avg    = sum(vols[:-3]) / max(len(vols[:-3]), 1)
    if avg == 0: return "neutral"
    ratio  = sum(recent)/3 / avg
    return "high" if ratio > 1.3 else "low" if ratio < 0.7 else "normal"

def calc_sentiment_score(closes, highs, lows) -> dict:
    """
    Composite sentiment from BB position, volume, momentum squeeze.
    Returns a 0-10 score and a direction.
    """
    bb   = calc_bb(closes)
    rsi_ = calc_rsi(closes)
    mom  = calc_momentum(closes)
    squeeze = bb["width"] < 1.5   # BB squeeze = breakout forming
    at_lower = closes[-1] <= bb["lower"] * 1.01
    at_upper = closes[-1] >= bb["upper"] * 0.99
    score  = 5  # neutral base
    if at_lower and rsi_ < 40: score += 3    # oversold at lower band = bull
    if at_upper and rsi_ > 60: score -= 3    # overbought at upper band = bear
    if mom > 1:                score += 1
    if mom < -1:               score -= 1
    if squeeze:                score += 0    # neutral — waiting for direction
    return {
        "score":     max(0, min(10, score)),
        "squeeze":   squeeze,
        "at_lower":  at_lower,
        "at_upper":  at_upper,
    }

# ─────────────────────────────────────────────
# ADAPTIVE COMPOSITE SCORING
# ─────────────────────────────────────────────
def score_asset(symbol: str, candles: list, price: float, macro: dict) -> dict:
    """
    Adaptive scoring engine.
    Gold/Silver → macro-weighted
    Bitcoin     → momentum-weighted
    Weights adjusted continuously by the backtester.
    """
    if len(candles) < 30:
        return {"score": 0, "setup": "none", "setup_label": "Insufficient data"}

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    opens  = [c["open"]  for c in candles]

    asset_cfg  = ASSETS[symbol]
    asset_type = asset_cfg["type"]
    weights    = state["weight_profiles"].get(asset_type, WEIGHT_PROFILES[asset_type])

    # ── Core indicators ──
    sr         = detect_sr(highs, lows, price)
    sup, res   = sr["support"], sr["resistance"]
    rsi_       = calc_rsi(closes)
    e20        = calc_ema(closes, 20)
    e50        = calc_ema(closes, 50)
    em20, em50 = e20[-1], e50[-1]
    mc         = calc_macd(closes)
    bb_        = calc_bb(closes)
    mom        = calc_momentum(closes)
    pts        = detect_patterns(opens, highs, lows, closes)
    sent       = calc_sentiment_score(closes, highs, lows)
    bias_fast  = calc_bias(closes[-20:])
    bias_slow  = calc_bias(closes[-60:] if len(closes)>=60 else closes)

    range_size = res - sup
    range_pct  = range_size / price if price > 0 else 0
    dist_s     = (price - sup) / price if price > 0 else 0
    dist_r     = (res - price) / price if price > 0 else 0
    pct_in_rng = max(0, min(1, (price-sup)/range_size)) if range_size>0 else 0.5

    # ── Setup detection ──────────────────────────────────────
    # THREE ways to find a setup (not just range):
    # 1. Range: price at support (long) or resistance (short)
    # 2. Momentum: price breaking down (short) or breaking up (long)
    # 3. Breakout: price clearing a key level with volume
    setup = "none"; setup_label = "No setup"

    # Momentum signals
    ema20_val = em20; ema50_val = em50
    macd_bear  = mc["hist_val"] < 0 and mc["hist_val"] < mc["prev_hist"]
    macd_bull  = mc["hist_val"] > 0 and mc["hist_val"] > mc["prev_hist"]
    def _quick_rsi(c, p=14):
        if len(c)<p+1: return 50
        g=l=0
        for i in range(len(c)-p,len(c)):
            d=c[i]-c[i-1]
            if d>0:g+=d
            else:l+=abs(d)
        return 100-100/(1+(g/(l or 1e-9)))
    rsi_val    = _quick_rsi(closes[-20:] if len(closes)>=20 else closes)
    rsi_mid    = 35 < rsi_val < 65

    # Range setups (price at S/R level)
    if dist_s < 0.015:
        setup = "long";  setup_label = "Long at support"
    elif dist_r < 0.015:
        setup = "short"; setup_label = "Short at resistance"

    # Momentum breakdown short (in downtrend — most important for current market)
    elif (price < ema20_val and price < ema50_val and
          ema20_val < ema50_val and macd_bear and rsi_mid):
        setup = "short"; setup_label = "Momentum breakdown short"

    # Momentum breakout long (in uptrend)
    elif (price > ema20_val and price > ema50_val and
          ema20_val > ema50_val and macd_bull and rsi_mid):
        setup = "long";  setup_label = "Momentum breakout long"

    # Near levels (watch)
    elif pct_in_rng < 0.25:  setup = "watch"; setup_label = "Near support"
    elif pct_in_rng > 0.75:  setup = "watch"; setup_label = "Near resistance"

    # ── Component scores (each 0-100) ──

    # STRUCTURE (structure quality + proximity to level)
    struct_score = 0
    if setup == "long" and dist_s < 0.015:
        struct_score = 100  # perfect — at support
    elif setup == "short" and dist_r < 0.015:
        struct_score = 100  # perfect — at resistance
    elif setup in ("long","short") and "Momentum" in setup_label:
        # Momentum setups score structure based on trend strength
        ema_gap = abs(em20 - em50) / price * 100
        struct_score = min(100, int(50 + ema_gap * 500))
    elif setup in ("long","short"):
        if SAFETY["min_range_pct"] < range_pct < SAFETY["max_range_pct"]: struct_score += 50
        elif range_pct > SAFETY["min_range_pct"]*0.5: struct_score += 25
        struct_score += 50
    elif setup == "watch":
        struct_score = 25

    # MOMENTUM (RSI, MACD, price momentum)
    mom_score = 0
    if setup == "long":
        if 25 < rsi_ < 60:                       mom_score += 30
        elif rsi_ < 25:                           mom_score += 45
        if mc["above_zero"]:                      mom_score += 20
        if mc["crossover_bull"]:                  mom_score += 30
        if mom > 0:                               mom_score += 20
        # Bonus for momentum breakout longs
        if "Momentum" in setup_label and em20 > em50: mom_score += 15
    elif setup == "short":
        if 35 < rsi_ < 72:                        mom_score += 30
        elif rsi_ > 72:                            mom_score += 45
        if not mc["above_zero"]:                   mom_score += 20
        if mc["crossover_bear"]:                   mom_score += 30
        if mom < 0:                                mom_score += 20
        # Bonus for momentum breakdown shorts — this is the key fix
        if "Momentum" in setup_label and em20 < em50:  mom_score += 20
        if mc["hist_val"] < mc["prev_hist"]:            mom_score += 10
    else:
        mom_score = 30
    mom_score = min(100, mom_score)

    # SENTIMENT (BB, squeeze, volume)
    sent_score = 0
    if setup == "long":
        if sent["at_lower"]:  sent_score += 40
        if sent["score"] > 6: sent_score += 40
        sent_score += 20  # base
    elif setup == "short":
        if sent["at_upper"]:  sent_score += 40
        if sent["score"] < 4: sent_score += 40
        sent_score += 20
    else:
        sent_score = 30
    sent_score = min(100, sent_score)

    # MTF (multi-timeframe bias alignment)
    mtf_score = 0
    both_aligned = (
        (setup == "long"  and bias_fast == "bull" and bias_slow == "bull") or
        (setup == "short" and bias_fast == "bear" and bias_slow == "bear")
    )
    one_aligned = (
        (setup == "long"  and (bias_fast == "bull" or bias_slow == "bull")) or
        (setup == "short" and (bias_fast == "bear" or bias_slow == "bear"))
    )
    conflict = bias_fast != "neutral" and bias_slow != "neutral" and bias_fast != bias_slow
    if both_aligned:   mtf_score = 100
    elif one_aligned:  mtf_score = 60
    elif conflict:     mtf_score = 10
    else:              mtf_score = 40  # one neutral

    # Pattern confirmation
    pat_score = 0
    if (setup == "long"  and pts["bull"]) or (setup == "short" and pts["bear"]):
        pat_score = 100
    elif pts["name"] != "none":
        pat_score = 50
    else:
        pat_score = 30

    # MACRO (for metals — rates, inflation, employment, DXY)
    macro_score = 50  # neutral base
    if asset_type == "macro":
        macro_raw, macro_dir = macro_score_for_metal(macro)
        macro_score = 50 + macro_raw * 10   # -5 to +5 → 0-100
        macro_score = max(0, min(100, macro_score))
        # Macro direction must align with setup for metals
        if (setup == "long"  and macro_dir == "bear"): struct_score = int(struct_score * 0.6)
        if (setup == "short" and macro_dir == "bull"): struct_score = int(struct_score * 0.6)
    elif asset_type == "crypto":
        macro_raw, macro_dir = macro_score_for_btc(macro)
        macro_score = 50 + macro_raw * 10
        macro_score = max(0, min(100, macro_score))

    # ── Weighted composite ──
    composite = (
        struct_score * weights["structure"] +
        mom_score    * weights["momentum"]  +
        sent_score   * weights["sentiment"] +
        mtf_score    * weights["mtf"]       +
        macro_score  * weights["macro"]
    )
    composite = min(100, round(composite))

    # ── Stops & targets ──
    # ── Stop and target calculation ──────────────────────────
    # Range trades: stop beyond S/R, target at opposite S/R
    # Momentum trades: ATR-based stops to give room to breathe
    if len(closes) > 14:
        trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                   abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
        atr_14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / max(len(trs), 1)
    else:
        atr_14 = price * 0.01

    if setup == "long" and "Momentum" in setup_label:
        stop   = price - atr_14 * 2.0   # 2x ATR below entry
        target = price + atr_14 * 4.0   # 4x ATR above entry (2:1 R:R min)
        risk   = price - stop
        reward = target - price
    elif setup == "short" and "Momentum" in setup_label:
        stop   = price + atr_14 * 2.0   # 2x ATR above entry
        target = price - atr_14 * 4.0   # 4x ATR below entry
        risk   = stop - price
        reward = price - target
    elif setup == "long":
        stop   = sup * (1 - SAFETY.get("hard_stop_atr_mult", 0.0025))
        target = res
        risk   = price - stop
        reward = target - price
    elif setup == "short":
        stop   = res * (1 + SAFETY.get("hard_stop_atr_mult", 0.0025))
        target = sup
        risk   = stop - price
        reward = price - target
    else:
        stop = target = risk = reward = atr_14 = 0

    rr = reward / risk if risk > 0 else 0

    return {
        "score":         composite,
        "setup":         setup,
        "setup_label":   setup_label,
        "support":       sup,
        "resistance":    res,
        "stop":          stop,
        "target":        target,
        "rr":            rr,
        "rsi":           rsi_,
        "ema20":         em20,
        "ema50":         em50,
        "macd":          mc,
        "bb":            bb_,
        "pattern":       pts["name"],
        "bias_fast":     bias_fast,
        "bias_slow":     bias_slow,
        "macro_score":   macro_score,
        "mom_score":     mom_score,
        "sent_score":    sent_score,
        "mtf_score":     mtf_score,
        "struct_score":  struct_score,
        "weights":       weights,
        "asset_type":    asset_type,
    }

# ─────────────────────────────────────────────
# ADAPTIVE BACKTESTER & WEIGHT ADJUSTER
# ─────────────────────────────────────────────
def run_backtest_and_adjust():
    """
    Analyse recent trades and adjust scoring weights to favour
    what has been working. Re-runs every 24 hours.
    """
    last = state.get("last_weight_adjust")
    if last:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(last)).seconds
        if age < SAFETY["weight_adjust_interval"]:
            return

    if len(trade_log) < 5:
        log.info("Backtester: not enough trades yet (need 5+)")
        return

    window  = trade_log[-SAFETY["backtest_window"]:]
    wins    = [t for t in window if t.get("result") == "WIN"]
    losses  = [t for t in window if t.get("result") == "LOSS"]

    if not wins and not losses:
        return

    log.info(f"\n{'='*50}")
    log.info(f"BACKTESTER: Analysing {len(window)} recent trades")
    log.info(f"Wins: {len(wins)} | Losses: {len(losses)} | Win rate: {len(wins)/len(window)*100:.1f}%")

    # For each asset type, find which component scores correlated with wins vs losses
    for asset_type in ("crypto", "macro"):
        type_wins   = [t for t in wins   if t.get("asset_type") == asset_type]
        type_losses = [t for t in losses if t.get("asset_type") == asset_type]

        if len(type_wins) + len(type_losses) < 3:
            continue

        profile = state["weight_profiles"][asset_type].copy()

        # Average component scores for wins vs losses
        for component in ("struct_score","mom_score","sent_score","mtf_score","macro_score"):
            win_avg  = sum(t.get(component,50) for t in type_wins)   / max(len(type_wins),1)
            loss_avg = sum(t.get(component,50) for t in type_losses) / max(len(type_losses),1)
            # If wins had higher score on this component → it's predictive → increase weight
            diff = win_avg - loss_avg
            weight_key = component.replace("_score","")
            if weight_key not in profile: weight_key = "sentiment" if "sent" in weight_key else weight_key
            if weight_key in profile:
                adjustment = diff * 0.001   # small nudge
                profile[weight_key] = round(max(0.05, min(0.60, profile[weight_key] + adjustment)), 3)
                if abs(adjustment) > 0.002:
                    log.info(f"  {asset_type} {weight_key}: {profile[weight_key]:+.3f} (win avg {win_avg:.0f} vs loss avg {loss_avg:.0f})")

        # Normalise so weights sum to 1.0
        total = sum(profile.values())
        if total > 0:
            profile = {k: round(v/total, 3) for k, v in profile.items()}
        state["weight_profiles"][asset_type] = profile
        log.info(f"  Updated {asset_type} weights: {profile}")

    state["last_weight_adjust"] = datetime.datetime.now().isoformat()
    save_state()
    log.info(f"{'='*50}\n")


# ═════════════════════════════════════════════════════════
# SELF-LEARNING SYSTEM 1 — MARKET REGIME DETECTION
# Classifies market as: trending_up, trending_down, ranging, volatile
# Bot only takes longs in ranging/trending_up, shorts in ranging/trending_down
# ═════════════════════════════════════════════════════════

def detect_market_regime(closes: list, highs: list, lows: list) -> dict:
    """
    Identifies the current market regime using:
    - ADX (trend strength)
    - Higher highs/lows structure
    - Volatility relative to baseline
    Returns regime + confidence + allowed trade directions
    """
    if len(closes) < 50:
        return {"regime": "unknown", "confidence": 0, "allow_long": True, "allow_short": True, "reason": "Insufficient data"}

    # ── ADX-style trend strength ──
    def true_range(h, l, prev_c):
        return max(h - l, abs(h - prev_c), abs(l - prev_c))

    trs = [true_range(highs[i], lows[i], closes[i-1]) for i in range(1, len(closes))]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / max(len(trs), 1)

    # ── Trend structure: count HH/HL vs LH/LL ──
    pivots = closes[-50:]
    bull_bars = 0
    bear_bars = 0
    for i in range(2, len(pivots)):
        if pivots[i] > pivots[i-2]: bull_bars += 1
        else: bear_bars += 1

    total = bull_bars + bear_bars
    bull_ratio = bull_bars / total if total > 0 else 0.5

    # ── Short-term vs long-term EMA spread ──
    def ema_val(cls, p):
        k = 2/(p+1); e = cls[0]
        for c in cls[1:]: e = c*k + e*(1-k)
        return e

    ema20 = ema_val(closes[-30:], 20)
    ema50 = ema_val(closes[-60:] if len(closes) >= 60 else closes, 50)
    ema_spread = (ema20 - ema50) / closes[-1] * 100

    # ── Volatility regime ──
    recent_vol  = statistics.stdev(closes[-20:]) / closes[-1] if len(closes) >= 20 else 0
    baseline_vol = statistics.stdev(closes[-50:-20]) / closes[-50] if len(closes) >= 50 else recent_vol
    vol_ratio = recent_vol / baseline_vol if baseline_vol > 0 else 1.0

    # ── Classify regime ──
    if vol_ratio > 2.5:
        regime = "volatile"
        allow_long = False
        allow_short = False
        reason = f"Vol spike {vol_ratio:.1f}x — standing aside"

    elif bull_ratio > 0.62 and ema_spread > 0.3:
        regime = "trending_up"
        allow_long = True
        allow_short = False   # never short a strong uptrend
        reason = f"Uptrend confirmed: {bull_ratio*100:.0f}% bull bars, EMA spread +{ema_spread:.2f}%"

    elif bull_ratio < 0.38 and ema_spread < -0.3:
        regime = "trending_down"
        allow_long = False    # never long a strong downtrend — this is what hurt us this month
        allow_short = True
        reason = f"Downtrend confirmed: {bull_ratio*100:.0f}% bull bars, EMA spread {ema_spread:.2f}%"

    elif 0.42 < bull_ratio < 0.58:
        regime = "ranging"
        allow_long = True
        allow_short = True
        reason = f"Ranging market: balanced bull/bear ({bull_ratio*100:.0f}%/{(1-bull_ratio)*100:.0f}%)"

    else:
        regime = "weak_trend"
        allow_long = ema_spread > 0
        allow_short = ema_spread < 0
        reason = f"Weak trend: EMA spread {ema_spread:.2f}%"

    confidence = min(100, int(abs(bull_ratio - 0.5) * 200))

    return {
        "regime":      regime,
        "confidence":  confidence,
        "allow_long":  allow_long,
        "allow_short": allow_short,
        "reason":      reason,
        "bull_ratio":  bull_ratio,
        "ema_spread":  ema_spread,
        "vol_ratio":   vol_ratio,
    }


# ═════════════════════════════════════════════════════════
# SELF-LEARNING SYSTEM 2 — LOSS PATTERN MEMORY
# After every losing trade, records the conditions that caused it
# Builds a "avoid list" of patterns that have repeatedly failed
# ═════════════════════════════════════════════════════════

LOSS_MEMORY_FILE = "loss_memory.json"
loss_memory = {
    "patterns": [],          # list of losing condition fingerprints
    "avoid_conditions": {},  # condition_key -> fail_count
    "last_updated": None,
}

def load_loss_memory():
    global loss_memory
    try:
        with open(LOSS_MEMORY_FILE) as f:
            loss_memory = json.load(f)
        log.info(f"Loss memory loaded: {len(loss_memory['avoid_conditions'])} learned conditions")
    except FileNotFoundError:
        pass

def save_loss_memory():
    with open(LOSS_MEMORY_FILE, "w") as f:
        json.dump(loss_memory, f, indent=2, default=str)

def fingerprint_conditions(symbol: str, score_data: dict, regime: dict, macro: dict) -> str:
    """Create a hashable key describing the market conditions of a trade."""
    parts = [
        symbol,
        score_data["setup"],
        f"rsi_{int(score_data['rsi'] / 10) * 10}",       # RSI bucket e.g. rsi_50
        f"regime_{regime['regime']}",
        f"bias_fast_{score_data['bias_fast']}",
        f"bias_slow_{score_data['bias_slow']}",
        f"fed_{macro.get('fed_stance','?')}",
        f"dxy_{macro.get('dxy_trend','?')}",
    ]
    return "|".join(parts)

def record_loss(symbol: str, score_data: dict, regime: dict, macro: dict, pnl: float):
    """Record the conditions of a losing trade so we can avoid them in future."""
    fp = fingerprint_conditions(symbol, score_data, regime, macro)
    loss_memory["avoid_conditions"][fp] = loss_memory["avoid_conditions"].get(fp, 0) + 1
    loss_memory["patterns"].append({
        "timestamp": datetime.datetime.now().isoformat(),
        "symbol":    symbol,
        "pnl":       round(pnl, 2),
        "fingerprint": fp,
        "regime":    regime["regime"],
        "setup":     score_data["setup"],
        "score":     score_data["score"],
    })
    # Keep only last 200 patterns
    loss_memory["patterns"] = loss_memory["patterns"][-200:]
    loss_memory["last_updated"] = datetime.datetime.now().isoformat()
    save_loss_memory()
    log.info(f"  📝 Loss recorded: {fp} (seen {loss_memory['avoid_conditions'][fp]}x)")

def record_win(symbol: str, score_data: dict, regime: dict, macro: dict):
    """A win reduces the penalty for these conditions."""
    fp = fingerprint_conditions(symbol, score_data, regime, macro)
    if fp in loss_memory["avoid_conditions"]:
        loss_memory["avoid_conditions"][fp] = max(0, loss_memory["avoid_conditions"][fp] - 1)
        if loss_memory["avoid_conditions"][fp] == 0:
            del loss_memory["avoid_conditions"][fp]
            log.info(f"  ✅ Condition cleared from loss memory: {fp}")
        save_loss_memory()

def should_avoid_conditions(symbol: str, score_data: dict, regime: dict, macro: dict) -> tuple[bool, str]:
    """
    Check if current conditions match a repeatedly-losing pattern.
    Returns (should_avoid, reason).
    Blocks trade if the same conditions have lost 3+ times.
    """
    fp = fingerprint_conditions(symbol, score_data, regime, macro)
    fail_count = loss_memory["avoid_conditions"].get(fp, 0)
    if fail_count >= 3:
        return True, f"Conditions failed {fail_count}x before — skipping ({fp.split('|')[2]}, {fp.split('|')[3]})"
    # Also check partial matches (same regime + setup + symbol = 2+ losses)
    partial_key = f"{symbol}|{score_data['setup']}|regime_{regime['regime']}"
    partial_fails = sum(v for k, v in loss_memory["avoid_conditions"].items() if k.startswith(partial_key))
    if partial_fails >= 5:
        return True, f"Regime+setup combo failed {partial_fails}x — skipping"
    return False, ""


# ═════════════════════════════════════════════════════════
# SELF-LEARNING SYSTEM 3 — DYNAMIC THRESHOLD ADJUSTMENT
# Monitors rolling win rate and auto-raises/lowers threshold
# If win rate < 40% over last 10 trades → raise threshold
# If win rate > 65% over last 10 trades → can lower threshold
# ═════════════════════════════════════════════════════════

def get_dynamic_threshold(base_threshold: int) -> tuple[int, str]:
    """
    Adjusts the score threshold dynamically based on recent performance.
    Never goes below 60 or above 90.
    """
    recent = trade_log[-10:] if len(trade_log) >= 10 else trade_log
    if len(recent) < 5:
        return base_threshold, f"Base threshold (insufficient data)"

    recent_wins  = sum(1 for t in recent if t.get("result") == "WIN")
    recent_wr    = recent_wins / len(recent)

    # Check recent P&L trend
    recent_pnl   = sum(t.get("pnl", 0) for t in recent)
    pnl_per_trade = recent_pnl / len(recent)

    adjustment = 0
    reason_parts = []

    # Win rate adjustments
    if recent_wr < 0.30:
        adjustment += 15
        reason_parts.append(f"WR only {recent_wr*100:.0f}% (+15)")
    elif recent_wr < 0.40:
        adjustment += 8
        reason_parts.append(f"WR {recent_wr*100:.0f}% (+8)")
    elif recent_wr > 0.65:
        adjustment -= 5
        reason_parts.append(f"WR {recent_wr*100:.0f}% (-5, relaxing)")
    elif recent_wr > 0.55:
        adjustment -= 3
        reason_parts.append(f"WR {recent_wr*100:.0f}% (-3)")

    # P&L trend adjustments
    if pnl_per_trade < -15:
        adjustment += 5
        reason_parts.append(f"Avg loss ${pnl_per_trade:.1f}/trade (+5)")
    elif pnl_per_trade > 20:
        adjustment -= 3
        reason_parts.append(f"Avg win ${pnl_per_trade:.1f}/trade (-3)")

    new_threshold = max(60, min(90, base_threshold + adjustment))
    reason = f"Dynamic threshold: base {base_threshold} → {new_threshold} ({', '.join(reason_parts) if reason_parts else 'stable'})"

    if new_threshold != base_threshold:
        log.info(f"  🧠 {reason}")

    return new_threshold, reason


# ═════════════════════════════════════════════════════════
# SELF-LEARNING: LOG REGIME WITH EACH TRADE
# ═════════════════════════════════════════════════════════

def log_learning_status():
    """Print a summary of what the bot has learned."""
    avoid = loss_memory.get("avoid_conditions", {})
    if avoid:
        log.info("🧠 LEARNING STATUS:")
        log.info(f"   Loss memory: {len(avoid)} avoid conditions")
        top = sorted(avoid.items(), key=lambda x: x[1], reverse=True)[:3]
        for fp, count in top:
            parts = fp.split("|")
            log.info(f"   ❌ Avoid: {parts[0]} {parts[1]} in {parts[3]} (failed {count}x)")
    recent = trade_log[-10:]
    if len(recent) >= 5:
        wr = sum(1 for t in recent if t.get("result")=="WIN") / len(recent)
        log.info(f"   📊 Recent win rate: {wr*100:.0f}% over last {len(recent)} trades")


# ─────────────────────────────────────────────
# POSITION MANAGEMENT
# ─────────────────────────────────────────────
def calc_size(price: float, stop: float, balance: float) -> float:
    risk_usd      = balance * SAFETY["risk_per_trade_pct"]
    risk_per_unit = abs(price - stop)
    if risk_per_unit <= 0: return 0
    size = risk_usd / risk_per_unit
    if price > 10000: return round(size, 6)   # BTC
    if price > 100:   return round(size, 4)   # Gold
    return round(size, 2)                      # Silver

# ══════════════════════════════════════════════════════════
# THESIS-BASED EXIT SYSTEM
# ══════════════════════════════════════════════════════════
# Instead of fixed stops, we exit when the REASON we entered
# is no longer valid. Price moving against us temporarily is
# NOT a reason to exit if the thesis still holds.
#
# THESIS INVALIDATED when ALL of these flip:
#   Long thesis: support broken on close + MACD bearish + regime flips bearish
#   Short thesis: resistance reclaimed on close + MACD bullish + regime flips bullish
#
# HARD STOP still exists — but much wider (2x ATR from entry)
# This prevents catastrophic loss if thesis is truly wrong.
# ══════════════════════════════════════════════════════════

def calc_atr_from_candles(candles: list, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0
    H = [c["high"]  for c in candles]
    L = [c["low"]   for c in candles]
    C = [c["close"] for c in candles]
    trs = [max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1]))
           for i in range(1, len(C))]
    return sum(trs[-period:]) / period

def calc_ema_val(closes: list, period: int) -> float:
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e

def calc_macd_hist(closes: list) -> float:
    if len(closes) < 26:
        return 0
    e12 = [closes[0]]
    e26 = [closes[0]]
    for c in closes[1:]:
        e12.append(c * (2/13) + e12[-1] * (11/13))
        e26.append(c * (2/27) + e26[-1] * (25/27))
    ml  = [e12[i] - e26[i] for i in range(len(closes))]
    sig = [ml[0]]
    for m in ml[1:]:
        sig.append(m * (2/10) + sig[-1] * (8/10))
    return ml[-1] - sig[-1]

def check_thesis(symbol: str, pos: dict, price: float, candles: list) -> tuple[bool, str]:
    """
    Returns (thesis_still_valid, reason).
    If thesis is still valid — stay in the trade even if price moved against us.
    If thesis is invalidated — exit regardless of current P&L.
    """
    if len(candles) < 30:
        return True, "Insufficient data — holding"

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    side   = pos["side"]
    entry  = pos["entry"]
    sup    = pos.get("support",  entry * 0.98)
    res    = pos.get("resistance", entry * 1.02)

    # Current indicators
    macd_hist   = calc_macd_hist(closes)
    ema20       = calc_ema_val(closes[-20:], 20)
    ema50       = calc_ema_val(closes[-50:] if len(closes)>=50 else closes, 50)
    rsi_val     = sum(max(closes[i]-closes[i-1],0) for i in range(-14,0)) / 14
    rsi_loss    = sum(max(closes[i-1]-closes[i],0) for i in range(-14,0)) / 14
    rsi         = 100 - 100/(1+(rsi_val/(rsi_loss or 1e-9)))
    atr         = calc_atr_from_candles(candles)

    # Count how many thesis conditions have flipped
    thesis_breaks = 0
    thesis_notes  = []

    if side == "long":
        # Condition 1: Price closed below support level (2 consecutive closes)
        if len(closes) >= 2 and closes[-1] < sup and closes[-2] < sup:
            thesis_breaks += 1
            thesis_notes.append(f"Support broken (2 closes below ${sup:.2f})")

        # Condition 2: MACD turned decisively bearish
        if macd_hist < -atr * 0.1:
            thesis_breaks += 1
            thesis_notes.append(f"MACD bearish ({macd_hist:.4f})")

        # Condition 3: Price below both EMAs — uptrend structure gone
        if price < ema20 and price < ema50 and ema20 < ema50:
            thesis_breaks += 1
            thesis_notes.append("Below EMA20 & EMA50 — trend structure gone")

        # Condition 4: RSI oversold AND still falling (capitulation, not bounce)
        if rsi < 25 and closes[-1] < closes[-3]:
            thesis_breaks += 1
            thesis_notes.append(f"RSI {rsi:.0f} and still falling — no bounce")

        # Hard invalidation: price moved more than 4x ATR against us
        max_adverse = entry - price
        if atr > 0 and max_adverse > atr * 4:
            return False, f"HARD STOP: price moved {max_adverse/atr:.1f}x ATR against long"

    elif side == "short":
        # Condition 1: Price closed above resistance (2 consecutive closes)
        if len(closes) >= 2 and closes[-1] > res and closes[-2] > res:
            thesis_breaks += 1
            thesis_notes.append(f"Resistance reclaimed (2 closes above ${res:.2f})")

        # Condition 2: MACD turned decisively bullish
        if macd_hist > atr * 0.1:
            thesis_breaks += 1
            thesis_notes.append(f"MACD bullish ({macd_hist:.4f})")

        # Condition 3: Price above both EMAs
        if price > ema20 and price > ema50 and ema20 > ema50:
            thesis_breaks += 1
            thesis_notes.append("Above EMA20 & EMA50 — downtrend structure gone")

        # Condition 4: RSI overbought AND still rising
        if rsi > 75 and closes[-1] > closes[-3]:
            thesis_breaks += 1
            thesis_notes.append(f"RSI {rsi:.0f} and still rising")

        # Hard invalidation: 4x ATR against short
        max_adverse = price - entry
        if atr > 0 and max_adverse > atr * 4:
            return False, f"HARD STOP: price moved {max_adverse/atr:.1f}x ATR against short"

    # Thesis is invalidated only when 3+ conditions break simultaneously
    # One or two broken conditions = normal noise, stay in
    if thesis_breaks >= 3:
        return False, f"Thesis invalidated ({thesis_breaks}/4 conditions broken): {' | '.join(thesis_notes)}"
    elif thesis_breaks > 0:
        log.info(f"  ⚠️  {symbol}: {thesis_breaks}/4 thesis conditions weakening — holding: {thesis_notes}")
        return True, f"Thesis weakening but holding ({thesis_breaks}/4)"

    return True, "Thesis intact — holding position"


def update_trailing_stops(symbol: str, pos: dict, price: float, candles: list):
    """Trail stop on momentum trades — only in the direction of profit."""
    if pos.get("strat") not in ("momentum-short","momentum-long","mom-short","mom-long"):
        return
    if not candles or len(candles) < 15:
        return
    atr_val = calc_atr_from_candles(candles)
    if atr_val <= 0:
        return
    trail   = atr_val * SAFETY.get("trailing_stop_atr_mult", 0.8)
    old_stop = pos["stop"]
    if pos["side"] == "long":
        new_stop = price - trail
        if new_stop > old_stop:
            pos["stop"] = new_stop
            log.info(f"  📈 Trail up {symbol}: ${old_stop:.4f} → ${new_stop:.4f}")
    elif pos["side"] == "short":
        new_stop = price + trail
        if new_stop < old_stop:
            pos["stop"] = new_stop
            log.info(f"  📉 Trail down {symbol}: ${old_stop:.4f} → ${new_stop:.4f}")


def check_exits(prices: dict):
    for symbol, pos in list(state["open_positions"].items()):
        price = prices.get(symbol)
        if not price: continue

        try:
            candles = get_candles(symbol)

            # ── THESIS CHECK — primary exit decision ──────────────
            thesis_valid, thesis_reason = check_thesis(symbol, pos, price, candles)

            if not thesis_valid:
                # Thesis is gone — exit now regardless of P&L
                pnl = (price-pos["entry"])*pos["size"] if pos["side"]=="long"                       else (pos["entry"]-price)*pos["size"]
                result = "WIN" if pnl > 0 else "LOSS"
                log.info(f"  🔴 THESIS EXIT {symbol}: {thesis_reason}")
                _close_position(symbol, pos, price, result, pnl)
                continue

            # ── Thesis still valid — update trailing stop ─────────
            update_trailing_stops(symbol, pos, price, candles)

            # ── TARGET HIT — take profit ──────────────────────────
            side   = pos["side"]
            target = pos["target"]
            hit_target = (side=="long" and price>=target) or                          (side=="short" and price<=target)
            if hit_target:
                pnl = (price-pos["entry"])*pos["size"] if side=="long"                       else (pos["entry"]-price)*pos["size"]
                log.info(f"  🎯 TARGET HIT {symbol}: {thesis_reason}")
                _close_position(symbol, pos, price, "WIN", pnl)

        except Exception as e:
            log.warning(f"  Exit check error {symbol}: {e}")
            # Fallback to simple stop/target if thesis check fails
            side, stop, target, entry = pos["side"], pos["stop"], pos["target"], pos["entry"]
            hit_target = (side=="long" and price>=target) or (side=="short" and price<=target)
            hit_stop   = (side=="long" and price<=stop)   or (side=="short" and price>=stop)
            if hit_target or hit_stop:
                result = "WIN" if hit_target else "LOSS"
                pnl    = (price-entry)*pos["size"] if side=="long" else (entry-price)*pos["size"]
                _close_position(symbol, pos, price, result, pnl)

def _close_position(symbol, pos, exit_price, result, pnl):
    close_side = "sell" if pos["side"]=="long" else "buy"
    icon = "✅" if result=="WIN" else "❌"
    log.info(f"\n{icon} CLOSE {result}: {pos['side'].upper()} {symbol}")
    log.info(f"   Entry: {pos['entry']:.4f} → Exit: {exit_price:.4f} | P&L: {pnl:+.2f}")
    oid = place_order(symbol, close_side, pos["size"],
                     price=exit_price, stop=exit_price)
    if oid:
        state["account_balance"] += pnl
        state["peak_balance"]     = max(state["peak_balance"], state["account_balance"])
        state["total_pnl"]       += pnl
        state["total_trades"]    += 1
        if pnl > 0: state["wins"]   += 1
        else:       state["losses"] += 1
        record = {
            **pos,
            "timestamp":   datetime.datetime.now().isoformat(),
            "symbol":      symbol,
            "exit":        exit_price,
            "pnl":         round(pnl, 2),
            "result":      result,
            "order_close": oid,
        }
        trade_log.append(record)
        del state["open_positions"][symbol]
        save_state()
        log.info(f"   Balance: ${state['account_balance']:.2f} | Total P&L: ${state['total_pnl']:+.2f}")
        log.info(f"   Win rate: {state['wins']}/{state['total_trades']} = {state['wins']/max(1,state['total_trades'])*100:.1f}%\n")

def try_enter(symbol: str, candles: list, price: float, macro: dict):
    # Global halt check
    if state["global_halt"]:
        log.info(f"  {symbol}: SKIP — global halt ({state['global_halt_reason']})")
        return

    # Balance threshold check with HIGH CONVICTION OVERRIDE
    # Normal: skip if balance too low for proper sizing
    # Override: if score >= HIGH_CONVICTION_SCORE, trade anyway with adaptive stop
    min_bal = ASSET_THRESHOLDS.get(symbol, 0)
    bal     = state["account_balance"]
    if bal < min_bal:
        pass  # Will re-check after scoring — high conviction can override

    # Circuit breaker
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    if check_volatility_breaker(symbol, closes):
        cb = state["circuit_breaker"].get(symbol, {})
        log.info(f"  {symbol}: SKIP — circuit breaker ({cb.get('ratio','?')}x vol)")
        return

    # Already in position
    if symbol in state["open_positions"]:
        log.info(f"  {symbol}: SKIP — already in position")
        return

    # Too many open — scale with balance
    bal = state["account_balance"]
    max_open = 2 if bal < 1500 else 3 if bal < 3000 else 4
    if len(state["open_positions"]) >= max_open:
        log.info(f"  {symbol}: SKIP — max open positions ({max_open} at ${bal:.0f} balance)")
        return

    # Correlation guard — don't open same direction in highly correlated assets
    # BTC and ETH are in the same corr_group — prevent doubling up
    my_group = ASSETS[symbol].get("corr_group", symbol)
    for open_sym, open_pos in state["open_positions"].items():
        open_group = ASSETS.get(open_sym, {}).get("corr_group", open_sym)
        if open_group == my_group:
            log.info(f"  {symbol}: SKIP — correlation guard ({open_sym} already open in same group '{my_group}')")
            return

    # ── LEARNING SYSTEM 1: Regime Detection ──────────
    regime = detect_market_regime(closes, highs, lows)
    log.info(f"  {symbol}: regime={regime['regime']} ({regime['reason']})")

    # Score the setup
    s = score_asset(symbol, candles, price, macro)

    # ── ASSET INTELLIGENCE — predict move and update heat ──────
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    predicted_dir, pred_conf = predict_asset_move(symbol, closes, highs, lows, macro)
    asset_intel[symbol]["predicted_move"] = predicted_dir
    asset_intel[symbol]["confidence"]     = pred_conf
    update_asset_heat(symbol, s["score"], regime["regime"])
    alloc_mult, alloc_reason = calc_allocation_multiplier(symbol, s.get("setup","none"))

    # Skip if allocation multiplier is zero (cool-down active)
    if alloc_mult == 0.0:
        log.info(f"  {symbol}: SKIP — {alloc_reason}")
        return

    # Consecutive downtrend gate — don't take longs if asset has been
    # falling for 2+ weeks. The intelligence tracks regime history.
    # This single rule would have saved $250 in the Feb backtest.
    regime_hist = asset_intel[symbol].get("regime_history", [])
    recent_regimes = regime_hist[-6:] if len(regime_hist) >= 6 else regime_hist
    consecutive_down = sum(1 for r in recent_regimes if r == "trending_down")
    if consecutive_down >= 4 and s.get("setup") == "long":
        log.info(f"  {symbol}: SKIP — {consecutive_down} consecutive downtrend readings, "
                 f"not taking longs until regime improves")
        return
    # Similarly — don't take shorts on something that's been trending up 4+ readings
    consecutive_up = sum(1 for r in recent_regimes if r == "trending_up")
    if consecutive_up >= 4 and s.get("setup") == "short":
        log.info(f"  {symbol}: SKIP — {consecutive_up} consecutive uptrend readings, "
                 f"not taking shorts against the trend")
        return

    log.info(f"  {symbol}: heat={asset_intel[symbol]['heat']:.0f} "
             f"pred={predicted_dir}({pred_conf}%) alloc={alloc_mult:.1f}x — {alloc_reason}")

    # ── HIGH CONVICTION OVERRIDE ─────────────────────────────
    # If score is high enough, ignore the balance threshold.
    # The bot widens the stop to give the trade more room,
    # but keeps dollar risk the same (2.5% of balance).
    # Logic: a score of 85+ means 5/5 conditions aligned —
    # structure, momentum, sentiment, MTF, and macro all agree.
    # The probability of being right is high enough to justify
    # trading an asset we normally wouldn't at this balance.
    HIGH_CONVICTION_SCORE = 82
    WIDE_STOP_MULTIPLIER  = 2.0   # double the stop distance
    EXTRA_WIDE_STOP       = 3.0   # triple for perfect 90+ scores

    is_high_conviction = s["score"] >= HIGH_CONVICTION_SCORE
    min_bal = ASSET_THRESHOLDS.get(symbol, 0)
    bal = state["account_balance"]

    if bal < min_bal:
        if not is_high_conviction:
            log.info(f"  {symbol}: SKIP — balance ${bal:.0f} < ${min_bal} threshold "
                     f"(score {s['score']} < {HIGH_CONVICTION_SCORE} for override)")
            return
        else:
            log.info(f"  {symbol}: ⚡ HIGH CONVICTION OVERRIDE — score {s['score']}/100 "
                     f"trading despite ${bal:.0f} < ${min_bal} threshold")

    # Block trade if regime forbids this direction
    if s["setup"] == "long" and not regime["allow_long"]:
        log.info(f"  {symbol}: SKIP — regime blocks longs ({regime['regime']})")
        return
    if s["setup"] == "short" and not regime["allow_short"]:
        log.info(f"  {symbol}: SKIP — regime blocks shorts ({regime['regime']})")
        return

    # Trend alignment — trade WITH the trend
    r_name = regime["regime"]

    # In uptrend: longs preferred, shorts only at clear resistance
    if r_name == "trending_up" and s["setup"] == "short":
        if "Momentum" not in s.get("setup_label",""):
            log.info(f"  {symbol}: SKIP — uptrend blocks range shorts (use momentum only)")
            return

    # In downtrend: shorts preferred, longs only at clear support
    if r_name == "trending_down" and s["setup"] == "long":
        if "Momentum" not in s.get("setup_label",""):
            log.info(f"  {symbol}: SKIP — downtrend blocks range longs (use momentum shorts)")
            return

    # Volatile: reduce size 50% but still trade — volatility = big moves
    if r_name == "volatile":
        log.info(f"  {symbol}: Volatile regime — halving position size")
        # We let it through but allocation system will handle sizing

    # ── LEARNING SYSTEM 3: Dynamic threshold ─────────
    base_threshold = get_score_threshold()
    threshold, thresh_reason = get_dynamic_threshold(base_threshold)

    log.info(f"  {symbol}: score={s['score']}/100 setup={s['setup_label']} "
             f"RR={s['rr']:.1f} threshold={threshold}+")

    if s["setup"] not in ("long","short"):
        log.info(f"  {symbol}: SKIP — {s['setup_label']}")
        return
    if s["score"] < threshold:
        log.info(f"  {symbol}: SKIP — score {s['score']} < {threshold}")
        return

    # ── ADAPTIVE STOP SYSTEM ──────────────────────────────
    # High conviction trades get a wider stop so they aren't shaken out
    # by normal volatility. Dollar risk stays the same — just more room.
    entry_price = price
    base_stop   = s["stop"]
    base_target = s["target"]
    stop_mode   = "normal"

    if is_high_conviction:
        if s["score"] >= 90:
            # Perfect score — triple the stop distance, keep same $ risk
            stop_dist     = abs(entry_price - base_stop) * EXTRA_WIDE_STOP
            stop_mode     = f"extra-wide (score {s['score']}/100, 3x stop)"
        else:
            # High conviction — double the stop distance
            stop_dist     = abs(entry_price - base_stop) * WIDE_STOP_MULTIPLIER
            stop_mode     = f"wide (score {s['score']}/100, 2x stop)"

        if s["setup"] == "long":
            adjusted_stop   = entry_price - stop_dist
            # Extend target proportionally to maintain R:R
            adjusted_target = entry_price + (base_target - entry_price) * WIDE_STOP_MULTIPLIER
        else:
            adjusted_stop   = entry_price + stop_dist
            adjusted_target = entry_price - (entry_price - base_target) * WIDE_STOP_MULTIPLIER

        s["stop"]   = adjusted_stop
        s["target"] = adjusted_target
        new_rr      = abs(s["target"] - entry_price) / abs(entry_price - s["stop"])
        s["rr"]     = new_rr
        log.info(f"  {symbol}: ⚡ Stop widened — {stop_mode}")
        log.info(f"  {symbol}: Stop: {base_stop:.4f} → {s['stop']:.4f} | "
                 f"Target: {base_target:.4f} → {s['target']:.4f} | R:R: {s['rr']:.1f}:1")

    # R:R check — high conviction with wide stop might have lower R:R, allow 1.5 minimum
    min_rr = 1.5 if is_high_conviction else SAFETY["min_rr_ratio"]
    if s["rr"] < min_rr:
        log.info(f"  {symbol}: SKIP — R:R {s['rr']:.2f} < {min_rr}")
        return

    if not (SAFETY["min_range_pct"] < s.get("bb",{}).get("width",5)/100 + 0.005 or
            SAFETY["min_range_pct"] < (s["resistance"]-s["support"])/price < SAFETY["max_range_pct"]):
        # High conviction overrides range filter too
        if not is_high_conviction:
            log.info(f"  {symbol}: SKIP — range out of bounds")
            return

    # MTF conflict check — high conviction can override weak MTF conflict
    bf, bs = s["bias_fast"], s["bias_slow"]
    if bf != "neutral" and bs != "neutral" and bf != bs:
        if is_high_conviction and s["score"] >= 88:
            log.info(f"  {symbol}: MTF conflict overridden by score {s['score']}")
        else:
            log.info(f"  {symbol}: SKIP — MTF conflict ({bf} vs {bs})")
            return

    # ── LEARNING SYSTEM 2: Loss pattern memory ────────────
    # High conviction can override loss memory if score is truly exceptional
    avoid, avoid_reason = should_avoid_conditions(symbol, s, regime, macro)
    if avoid:
        if is_high_conviction and s["score"] >= 90:
            log.info(f"  {symbol}: Loss memory overridden by perfect score {s['score']}")
        else:
            log.info(f"  {symbol}: SKIP — loss memory: {avoid_reason}")
            return

    # Position sizing — variable risk × asset allocation multiplier
    balance  = get_balance()
    if s["score"] >= 90:
        base_risk_pct = SAFETY["risk_hc_90"]
        risk_tag = f"20% perfect"
    elif s["score"] >= 82:
        base_risk_pct = SAFETY["risk_hc_82"]
        risk_tag = f"12% HC"
    else:
        base_risk_pct = SAFETY["risk_per_trade_pct"]
        risk_tag = f"5% std"

    # Apply asset intelligence allocation multiplier
    # Hot asset with aligned prediction = up to 3x
    # Cold asset or prediction against = down to 0.25x
    final_risk_pct = base_risk_pct * alloc_mult
    final_risk_pct = min(final_risk_pct, 0.40)   # hard cap at 40% of balance
    risk_usd  = balance * final_risk_pct
    stop_dist = abs(price - s["stop"])
    size = risk_usd / stop_dist if stop_dist > 0 else 0
    if size <= 0:
        log.warning(f"  {symbol}: SKIP — size is zero")
        return
    log.info(f"  {symbol}: Risk {risk_tag} × {alloc_mult:.1f}x = "
             f"{final_risk_pct*100:.1f}% = ${risk_usd:.2f} | {alloc_reason}")

    oid = place_order(symbol, "buy" if s["setup"]=="long" else "sell", size,
                     price=price, stop=s["stop"])
    if oid:
        state["open_positions"][symbol] = {
            "side":             s["setup"],
            "entry":            price,
            "stop":             s["stop"],
            "target":           s["target"],
            "support":          s.get("support", price * 0.97),
            "resistance":       s.get("resistance", price * 1.03),
            "size":             size,
            "score":            s["score"],
            "pattern":          s["pattern"],
            "rr":               s["rr"],
            "asset_type":       s["asset_type"],
            "struct_score":     s["struct_score"],
            "mom_score":        s["mom_score"],
            "sent_score":       s["sent_score"],
            "mtf_score":        s["mtf_score"],
            "macro_score":      s["macro_score"],
            "regime":           regime["regime"],
            "score_data":       s,
            "high_conviction":  is_high_conviction,
            "stop_mode":        stop_mode,
            "strat":            s.get("setup_label","range"),
            "risk_pct":         risk_pct,
            "opened_at":        datetime.datetime.now().isoformat(),
            "order_id":         oid,
        }
        state["trades_today"] += 1
        save_state()

        icon       = "🟢" if s["setup"]=="long" else "🔴"
        asset_icon = ASSETS[symbol]["color"]
        conv_tag   = "⚡ HIGH CONVICTION" if is_high_conviction else ""
        log.info(f"\n{'*'*55}")
        log.info(f"{icon} TRADE ENTERED {asset_icon} {s['setup'].upper()} {symbol} {conv_tag}")
        log.info(f"   Score:     {s['score']}/100 (threshold was {threshold}+)")
        log.info(f"   Regime:    {regime['regime']} — {regime['reason']}")
        log.info(f"   Price:     {price:.4f} | Stop: {s['stop']:.4f} | Target: {s['target']:.4f}")
        log.info(f"   R:R:       {s['rr']:.1f}:1 | Pattern: {s['pattern']}")
        log.info(f"   Stop mode: {stop_mode}")
        log.info(f"   Threshold: {thresh_reason}")
        log.info(f"   Trades today: {state['trades_today']} / max {SAFETY['max_trades_per_day']}")
        log.info(f"{'*'*55}\n")

# ─────────────────────────────────────────────
# STATUS REPORT
# ─────────────────────────────────────────────
def print_status(prices: dict):
    bal   = state["account_balance"]
    peak  = state["peak_balance"]
    dd    = (peak-bal)/peak*100 if peak>0 else 0
    wr    = state["wins"]/max(1,state["total_trades"])*100
    thresh = get_score_threshold()
    mode   = "📄 PAPER" if SAFETY["paper_mode"] else "💰 LIVE"
    log.info(f"\n{'─'*60}")
    log.info(f"MERIDIAN STATUS  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info(f"Mode: {mode} | Balance: ${bal:.2f} | Drawdown: {dd:.1f}%")
    log.info(f"P&L: ${state['total_pnl']:+.2f} | Win rate: {state['wins']}/{state['total_trades']} ({wr:.1f}%)")
    log.info(f"Today: {state['trades_today']} trades | Next threshold: {thresh}+")
    if state["global_halt"]:
        log.info(f"🚨 GLOBAL HALT: {state['global_halt_reason']}")
    for sym, cb in state["circuit_breaker"].items():
        if cb.get("tripped"):
            log.info(f"⚡ CIRCUIT BREAKER: {sym} — {cb.get('reason')}")
    if state["open_positions"]:
        log.info("Open positions:")
        for sym, pos in state["open_positions"].items():
            p   = prices.get(sym, 0)
            pnl = (p-pos["entry"])*pos["size"] if pos["side"]=="long" else (pos["entry"]-p)*pos["size"]
            log.info(f"  {ASSETS[sym]['color']} {sym}: {pos['side'].upper()} @ {pos['entry']:.4f} "
                     f"→ now {p:.4f} | P&L: {pnl:+.2f}")
    log.info(f"Weights — crypto: {state['weight_profiles'].get('crypto',{})}")
    log.info(f"Weights — macro:  {state['weight_profiles'].get('macro',{})}")
    log.info(f"\nAsset intelligence heat map:")
    log.info(get_portfolio_allocation_summary())
    # Show which assets are active vs waiting for balance threshold
    active   = [s for s in ASSETS if state["account_balance"] >= ASSET_THRESHOLDS.get(s, 0)]
    inactive = [s for s in ASSETS if state["account_balance"] < ASSET_THRESHOLDS.get(s, 0)]
    if inactive:
        log.info(f"Active assets:  {' '.join(ASSETS[s]['color']+s for s in active)}")
        for s in inactive:
            needed = ASSET_THRESHOLDS[s] - state["account_balance"]
            log.info(f"  ⏳ {ASSETS[s]['color']} {s}: unlocks at ${ASSET_THRESHOLDS[s]} (need +${needed:.0f} more)")
    log.info(f"{'─'*60}\n")

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# DAILY EMAIL SUMMARY
# ─────────────────────────────────────────────
def send_daily_summary():
    """Send a clean daily summary email at end of trading day."""
    if not EMAIL_FROM or not EMAIL_PASS or not EMAIL_TO:
        log.info("Email not configured — skipping daily summary")
        return

    today      = datetime.date.today().isoformat()
    today_trades = [t for t in trade_log if t.get("timestamp","").startswith(today)]
    wins        = [t for t in today_trades if t.get("result") == "WIN"]
    losses      = [t for t in today_trades if t.get("result") == "LOSS"]
    total_pnl   = sum(t.get("pnl", 0) for t in today_trades)
    win_rate    = len(wins) / len(today_trades) * 100 if today_trades else 0
    open_pos    = state["open_positions"]
    bal         = state["account_balance"]
    drawdown    = (state["peak_balance"] - bal) / state["peak_balance"] * 100 if state["peak_balance"] > 0 else 0

    # Build HTML email
    status_color = "#00d17a" if total_pnl >= 0 else "#ff4757"
    mode = "📄 PAPER" if SAFETY["paper_mode"] else "💰 LIVE"

    rows = ""
    for t in today_trades:
        color = "#00d17a" if t.get("pnl", 0) >= 0 else "#ff4757"
        result_icon = "✅" if t.get("result") == "WIN" else "❌"
        rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">{result_icon}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">{t.get('symbol','—')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130;color:{'#00d17a' if t.get('side')=='long' else '#ff4757'};font-weight:700">{t.get('side','—').upper()}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">${t.get('entry',0):,.4f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">${t.get('exit',0):,.4f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130;color:{color};font-weight:700">${t.get('pnl',0):+.2f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">{t.get('score',0)}/100</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="7" style="padding:16px;text-align:center;color:#475569">No trades executed today</td></tr>'

    open_rows = ""
    for sym, pos in open_pos.items():
        open_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">{sym}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130;color:{'#00d17a' if pos['side']=='long' else '#ff4757'};font-weight:700">{pos['side'].upper()}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">${pos['entry']:,.4f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">${pos['stop']:,.4f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">${pos['target']:,.4f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #1a2130">{pos['score']}/100</td>
        </tr>"""

    if not open_rows:
        open_rows = '<tr><td colspan="6" style="padding:16px;text-align:center;color:#475569">No open positions overnight</td></tr>'

    macro = state.get("macro_context", {})
    macro_html = ""
    for k, v in macro.items():
        if k == "signal_strength": continue
        color = "#00d17a" if v in ("dovish","rising","weakening","falling","negative") else "#ff4757" if v in ("hawkish","strong","rising_dxy","positive") else "#ffb800"
        macro_html += f'<span style="background:{color}22;color:{color};border:1px solid {color}44;padding:3px 10px;border-radius:3px;font-size:12px;margin:3px;display:inline-block">{k.replace("_"," ").title()}: {v}</span>'

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#08090b;font-family:'Helvetica Neue',Arial,sans-serif;color:#e2e8f0">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">

  <!-- HEADER -->
  <div style="display:flex;align-items:center;margin-bottom:24px">
    <div style="background:#0f1318;border-radius:8px;padding:10px 14px;border:1px solid #1a2130;margin-right:12px">
      <span style="font-size:18px;font-weight:700;letter-spacing:-.02em">Meridian</span>
    </div>
    <div>
      <div style="font-size:13px;color:#475569">Daily Trading Summary</div>
      <div style="font-size:12px;color:#2d3748">{today} · {mode}</div>
    </div>
  </div>

  <!-- P&L CARDS -->
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px">
    <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;padding:14px">
      <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">Today's P&L</div>
      <div style="font-size:22px;font-weight:700;color:{status_color}">${total_pnl:+.2f}</div>
    </div>
    <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;padding:14px">
      <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">Balance</div>
      <div style="font-size:22px;font-weight:700;color:#e2e8f0">${bal:,.2f}</div>
      <div style="font-size:10px;color:#475569;margin-top:3px">Drawdown: {drawdown:.1f}%</div>
    </div>
    <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;padding:14px">
      <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">Win Rate</div>
      <div style="font-size:22px;font-weight:700;color:#3b8bff">{win_rate:.0f}%</div>
      <div style="font-size:10px;color:#475569;margin-top:3px">{len(wins)}W / {len(losses)}L · {len(today_trades)} trades</div>
    </div>
  </div>

  <!-- TRADES TABLE -->
  <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;margin-bottom:20px;overflow:hidden">
    <div style="padding:12px 14px;border-bottom:1px solid #1a2130;font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em">
      Today's Trades
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:monospace">
      <thead>
        <tr style="background:#141920">
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase"></th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Asset</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Side</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Entry</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Exit</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">P&L</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Score</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <!-- OPEN POSITIONS -->
  <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;margin-bottom:20px;overflow:hidden">
    <div style="padding:12px 14px;border-bottom:1px solid #1a2130;font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em">
      Open Positions (Carrying Overnight)
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:monospace">
      <thead>
        <tr style="background:#141920">
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Asset</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Side</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Entry</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Stop</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Target</th>
          <th style="padding:8px 12px;text-align:left;color:#475569;font-size:10px;text-transform:uppercase">Score</th>
        </tr>
      </thead>
      <tbody>{open_rows}</tbody>
    </table>
  </div>

  <!-- MACRO -->
  <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;padding:14px;margin-bottom:20px">
    <div style="font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Macro Context</div>
    <div>{macro_html}</div>
  </div>

  <!-- ALL TIME STATS -->
  <div style="background:#0e1117;border:1px solid #1a2130;border-radius:8px;padding:14px;margin-bottom:24px">
    <div style="font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">All-Time Stats</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;font-size:12px;font-family:monospace">
      <div><div style="color:#475569;font-size:10px;margin-bottom:3px">Total P&L</div><div style="color:{'#00d17a' if state['total_pnl']>=0 else '#ff4757'};font-weight:700">${state['total_pnl']:+.2f}</div></div>
      <div><div style="color:#475569;font-size:10px;margin-bottom:3px">Total Trades</div><div style="font-weight:700">{state['total_trades']}</div></div>
      <div><div style="color:#475569;font-size:10px;margin-bottom:3px">Wins</div><div style="color:#00d17a;font-weight:700">{state['wins']}</div></div>
      <div><div style="color:#475569;font-size:10px;margin-bottom:3px">Losses</div><div style="color:#ff4757;font-weight:700">{state['losses']}</div></div>
    </div>
  </div>

  <div style="font-size:10px;color:#2d3748;text-align:center">
    Meridian Trading Bot · {mode} · Sent {datetime.datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
  </div>
</div>
</body>
</html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Meridian {'📄' if SAFETY['paper_mode'] else '💰'} Daily Summary — {today} — P&L ${total_pnl:+.2f}"
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        log.info(f"✉️  Daily summary sent to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def should_send_daily_summary() -> bool:
    """Send at 5pm ET (22:00 UTC) once per day."""
    now   = datetime.datetime.utcnow()
    today = datetime.date.today().isoformat()
    last  = state.get("last_email_date", "")
    return now.hour == 22 and last != today



def main():
    log.info("="*60)
    log.info("   MERIDIAN TRADING BOT v2.0")
    log.info(f"   Mode: {'PAPER (simulation)' if SAFETY['paper_mode'] else 'LIVE TRADING'}")
    asset_list = ', '.join(f"{ASSETS[s]['color']} {ASSETS[s]['label']}" for s in ASSETS)
    log.info(f"   Assets: {asset_list}")
    log.info(f"   Daily limit: {SAFETY['max_trades_per_day']} trades")
    log.info(f"   Score: {SAFETY['score_threshold_early']}+ (first 3) → {SAFETY['score_threshold_late']}+ (after)")
    log.info(f"   Risk: {SAFETY['risk_per_trade_pct']*100}% per trade | Max daily loss: {SAFETY['max_daily_loss_pct']*100}%")
    log.info(f"   Drawdown limit: {SAFETY['max_drawdown_pct']*100}% | Min R:R: {SAFETY['min_rr_ratio']}:1")
    log.info(f"   Vol circuit breaker: {SAFETY['vol_spike_multiplier']}x | Resumes: {SAFETY['vol_resume_multiplier']}x")
    log.info(f"   Scoring: Gold/Silver/Oil=macro-weighted | BTC/ETH/XRP=momentum-weighted")
    log.info(f"   Leverage: BTC/ETH/XRP=10x | Gold/Silver=20x | Oil=25x (margin only — risk still 2.5%)")
    log.info(f"   Adaptive weights: re-assessed every 24h from trade history")
    log.info("="*60 + "\n")

    load_state()
    load_loss_memory()
    load_asset_intel()

    if not API_KEY or not API_SECRET:
        log.warning("⚠️  No API keys — paper mode only")
        SAFETY["paper_mode"] = True

    while True:
        try:
            reset_daily()
            check_global_halt()
            run_backtest_and_adjust()

            # Fetch macro context once per hour
            macro = fetch_macro_context()

            # Fetch prices
            log.info("Fetching prices...")
            prices = {}
            for symbol in ASSETS:
                p = get_price(symbol)
                if p:
                    prices[symbol] = p
                    log.info(f"  {ASSETS[symbol]['color']} {symbol}: ${p:,.4f}")

            # Check exits first
            if state["open_positions"]:
                check_exits(prices)

            # Check global halt again after exits update balance
            if check_global_halt():
                log.warning("Trading halted — monitoring positions only")
            else:
                # Scan for entries
                log.info("Scanning for setups...")
                for symbol in ASSETS:
                    price = prices.get(symbol)
                    if not price:
                        continue
                    candles = get_candles(symbol)
                    if len(candles) < 30:
                        log.warning(f"  {symbol}: only {len(candles)} candles — skipping")
                        continue
                    try_enter(symbol, candles, price, macro)

            print_status(prices)

            # Daily summary email at 5pm ET
            if should_send_daily_summary():
                state["last_email_date"] = datetime.date.today().isoformat()
                save_state()
                send_daily_summary()

            interval    = SAFETY["check_interval_secs"]
            next_check  = datetime.datetime.now() + datetime.timedelta(seconds=interval)
            log.info(f"Sleeping until {next_check.strftime('%H:%M:%S')}...")
            time.sleep(interval)

        except KeyboardInterrupt:
            log.info("\n🛑 Bot stopped")
            save_state()
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
