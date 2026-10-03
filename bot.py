import os
import asyncio
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import pandas as pd
import numpy as np

from telegram import Bot

from database import (
    init_db,
    save_trade,
    get_open_trades,
    update_trade_tp1,
    close_trade,
    get_weekly_performance_data,
)


# ============================================================
# CONFIGURATIONS
# ============================================================

TELEGRAM_BOT_TOKEN = "8983892388:AAG5rvlx_b0C6hIKkElHuQVs5ZlW2Vw89GI"
TELEGRAM_CHAT_ID = "-1004306671705"

# ============================================================
# SCANNER SETTINGS
# ============================================================

TOP_SYMBOLS = 75

OHLCV_1H_LIMIT = 100
OHLCV_5M_LIMIT = 150

SCAN_DELAY = 20

SWING_LEFT = 3
SWING_RIGHT = 3

STRUCTURE_LOOKBACK = 40

MIN_BODY_RATIO = 0.55
MIN_DISPLACEMENT = 0.0010

RETEST_MAX_BARS = 8
RETEST_TOLERANCE = 0.0015

MIN_RISK_PERCENT = 0.0025
MAX_RISK_PERCENT = 0.035

TP1_R = 1.5
TP2_R = 2.5


# ============================================================
# GLOBALS
# ============================================================

sent_setup_ids = set()

bot = Bot(token=TELEGRAM_BOT_TOKEN)

bybit = ccxt.bybit({
    "enableRateLimit": True,
    "options": {
        "defaultType": "linear",
    },
})


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def body_ratio(candle):
    high = safe_float(candle["high"])
    low = safe_float(candle["low"])
    open_price = safe_float(candle["open"])
    close_price = safe_float(candle["close"])

    candle_range = high - low

    if candle_range <= 0:
        return 0.0

    return abs(close_price - open_price) / candle_range


def is_bullish(candle):
    return safe_float(candle["close"]) > safe_float(candle["open"])


def is_bearish(candle):
    return safe_float(candle["close"]) < safe_float(candle["open"])


def displacement(candle):
    open_price = safe_float(candle["open"])
    close_price = safe_float(candle["close"])

    if open_price == 0:
        return 0.0

    return abs(close_price - open_price) / open_price


# ============================================================
# SWING DETECTION
# ============================================================

def find_confirmed_swings(df):
    highs = df["high"].values
    lows = df["low"].values

    swing_highs = []
    swing_lows = []

    left = SWING_LEFT
    right = SWING_RIGHT

    for i in range(left, len(df) - right):
        current_high = highs[i]
        current_low = lows[i]

        left_highs = highs[i - left:i]
        right_highs = highs[i + 1:i + right + 1]

        left_lows = lows[i - left:i]
        right_lows = lows[i + 1:i + right + 1]

        if current_high > max(left_highs) and current_high > max(right_highs):
            swing_highs.append(i)

        if current_low < min(left_lows) and current_low < min(right_lows):
            swing_lows.append(i)

    return swing_highs, swing_lows


# ============================================================
# FVG DETECTION
# ============================================================

def find_bullish_fvg(df, start_idx, end_idx):
    for i in range(start_idx + 2, end_idx + 1):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        c1_high = safe_float(c1["high"])
        c3_low = safe_float(c3["low"])

        if c1_high < c3_low:
            return {
                "type": "bullish_fvg",
                "low": c1_high,
                "high": c3_low,
                "index": i
            }
    return None


def find_bearish_fvg(df, start_idx, end_idx):
    for i in range(start_idx + 2, end_idx + 1):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        c1_low = safe_float(c1["low"])
        c3_high = safe_float(c3["high"])

        if c1_low > c3_high:
            return {
                "type": "bearish_fvg",
                "low": c3_high,
                "high": c1_low,
                "index": i
            }
    return None


# ============================================================
# ORDER BLOCK DETECTION
# ============================================================

def find_bullish_order_block(df, breakout_idx):
    for i in range(breakout_idx - 1, max(-1, breakout_idx - 8), -1):
        candle = df.iloc[i]
        if is_bearish(candle):
            return {
                "type": "bullish_ob",
                "low": safe_float(candle["low"]),
                "high": safe_float(candle["high"]),
                "index": i
            }
    return None


def find_bearish_order_block(df, breakout_idx):
    for i in range(breakout_idx - 1, max(-1, breakout_idx - 8), -1):
        candle = df.iloc[i]
        if is_bullish(candle):
            return {
                "type": "bearish_ob",
                "low": safe_float(candle["low"]),
                "high": safe_float(candle["high"]),
                "index": i
            }
    return None


# ============================================================
# ZONE TOUCH
# ============================================================

def candle_touches_zone(candle, zone_low, zone_high):
    candle_low = safe_float(candle["low"])
    candle_high = safe_float(candle["high"])

    return (
        candle_high >= zone_low
        and candle_low <= zone_high
    )


# ============================================================
# 5M SIGNAL DETECTION
# ============================================================

def detect_5m_signal(df):
    if df is None or len(df) < 60:
        return None

    # CLOSED CANDLES ONLY
    df = df.iloc[:-1].copy()

    if len(df) < 60:
        return None

    df.reset_index(drop=True, inplace=True)

    current_idx = len(df) - 1
    current = df.iloc[current_idx]

    swing_highs, swing_lows = find_confirmed_swings(df)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    recent_swing_highs = [x for x in swing_highs if x < current_idx - SWING_RIGHT]
    recent_swing_lows = [x for x in swing_lows if x < current_idx - SWING_RIGHT]

    # ========================================================
    # BEARISH CHOCH
    # ========================================================
    if len(recent_swing_highs) >= 2 and len(recent_swing_lows) >= 2:
        hh_idx = recent_swing_highs[-1]
        previous_lows = [x for x in recent_swing_lows if x < hh_idx]

        if previous_lows:
            hl_idx = previous_lows[-1]
            protected_high = safe_float(df.iloc[hh_idx]["high"])
            choch_level = safe_float(df.iloc[hl_idx]["low"])

            # Liquidity Sweep
            sweep_start = hh_idx + 1
            sweep_end = current_idx - 2
            bearish_sweep_idx = None

            if sweep_end >= sweep_start:
                for i in range(sweep_start, sweep_end + 1):
                    candle = df.iloc[i]
                    if (
                        safe_float(candle["high"]) > protected_high
                        and safe_float(candle["close"]) < protected_high
                    ):
                        bearish_sweep_idx = i

            # CHoCH Break
            if bearish_sweep_idx is not None:
                breakout_idx = None
                for i in range(bearish_sweep_idx + 1, current_idx):
                    candle = df.iloc[i]
                    close_price = safe_float(candle["close"])

                    if (
                        close_price < choch_level
                        and is_bearish(candle)
                        and body_ratio(candle) >= MIN_BODY_RATIO
                        and displacement(candle) >= MIN_DISPLACEMENT
                    ):
                        breakout_idx = i
                        break

                # Retest
                if breakout_idx is not None:
                    bars_after_break = current_idx - breakout_idx

                    if 1 <= bars_after_break <= RETEST_MAX_BARS:
                        fvg = find_bearish_fvg(df, max(0, breakout_idx - 5), current_idx)
                        ob = find_bearish_order_block(df, breakout_idx)

                        current_high = safe_float(current["high"])
                        current_low = safe_float(current["low"])
                        current_close = safe_float(current["close"])

                        retest_level_touched = (
                            current_high >= choch_level * (1 - RETEST_TOLERANCE)
                            and current_low <= choch_level * (1 + RETEST_TOLERANCE)
                        )

                        zone_touched = False
                        if fvg:
                            zone_touched |= candle_touches_zone(current, fvg["low"], fvg["high"])
                        if ob:
                            zone_touched |= candle_touches_zone(current, ob["low"], ob["high"])

                        confirmation = (
                            retest_level_touched
                            and zone_touched
                            and is_bearish(current)
                            and current_close < choch_level
                            and body_ratio(current) >= 0.40
                        )

                        if confirmation:
                            entry = current_close
                            retest_high = current_high
                            sl = max(protected_high * 1.0035, retest_high * 1.002)
                            risk = sl - entry

                            if risk > 0:
                                risk_percent = risk / entry
                                if MIN_RISK_PERCENT <= risk_percent <= MAX_RISK_PERCENT:
                                    tp1 = entry - risk * TP1_R
                                    tp2 = entry - risk * TP2_R

                                    return {
                                        "side": "SHORT",
                                        "entry": entry,
                                        "sl": sl,
                                        "tp1": tp1,
                                        "tp2": tp2,
                                        "choch": choch_level,
                                        "sweep": protected_high,
                                        "breakout_idx": breakout_idx,
                                        "signal_candle": current_idx,
                                    }

    # ========================================================
    # BULLISH CHOCH
    # ========================================================
    if len(recent_swing_lows) >= 2 and len(recent_swing_highs) >= 2:
        ll_idx = recent_swing_lows[-1]
        previous_highs = [x for x in recent_swing_highs if x < ll_idx]

        if previous_highs:
            lh_idx = previous_highs[-1]
            protected_low = safe_float(df.iloc[ll_idx]["low"])
            choch_level = safe_float(df.iloc[lh_idx]["high"])

            # Liquidity Sweep
            sweep_start = ll_idx + 1
            sweep_end = current_idx - 2
            bullish_sweep_idx = None

            if sweep_end >= sweep_start:
                for i in range(sweep_start, sweep_end + 1):
                    candle = df.iloc[i]
                    if (
                        safe_float(candle["low"]) < protected_low
                        and safe_float(candle["close"]) > protected_low
                    ):
                        bullish_sweep_idx = i

            # CHoCH Break
            if bullish_sweep_idx is not None:
                breakout_idx = None
                for i in range(bullish_sweep_idx + 1, current_idx):
                    candle = df.iloc[i]
                    close_price = safe_float(candle["close"])

                    if (
                        close_price > choch_level
                        and is_bullish(candle)
                        and body_ratio(candle) >= MIN_BODY_RATIO
                        and displacement(candle) >= MIN_DISPLACEMENT
                    ):
                        breakout_idx = i
                        break

                # Retest
                if breakout_idx is not None:
                    bars_after_break = current_idx - breakout_idx

                    if 1 <= bars_after_break <= RETEST_MAX_BARS:
                        fvg = find_bullish_fvg(df, max(0, breakout_idx - 5), current_idx)
                        ob = find_bullish_order_block(df, breakout_idx)

                        current_high = safe_float(current["high"])
                        current_low = safe_float(current["low"])
                        current_close = safe_float(current["close"])

                        retest_level_touched = (
                            current_low <= choch_level * (1 + RETEST_TOLERANCE)
                            and current_high >= choch_level * (1 - RETEST_TOLERANCE)
                        )

                        zone_touched = False
                        if fvg:
                            zone_touched |= candle_touches_zone(current, fvg["low"], fvg["high"])
                        if ob:
                            zone_touched |= candle_touches_zone(current, ob["low"], ob["high"])

                        confirmation = (
                            retest_level_touched
                            and zone_touched
                            and is_bullish(current)
                            and current_close > choch_level
                            and body_ratio(current) >= 0.40
                        )

                        if confirmation:
                            entry = current_close
                            retest_low = current_low
                            sl = min(protected_low * 0.9965, retest_low * 0.998)
                            risk = entry - sl

                            if risk > 0:
                                risk_percent = risk / entry
                                if MIN_RISK_PERCENT <= risk_percent <= MAX_RISK_PERCENT:
                                    tp1 = entry + risk * TP1_R
                                    tp2 = entry + risk * TP2_R

                                    return {
                                        "side": "LONG",
                                        "entry": entry,
                                        "sl": sl,
                                        "tp1": tp1,
                                        "tp2": tp2,
                                        "choch": choch_level,
                                        "sweep": protected_low,
                                        "breakout_idx": breakout_idx,
                                        "signal_candle": current_idx,
                                    }

    return None


# ============================================================
# FETCH TOP 75 SYMBOLS
# ============================================================

async def get_top_symbols():
    try:
        markets = await bybit.load_markets()
        tickers = await bybit.fetch_tickers(params={"category": "linear"})
        usdt_pairs = []

        for symbol, ticker in tickers.items():
            market = markets.get(symbol)
            if not market:
                continue
            if not market.get("linear") or not market.get("swap") or market.get("quote") != "USDT":
                continue

            volume = safe_float(ticker.get("quoteVolume"))
            if volume > 0:
                usdt_pairs.append({"symbol": symbol, "volume": volume})

        usdt_pairs.sort(key=lambda x: x["volume"], reverse=True)
        top_symbols = [item["symbol"] for item in usdt_pairs[:TOP_SYMBOLS]]
        return top_symbols

    except Exception as e:
        print(f"⚠️ get_top_symbols error: {e}", flush=True)
        return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]


# ============================================================
# FETCH OHLCV (WITH RATE LIMIT PROTECTION)
# ============================================================

async def fetch_ohlcv(symbol, timeframe, limit):
    try:
        data = await bybit.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            params={"category": "linear"}
        )

        if not data or len(data) < 55:
            return None

        df = pd.DataFrame(
            data,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    except Exception as e:
        err_msg = str(e)
        if "10006" in err_msg or "Rate Limit" in err_msg:
            print(f"⚠️ Bybit Rate limit hit! Cooling down 10s...", flush=True)
            await asyncio.sleep(10)
        return None


# ============================================================
# TELEGRAM MESSAGE (WITH TRADINGVIEW LINK)
# ============================================================

async def send_signal(symbol, signal):
    side = signal["side"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]

    risk = abs(entry - sl)

    setup_id = f"{symbol}_{side}_{signal['signal_candle']}"

    if setup_id in sent_setup_ids:
        return

    sent_setup_ids.add(setup_id)

    clean_pair = symbol.split(":")[0].replace("/", "")
    tv_chart_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{clean_pair}.P"

    if side == "LONG":
        emoji = "🟢"
        direction = "LONG"
    else:
        emoji = "🔴"
        direction = "SHORT"

    message = f"""
{emoji} <b>SMC WAVE SIGNAL</b>

━━━━━━━━━━━━━━━━━━━━

📊 <b>#{clean_pair}</b>
📈 Direction: <b>{direction}</b>

━━━━━━━━━━━━━━━━━━━━

🎯 <b>ENTRY</b>
<code>{entry:.6f}</code>

🛑 <b>STOP LOSS</b>
<code>{sl:.6f}</code>

🎯 <b>TP1 — 1:1.5</b>
<code>{tp1:.6f}</code>

🚀 <b>TP2 — 1:2.5</b>
<code>{tp2:.6f}</code>

━━━━━━━━━━━━━━━━━━━━

🧠 <b>CONFIRMATIONS</b>
✅ Liquidity Sweep
✅ 5M CHoCH
✅ Displacement
✅ FVG / Order Block
✅ Retest
✅ Closed Candle Confirmation
✅ Protected SL

━━━━━━━━━━━━━━━━━━━━

⚡ <b>Risk:</b> {risk / entry * 100:.2f}%
🕐 <b>Timeframe:</b> 5M
📊 <b>Chart:</b> <a href='{tv_chart_url}'>Open on TradingView ↗</a>

━━━━━━━━━━━━━━━━━━━━
⚠️ Educational / analysis signal
"""

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        # Database එකේ save කිරීම (await කරමින්)
        await save_trade(symbol, "BUY" if side == "LONG" else "SELL", entry, sl, tp1, tp2)

        print(f"📨 SIGNAL SENT → {symbol} {direction}", flush=True)

    except Exception as e:
        print(f"❌ Telegram error: {e}", flush=True)


# ============================================================
# PROCESS SYMBOL
# ============================================================

async def process_symbol(symbol):
    try:
        df_5m = await fetch_ohlcv(symbol, "5m", OHLCV_5M_LIMIT)

        if df_5m is None or len(df_5m) < 60:
            return

        signal = detect_5m_signal(df_5m)

        if signal:
            await send_signal(symbol, signal)

    except Exception as e:
        print(f"❌ Process error {symbol}: {e}", flush=True)


# ============================================================
# MONITOR OPEN TRADES (FIXED ASYNC AWAIT)
# ============================================================

async def monitor_open_trades():
    while True:
        try:
            # FIX: Coroutine was never awaited solved
            trades = await get_open_trades()

            if not trades:
                await asyncio.sleep(10)
                continue

            for trade in trades:
                try:
                    # SQLite Tuple හෝ Dict දෙකටම සහය දැක්වීම
                    if isinstance(trade, dict):
                        t_id = trade["id"]
                        symbol = trade["symbol"]
                        side = trade["side"]
                        entry = safe_float(trade["entry_price"])
                        sl = safe_float(trade["sl_price"])
                        tp1 = safe_float(trade["tp1_price"])
                        tp2 = safe_float(trade["tp2_price"])
                        tp1_hit = trade.get("tp1_hit", 0)
                    else:
                        t_id, symbol, side, entry, sl, tp1, tp2, tp1_hit, _ = trade

                    ticker = await bybit.fetch_ticker(
                        symbol,
                        params={"category": "linear"}
                    )
                    current_price = safe_float(ticker.get("last"))

                    # LONG
                    if side in ["BUY", "LONG"]:
                        if current_price <= sl:
                            await close_trade(t_id, "CLOSED_LOSS")
                            print(f"🛑 {symbol} LONG SL Hit", flush=True)
                            continue

                        if not tp1_hit and current_price >= tp1:
                            await update_trade_tp1(t_id)
                            print(f"🎯 {symbol} LONG TP1 Hit", flush=True)

                        if current_price >= tp2:
                            await close_trade(t_id, "CLOSED_PROFIT")
                            print(f"🚀 {symbol} LONG TP2 Hit", flush=True)

                    # SHORT
                    elif side in ["SELL", "SHORT"]:
                        if current_price >= sl:
                            await close_trade(t_id, "CLOSED_LOSS")
                            print(f"🛑 {symbol} SHORT SL Hit", flush=True)
                            continue

                        if not tp1_hit and current_price <= tp1:
                            await update_trade_tp1(t_id)
                            print(f"🎯 {symbol} SHORT TP1 Hit", flush=True)

                        if current_price <= tp2:
                            await close_trade(t_id, "CLOSED_PROFIT")
                            print(f"🚀 {symbol} SHORT TP2 Hit", flush=True)

                except Exception as e:
                    continue

        except Exception as e:
            print(f"❌ Monitor error: {e}", flush=True)

        await asyncio.sleep(10)


# ============================================================
# WEEKLY REPORT
# ============================================================

async def weekly_report():
    try:
        data = await get_weekly_performance_data()

        if not data:
            return

        total_signals = 0
        total_wins = 0
        total_losses = 0
        total_pnl = 0.0

        lines = []
        lines.append("<code>Day | Sigs | W - L | Win% | Net PnL</code>")
        lines.append("<code>-----------------------------------</code>")

        for date_key, stats in data.items():
            sigs = stats["signals"]
            w = stats["wins"]
            l = stats["losses"]
            pnl = stats["pnl_r"]

            win_rate = int((w / sigs) * 100) if sigs > 0 else 0
            pnl_str = f"+{pnl:.1f}R" if pnl >= 0 else f"{pnl:.1f}R"

            total_signals += sigs
            total_wins += w
            total_losses += l
            total_pnl += pnl

            lines.append(f"<code>{stats['day']:<3} | {sigs:^4} | {w:^2}-{l:^2} | {win_rate:>3}% | {pnl_str:>7}</code>")

        overall_win_rate = int((total_wins / total_signals) * 100) if total_signals > 0 else 0
        tot_pnl_str = f"+{total_pnl:.1f}R" if total_pnl >= 0 else f"{total_pnl:.1f}R"
        status_icon = "🟢" if total_pnl >= 0 else "🔴"

        lines.append("<code>-----------------------------------</code>")
        lines.append(f"<code>TOT | {total_signals:^4} | {total_wins:^2}-{total_losses:^2} | {overall_win_rate:>3}% | {tot_pnl_str:>7}</code>")

        message = (
            f"📊 <b>JK ANALYZING — WEEKLY REPORT</b> 📊\n"
            f"<i>Automated Weekly Performance Sheet</i>\n\n"
            + "\n".join(lines) + "\n\n"
            f"💰 <b>Total Net Return:</b> <code>{tot_pnl_str}</code> {status_icon}\n"
            f"🎯 <b>Accuracy Rate:</b> <code>{overall_win_rate}%</code>\n"
            f"📅 <i>Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}</i>"
        )

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )

    except Exception as e:
        print(f"❌ Weekly report error: {e}", flush=True)


# ============================================================
# WEEKLY REPORT SCHEDULER
# ============================================================

async def weekly_report_scheduler():
    last_report_week = None

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Sunday 23:55 UTC
            if now.weekday() == 6 and now.hour == 23 and now.minute >= 55:
                current_week = (now.year, now.isocalendar().week)

                if current_week != last_report_week:
                    await weekly_report()
                    last_report_week = current_week

        except Exception as e:
            print(f"❌ Scheduler error: {e}", flush=True)

        await asyncio.sleep(30)


# ============================================================
# SCANNER
# ============================================================

async def scanner():
    print("\n🔄 Loading Top 75 USDT pairs...", flush=True)
    symbols = await get_top_symbols()

    if not symbols:
        print("❌ No symbols found.", flush=True)
        return

    print(f"\n📊 Top {len(symbols)} USDT Pairs Loaded\n", flush=True)
    print("🟢 Closed Candle Only | 🛑 Protected SL | 🎯 TP1 1:1.5 | 🚀 TP2 1:2.5", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", flush=True)

    while True:
        try:
            total = len(symbols)

            for index, symbol in enumerate(symbols, start=1):
                clean_name = symbol.split(":")[0]
                print(f"🔍 [{index}/{total}] Scanning {clean_name}...", flush=True)

                await process_symbol(symbol)

                # Rate Limit නොවීම සඳහා ආරක්ෂිත delay එක (0.35s)
                await asyncio.sleep(0.35)

            print(f"\n✅ Scan completed. Next scan in {SCAN_DELAY}s...\n", flush=True)
            await asyncio.sleep(SCAN_DELAY)

            # Refresh Top 75
            try:
                new_symbols = await get_top_symbols()
                if new_symbols:
                    symbols = new_symbols
            except Exception:
                pass

        except Exception as e:
            print(f"❌ Scanner loop error: {e}", flush=True)
            await asyncio.sleep(10)


# ============================================================
# STARTUP MESSAGE
# ============================================================

async def startup_message():
    message = """
🚀 <b>SMC WAVE ANALYZER</b>

━━━━━━━━━━━━━━━━━━━━
🟢 Service Started
📊 Top 75 USDT Pairs
🕐 Timeframe: 5M

🧠 Strategy:
• Liquidity Sweep
• CHoCH + Displacement
• FVG / Order Block Retest
• Closed Candle Confirmation
• Protected SL (TP1 1:1.5 / TP2 1:2.5)
━━━━━━━━━━━━━━━━━━━━
⚡ Scanner is now live...
"""
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        print("✅ Startup message sent to Telegram!", flush=True)
    except Exception as e:
        print(f"❌ Startup Telegram error: {e}", flush=True)


# ============================================================
# MAIN
# ============================================================

async def main():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("🚀 SMC WAVE ANALYZER (TOP 75 USDT)", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    # Initialize database
    try:
        await init_db()
        print("✅ Database initialized successfully", flush=True)
    except Exception as e:
        print(f"❌ Database initialization error: {e}", flush=True)

    await startup_message()

    # Run scanner + monitor + weekly report concurrently
    await asyncio.gather(
        scanner(),
        monitor_open_trades(),
        weekly_report_scheduler()
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Bot stopped by user.", flush=True)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}", flush=True)
    finally:
        try:
            asyncio.run(bybit.close())
        except Exception:
            pass
