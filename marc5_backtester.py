"""
Meridian Marc5 Historical Backtester
Run this next to meridian_bot-Marc5.py.

What it does:
- imports the Marc5 strategy functions without starting the live bot
- downloads public Coinbase Exchange candles for crypto symbols
- walks forward through the past N days
- uses the same score_asset(), regime, volatility, threshold, sizing, stop/target logic style
- reports trades, win rate, P&L, drawdown, and ending balance

Notes:
- Public Coinbase Exchange candles support crypto spot pairs best.
- Gold/silver/oil Coinbase derivatives candles may need authenticated Advanced Trade access,
  so this script defaults to BTC, ETH, XRP. Add metals only after candle fetching is confirmed.
- This is a research simulation, not proof of future performance.
"""

import argparse
import csv
import datetime as dt
import importlib.util
import json
import math
import os
import time
from pathlib import Path

import requests


PUBLIC_COINBASE_GRANULARITY = 1800  # 30 minutes
MAX_CANDLES_PER_REQUEST = 300


def load_marc5(path: str):
    spec = importlib.util.spec_from_file_location("marc5", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_coinbase_exchange_candles(symbol: str, days: int):
    """
    Coinbase Exchange public endpoint returns [time, low, high, open, close, volume].
    It allows max about 300 candles per request.
    """
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days + 5)  # warmup buffer for indicators
    step = dt.timedelta(seconds=PUBLIC_COINBASE_GRANULARITY * MAX_CANDLES_PER_REQUEST)

    out = []
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
        params = {
            "start": cur.isoformat(),
            "end": nxt.isoformat(),
            "granularity": PUBLIC_COINBASE_GRANULARITY,
        }
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "MeridianBacktester/1.0"})
        if r.status_code != 200:
            raise RuntimeError(f"{symbol} candle fetch failed {r.status_code}: {r.text[:200]}")
        raw = r.json()
        for c in raw:
            out.append({
                "time": int(c[0]),
                "low": float(c[1]),
                "high": float(c[2]),
                "open": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]) if len(c) > 5 else 0.0,
            })
        cur = nxt
        time.sleep(0.20)

    # de-dupe and sort oldest -> newest
    by_time = {c["time"]: c for c in out}
    candles = [by_time[t] for t in sorted(by_time)]
    cutoff = int((end - dt.timedelta(days=days)).timestamp())
    return candles, cutoff


def default_macro():
    return {
        "fed_stance": "neutral",
        "inflation_trend": "neutral",
        "employment": "neutral",
        "dxy_trend": "neutral",
        "real_rates": "neutral",
        "risk_appetite": "neutral",
        "signal_strength": 0,
    }


def pnl_for(pos, exit_price):
    if pos["side"] == "long":
        return (exit_price - pos["entry"]) * pos["size"]
    return (pos["entry"] - exit_price) * pos["size"]


def simulate_symbol(mod, symbol, candles, cutoff, balance, starting_balance, max_open_positions=2):
    trades = []
    open_pos = None
    peak = balance
    max_dd = 0.0
    macro = default_macro()

    # Reset pieces of Marc5 state that scoring depends on
    mod.state["account_balance"] = balance
    mod.state["peak_balance"] = starting_balance
    mod.state["daily_start_balance"] = starting_balance
    mod.state["open_positions"] = {}
    mod.state["trades_today"] = 0
    mod.state["global_halt"] = False
    mod.state["circuit_breaker"] = {}
    mod.state["weight_profiles"] = mod.WEIGHT_PROFILES.copy()

    # keep enough warmup because Marc5 fetches 80 candles live
    for i in range(80, len(candles)):
        window = candles[i-80:i]
        bar = candles[i]
        price = bar["open"]
        bar_time = dt.datetime.fromtimestamp(bar["time"], dt.timezone.utc)

        # only count trades entered after cutoff, but use previous data as warmup
        after_cutoff = bar["time"] >= cutoff

        # Exit logic: approximate live bot using intrabar stop/target.
        # Marc5 also has thesis exits, but those require live get_candles calls.
        if open_pos is not None:
            hit_stop = False
            hit_target = False

            if open_pos["side"] == "long":
                hit_stop = bar["low"] <= open_pos["stop"]
                hit_target = bar["high"] >= open_pos["target"]
            else:
                hit_stop = bar["high"] >= open_pos["stop"]
                hit_target = bar["low"] <= open_pos["target"]

            if hit_stop or hit_target:
                # conservative: if both hit in same candle, assume stop first
                exit_price = open_pos["stop"] if hit_stop else open_pos["target"]
                pnl = pnl_for(open_pos, exit_price)
                balance += pnl
                peak = max(peak, balance)
                max_dd = max(max_dd, (peak - balance) / peak if peak else 0)
                trades.append({
                    **open_pos,
                    "exit_time": bar_time.isoformat(),
                    "exit": exit_price,
                    "pnl": pnl,
                    "result": "WIN" if pnl > 0 else "LOSS",
                    "balance_after": balance,
                })
                open_pos = None

        # Entry logic
        if open_pos is None and after_cutoff:
            score = mod.score_asset(symbol, window, price, macro)
            threshold = mod.SAFETY["score_threshold_early"]

            if score.get("setup") in ("long", "short") and score.get("score", 0) >= threshold:
                if score.get("rr", 0) >= mod.SAFETY["min_rr_ratio"]:
                    # respect balance gates from Marc5
                    min_bal = mod.ASSET_THRESHOLDS.get(symbol, 0)
                    high_conviction = score["score"] >= 82
                    if balance >= min_bal or high_conviction:
                        size = mod.calc_size(price, score["stop"], balance)
                        if size > 0:
                            open_pos = {
                                "symbol": symbol,
                                "side": score["setup"],
                                "entry_time": bar_time.isoformat(),
                                "entry": price,
                                "stop": score["stop"],
                                "target": score["target"],
                                "size": size,
                                "score": score["score"],
                                "rr": score["rr"],
                                "setup_label": score.get("setup_label", ""),
                            }

    # mark-to-market any open trade at final close
    if open_pos is not None:
        last = candles[-1]
        exit_price = last["close"]
        pnl = pnl_for(open_pos, exit_price)
        balance += pnl
        trades.append({
            **open_pos,
            "exit_time": dt.datetime.fromtimestamp(last["time"], dt.timezone.utc).isoformat(),
            "exit": exit_price,
            "pnl": pnl,
            "result": "OPEN_MTM_WIN" if pnl > 0 else "OPEN_MTM_LOSS",
            "balance_after": balance,
        })

    return trades, balance, max_dd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="meridian_bot-Marc5.py")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--symbols", default="ETH-USD,XRP-USD,BTC-USD")
    ap.add_argument("--out", default="marc5_backtest_results.csv")
    args = ap.parse_args()

    mod = load_marc5(args.bot)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    all_trades = []
    balance = args.balance
    start_balance = args.balance
    max_dd = 0.0

    print(f"Running Marc5 backtest for {args.days} days | start balance ${start_balance:,.2f}")
    print(f"Symbols: {', '.join(symbols)}")
    print()

    for symbol in symbols:
        print(f"Fetching {symbol} candles...")
        candles, cutoff = fetch_coinbase_exchange_candles(symbol, args.days)
        trades, balance_after_symbol, symbol_dd = simulate_symbol(
            mod, symbol, candles, cutoff, args.balance, start_balance
        )
        all_trades.extend(trades)
        max_dd = max(max_dd, symbol_dd)
        print(f"{symbol}: {len(trades)} trades")

    all_trades.sort(key=lambda t: t["entry_time"])
    pnl = sum(t["pnl"] for t in all_trades)
    wins = sum(1 for t in all_trades if t["pnl"] > 0)
    losses = sum(1 for t in all_trades if t["pnl"] <= 0)
    ending = start_balance + pnl
    win_rate = wins / len(all_trades) * 100 if all_trades else 0

    print("\n===== RESULTS =====")
    print(f"Trades: {len(all_trades)}")
    print(f"Wins/Losses: {wins}/{losses}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Net P&L: ${pnl:+.2f}")
    print(f"Return: {pnl/start_balance*100:+.2f}%")
    print(f"Ending balance: ${ending:,.2f}")
    print(f"Approx max drawdown: {max_dd*100:.2f}%")

    if all_trades:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            writer.writeheader()
            writer.writerows(all_trades)
        print(f"\nSaved trades to {args.out}")


if __name__ == "__main__":
    main()
