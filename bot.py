import os
import asyncio
from datetime import datetime

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

# 🔥 TOP 75 ONLY
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
        "defaultType": "swap",
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
    """
    Bullish FVG:
        Candle 1 high < Candle 3 low
    """

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
    """
    Bearish FVG:
        Candle 1 low > Candle 3 high
    """

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

    # --------------------------------------------------------
    # CLOSED CANDLES ONLY
    # --------------------------------------------------------

    df = df.iloc[:-1].copy()

    if len(df) < 60:
        return None

    df.reset_index(drop=True, inplace=True)

    current_idx = len(df) - 1
    current = df.iloc[current_idx]

    swing_highs, swing_lows = find_confirmed_swings(df)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    # ========================================================
    # BEARISH CHOCH
    # ========================================================

    recent_swing_highs = [
        x for x in swing_highs
        if x < current_idx - SWING_RIGHT
    ]

    recent_swing_lows = [
        x for x in swing_lows
        if x < current_idx - SWING_RIGHT
    ]

    if len(recent_swing_highs) >= 2 and len(recent_swing_lows) >= 2:

        hh_idx = recent_swing_highs[-1]

        previous_lows = [
            x for x in recent_swing_lows
            if x < hh_idx
        ]

        if previous_lows:

            hl_idx = previous_lows[-1]

            protected_high = safe_float(
                df.iloc[hh_idx]["high"]
            )

            choch_level = safe_float(
                df.iloc[hl_idx]["low"]
            )

            # ------------------------------------------------
            # LIQUIDITY SWEEP
            # ------------------------------------------------

            sweep_start = hh_idx + 1
            sweep_end = current_idx - 2

            bearish_sweep_idx = None

            if sweep_end >= sweep_start:

                for i in range(
                    sweep_start,
                    sweep_end + 1
                ):

                    candle = df.iloc[i]

                    if (
                        safe_float(candle["high"])
                        > protected_high
                        and
                        safe_float(candle["close"])
                        < protected_high
                    ):
                        bearish_sweep_idx = i

            # ------------------------------------------------
            # CHOCH BREAK
            # ------------------------------------------------

            if bearish_sweep_idx is not None:

                breakout_idx = None

                for i in range(
                    bearish_sweep_idx + 1,
                    current_idx
                ):

                    candle = df.iloc[i]

                    close_price = safe_float(
                        candle["close"]
                    )

                    if (
                        close_price < choch_level
                        and
                        is_bearish(candle)
                        and
                        body_ratio(candle) >= MIN_BODY_RATIO
                        and
                        displacement(candle) >= MIN_DISPLACEMENT
                    ):
                        breakout_idx = i
                        break

                # ------------------------------------------------
                # RETEST
                # ------------------------------------------------

                if breakout_idx is not None:

                    bars_after_break = (
                        current_idx - breakout_idx
                    )

                    if (
                        1 <= bars_after_break
                        <= RETEST_MAX_BARS
                    ):

                        fvg = find_bearish_fvg(
                            df,
                            max(0, breakout_idx - 5),
                            current_idx
                        )

                        ob = find_bearish_order_block(
                            df,
                            breakout_idx
                        )

                        current_high = safe_float(
                            current["high"]
                        )

                        current_low = safe_float(
                            current["low"]
                        )

                        current_close = safe_float(
                            current["close"]
                        )

                        retest_level_touched = (
                            current_high
                            >= choch_level * (
                                1 - RETEST_TOLERANCE
                            )
                            and
                            current_low
                            <= choch_level * (
                                1 + RETEST_TOLERANCE
                            )
                        )

                        zone_touched = False

                        if fvg:

                            zone_touched |= candle_touches_zone(
                                current,
                                fvg["low"],
                                fvg["high"]
                            )

                        if ob:

                            zone_touched |= candle_touches_zone(
                                current,
                                ob["low"],
                                ob["high"]
                            )

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

                            sl = max(
                                protected_high * 1.0035,
                                retest_high * 1.002
                            )

                            risk = sl - entry

                            if risk > 0:

                                risk_percent = (
                                    risk / entry
                                )

                                if (
                                    MIN_RISK_PERCENT
                                    <= risk_percent
                                    <= MAX_RISK_PERCENT
                                ):

                                    tp1 = (
                                        entry
                                        - risk * TP1_R
                                    )

                                    tp2 = (
                                        entry
                                        - risk * TP2_R
                                    )

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

        previous_highs = [
            x for x in recent_swing_highs
            if x < ll_idx
        ]

        if previous_highs:

            lh_idx = previous_highs[-1]

            protected_low = safe_float(
                df.iloc[ll_idx]["low"]
            )

            choch_level = safe_float(
                df.iloc[lh_idx]["high"]
            )

            # ------------------------------------------------
            # LIQUIDITY SWEEP
            # ------------------------------------------------

            sweep_start = ll_idx + 1
            sweep_end = current_idx - 2

            bullish_sweep_idx = None

            if sweep_end >= sweep_start:

                for i in range(
                    sweep_start,
                    sweep_end + 1
                ):

                    candle = df.iloc[i]

                    if (
                        safe_float(candle["low"])
                        < protected_low
                        and
                        safe_float(candle["close"])
                        > protected_low
                    ):
                        bullish_sweep_idx = i

            # ------------------------------------------------
            # CHOCH BREAK
            # ------------------------------------------------

            if bullish_sweep_idx is not None:

                breakout_idx = None

                for i in range(
                    bullish_sweep_idx + 1,
                    current_idx
                ):

                    candle = df.iloc[i]

                    close_price = safe_float(
                        candle["close"]
                    )

                    if (
                        close_price > choch_level
                        and
                        is_bullish(candle)
                        and
                        body_ratio(candle) >= MIN_BODY_RATIO
                        and
                        displacement(candle) >= MIN_DISPLACEMENT
                    ):
                        breakout_idx = i
                        break

                # ------------------------------------------------
                # RETEST
                # ------------------------------------------------

                if breakout_idx is not None:

                    bars_after_break = (
                        current_idx - breakout_idx
                    )

                    if (
                        1 <= bars_after_break
                        <= RETEST_MAX_BARS
                    ):

                        fvg = find_bullish_fvg(
                            df,
                            max(0, breakout_idx - 5),
                            current_idx
                        )

                        ob = find_bullish_order_block(
                            df,
                            breakout_idx
                        )

                        current_high = safe_float(
                            current["high"]
                        )

                        current_low = safe_float(
                            current["low"]
                        )

                        current_close = safe_float(
                            current["close"]
                        )

                        retest_level_touched = (
                            current_low
                            <= choch_level * (
                                1 + RETEST_TOLERANCE
                            )
                            and
                            current_high
                            >= choch_level * (
                                1 - RETEST_TOLERANCE
                            )
                        )

                        zone_touched = False

                        if fvg:

                            zone_touched |= candle_touches_zone(
                                current,
                                fvg["low"],
                                fvg["high"]
                            )

                        if ob:

                            zone_touched |= candle_touches_zone(
                                current,
                                ob["low"],
                                ob["high"]
                            )

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

                            sl = min(
                                protected_low * 0.9965,
                                retest_low * 0.998
                            )

                            risk = entry - sl

                            if risk > 0:

                                risk_percent = (
                                    risk / entry
                                )

                                if (
                                    MIN_RISK_PERCENT
                                    <= risk_percent
                                    <= MAX_RISK_PERCENT
                                ):

                                    tp1 = (
                                        entry
                                        + risk * TP1_R
                                    )

                                    tp2 = (
                                        entry
                                        + risk * TP2_R
                                    )

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

    markets = await bybit.load_markets()

    tickers = await bybit.fetch_tickers(
        params={
            "category": "linear"
        }
    )

    usdt_pairs = []

    for symbol, ticker in tickers.items():

        try:

            market = markets.get(symbol)

            if not market:
                continue

            if not market.get("linear"):
                continue

            if not market.get("swap"):
                continue

            if market.get("quote") != "USDT":
                continue

            volume = safe_float(
                ticker.get("quoteVolume")
            )

            if volume <= 0:
                continue

            usdt_pairs.append({
                "symbol": symbol,
                "volume": volume
            })

        except Exception:
            continue

    # --------------------------------------------------------
    # SORT BY 24H USDT VOLUME
    # --------------------------------------------------------

    usdt_pairs.sort(
        key=lambda x: x["volume"],
        reverse=True
    )

    # 🔥 TOP 75 ONLY
    top_symbols = [
        item["symbol"]
        for item in usdt_pairs[:TOP_SYMBOLS]
    ]

    return top_symbols


# ============================================================
# FETCH OHLCV
# ============================================================

async def fetch_ohlcv(symbol, timeframe, limit):

    try:

        data = await bybit.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            params={
                "category": "linear"
            }
        )

        if not data:
            return None

        df = pd.DataFrame(
            data,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            unit="ms"
        )

        return df

    except Exception as e:

        print(
            f"❌ OHLCV error {symbol} {timeframe}: {e}"
        )

        return None


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

async def send_signal(symbol, signal):

    side = signal["side"]

    entry = signal["entry"]
    sl = signal["sl"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]

    risk = abs(entry - sl)

    setup_id = (
        f"{symbol}_"
        f"{side}_"
        f"{signal['signal_candle']}"
    )

    if setup_id in sent_setup_ids:
        return

    sent_setup_ids.add(setup_id)

    if side == "LONG":

        emoji = "🟢"
        direction = "LONG"

    else:

        emoji = "🔴"
        direction = "SHORT"

    message = f"""
{emoji} <b>SMC WAVE SIGNAL</b>

━━━━━━━━━━━━━━━━━━━━

📊 <b>{symbol}</b>
📈 Direction: <b>{direction}</b>

━━━━━━━━━━━━━━━━━━━━

🎯 <b>ENTRY</b>
<code>{entry:.8f}</code>

🛑 <b>STOP LOSS</b>
<code>{sl:.8f}</code>

🎯 <b>TP1 — 1:1.5</b>
<code>{tp1:.8f}</code>

🚀 <b>TP2 — 1:2.5</b>
<code>{tp2:.8f}</code>

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

⚡ <b>Risk</b>
{risk / entry * 100:.2f}%

🕐 <b>Timeframe:</b> 5M

━━━━━━━━━━━━━━━━━━━━

⚠️ Educational / analysis signal
"""

    try:

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )

        print(
            f"📨 SIGNAL SENT → {symbol} {direction}"
        )

    except Exception as e:

        print(
            f"❌ Telegram error: {e}"
        )


# ============================================================
# PROCESS SYMBOL
# ============================================================

async def process_symbol(symbol):

    try:

        df_5m = await fetch_ohlcv(
            symbol,
            "5m",
            OHLCV_5M_LIMIT
        )

        if df_5m is None:
            return

        if len(df_5m) < 60:
            return

        signal = detect_5m_signal(
            df_5m
        )

        if signal:

            await send_signal(
                symbol,
                signal
            )

    except Exception as e:

        print(
            f"❌ Process error {symbol}: {e}"
        )


# ============================================================
# MONITOR OPEN TRADES
# ============================================================

async def monitor_open_trades():

    while True:

        try:

            trades = get_open_trades()

            if not trades:
                await asyncio.sleep(10)
                continue

            for trade in trades:

                try:

                    symbol = trade["symbol"]

                    side = trade["side"]

                    entry = safe_float(
                        trade["entry_price"]
                    )

                    sl = safe_float(
                        trade["sl_price"]
                    )

                    tp1 = safe_float(
                        trade["tp1_price"]
                    )

                    tp2 = safe_float(
                        trade["tp2_price"]
                    )

                    ticker = await bybit.fetch_ticker(
                        symbol,
                        params={
                            "category": "linear"
                        }
                    )

                    current_price = safe_float(
                        ticker.get("last")
                    )

                    # ------------------------------------------------
                    # LONG
                    # ------------------------------------------------

                    if side == "LONG":

                        if current_price <= sl:

                            close_trade(
                                trade["id"],
                                "SL"
                            )

                            print(
                                f"🛑 {symbol} LONG SL"
                            )

                            continue

                        if (
                            not trade.get("tp1_hit")
                            and current_price >= tp1
                        ):

                            update_trade_tp1(
                                trade["id"]
                            )

                            print(
                                f"🎯 {symbol} LONG TP1"
                            )

                        if current_price >= tp2:

                            close_trade(
                                trade["id"],
                                "TP2"
                            )

                            print(
                                f"🚀 {symbol} LONG TP2"
                            )

                    # ------------------------------------------------
                    # SHORT
                    # ------------------------------------------------

                    elif side == "SHORT":

                        if current_price >= sl:

                            close_trade(
                                trade["id"],
                                "SL"
                            )

                            print(
                                f"🛑 {symbol} SHORT SL"
                            )

                            continue

                        if (
                            not trade.get("tp1_hit")
                            and current_price <= tp1
                        ):

                            update_trade_tp1(
                                trade["id"]
                            )

                            print(
                                f"🎯 {symbol} SHORT TP1"
                            )

                        if current_price <= tp2:

                            close_trade(
                                trade["id"],
                                "TP2"
                            )

                            print(
                                f"🚀 {symbol} SHORT TP2"
                            )

                except Exception as e:

                    print(
                        f"❌ Trade monitor error: {e}"
                    )

        except Exception as e:

            print(
                f"❌ Monitor error: {e}"
            )

        await asyncio.sleep(5)


# ============================================================
# WEEKLY REPORT
# ============================================================

async def weekly_report():

    try:

        data = get_weekly_performance_data()

        if not data:
            return

        total_trades = data.get(
            "total_trades",
            0
        )

        wins = data.get(
            "wins",
            0
        )

        losses = data.get(
            "losses",
            0
        )

        pnl = data.get(
            "pnl",
            0
        )

        win_rate = 0

        if total_trades > 0:

            win_rate = (
                wins / total_trades
            ) * 100

        message = f"""
📊 <b>WEEKLY PERFORMANCE</b>

━━━━━━━━━━━━━━━━━━━━

📅 Weekly Report

📈 Total Trades:
<b>{total_trades}</b>

✅ Wins:
<b>{wins}</b>

❌ Losses:
<b>{losses}</b>

🎯 Win Rate:
<b>{win_rate:.2f}%</b>

💰 PnL:
<b>{pnl:.2f}</b>

━━━━━━━━━━━━━━━━━━━━
"""

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"❌ Weekly report error: {e}"
        )


# ============================================================
# WEEKLY REPORT SCHEDULER
# ============================================================

async def weekly_report_scheduler():

    last_report_week = None

    while True:

        try:

            now = datetime.utcnow()

            # Sunday 23:55 UTC

            if (
                now.weekday() == 6
                and now.hour == 23
                and now.minute >= 55
            ):

                current_week = (
                    now.year,
                    now.isocalendar().week
                )

                if current_week != last_report_week:

                    await weekly_report()

                    last_report_week = current_week

        except Exception as e:

            print(
                f"❌ Scheduler error: {e}"
            )

        await asyncio.sleep(30)


# ============================================================
# SCANNER
# ============================================================

async def scanner():

    print(
        "\n🔄 Loading Top 75 USDT pairs..."
    )

    symbols = await get_top_symbols()

    if not symbols:

        print(
            "❌ No symbols found."
        )

        return

    print(
        f"\n📊 Top {len(symbols)} USDT Pairs\n"
    )

    print(
        "🟢 Closed Candle Only"
    )

    print(
        "🛑 Protected SL"
    )

    print(
        "🎯 TP1 1:1.5"
    )

    print(
        "🚀 TP2 1:2.5"
    )

    print(
        "🧠 5M CHoCH + Retest"
    )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    while True:

        try:

            total = len(symbols)

            for index, symbol in enumerate(
                symbols,
                start=1
            ):

                print(
                    f"🔍 [{index}/{total}] "
                    f"Scanning {symbol}..."
                )

                await process_symbol(
                    symbol
                )

                await asyncio.sleep(
                    0.15
                )

            print(
                "\n✅ Scan completed."
            )

            print(
                f"⏳ Next scan in {SCAN_DELAY}s..."
            )

            await asyncio.sleep(
                SCAN_DELAY
            )

            # Refresh Top 75 periodically

            try:

                new_symbols = (
                    await get_top_symbols()
                )

                if new_symbols:

                    symbols = new_symbols

            except Exception as e:

                print(
                    f"⚠️ Symbol refresh error: {e}"
                )

        except Exception as e:

            print(
                f"❌ Scanner error: {e}"
            )

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
• CHoCH
• Displacement
• FVG
• Order Block
• Retest
• Closed Candle

🛑 Protected SL

🎯 TP1 → 1:1.5

🚀 TP2 → 1:2.5

━━━━━━━━━━━━━━━━━━━━

⚡ Scanner is now live...
"""

    try:

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"❌ Startup Telegram error: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        "🚀 SMC WAVE ANALYZER"
    )

    print(
        "📊 TOP 75 USDT FUTURES"
    )

    print(
        "🧠 5M CHoCH + RETEST"
    )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # Initialize database

    try:

        init_db()

        print(
            "✅ Database initialized"
        )

    except Exception as e:

        print(
            f"❌ Database initialization error: {e}"
        )

    await startup_message()

    # Run scanner + monitor + weekly report

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

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "\n🛑 Bot stopped by user."
        )

    except Exception as e:

        print(
            f"\n❌ Fatal error: {e}"
        )

    finally:

        try:

            asyncio.run(
                bybit.close()
            )

        except Exception:
            pass
