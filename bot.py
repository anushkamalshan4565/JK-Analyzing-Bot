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
# CONFIGURATION
# ============================================================

# IMPORTANT:
# Generate a NEW Telegram token if your previous token
# was exposed publicly.
#
# Windows CMD:
# set TELEGRAM_BOT_TOKEN=YOUR_NEW_TOKEN
#
# PowerShell:
# $env:TELEGRAM_BOT_TOKEN="YOUR_NEW_TOKEN"

TELEGRAM_BOT_TOKEN = os.getenv("8983892388:AAG5rvlx_b0C6hIKkElHuQVs5ZlW2Vw89GI")

TELEGRAM_CHAT_ID = "-1004306671705"


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN environment variable is not set."
    )


# ============================================================
# BYBIT
# ============================================================

bybit = ccxt.bybit(
    {
        "enableRateLimit": True,
        "options": {
            "defaultType": "linear",
        },
    }
)


tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)


# ============================================================
# SCANNER SETTINGS
# ============================================================

TOP_SYMBOLS = 100

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
# DUPLICATE SETUP PROTECTION
# ============================================================

sent_setup_ids = set()


# ============================================================
# EMA
# ============================================================

def calculate_ema(series, length):

    return series.ewm(
        span=length,
        adjust=False
    ).mean()


# ============================================================
# CCI
# ============================================================

def calculate_cci(df, length):

    typical_price = (
        df["high"]
        + df["low"]
        + df["close"]
    ) / 3.0

    sma = typical_price.rolling(
        window=length
    ).mean()

    mad = typical_price.rolling(
        window=length
    ).apply(
        lambda x: np.mean(
            np.abs(
                x - np.mean(x)
            )
        ),
        raw=True,
    )

    mad = mad.replace(
        0,
        0.00001
    )

    cci = (
        (typical_price - sma)
        / (0.015 * mad)
    )

    return cci


# ============================================================
# GET TOP 100 USDT PERPETUAL PAIRS
# ============================================================

async def get_top_symbols():

    try:

        markets = await bybit.load_markets()

        tickers = await bybit.fetch_tickers(
            params={
                "category": "linear"
            }
        )

        usdt_pairs = []

        for symbol, ticker in tickers.items():

            market = markets.get(symbol)

            if not market:
                continue

            if not market.get("linear"):
                continue

            if not market.get("contract"):
                continue

            if market.get("settle") != "USDT":
                continue

            quote_volume = (
                ticker.get("quoteVolume")
                or 0
            )

            try:

                quote_volume = float(
                    quote_volume
                )

            except Exception:

                quote_volume = 0

            if quote_volume <= 0:
                continue

            usdt_pairs.append(
                {
                    "symbol": symbol,
                    "volume": quote_volume,
                }
            )

        usdt_pairs.sort(
            key=lambda x: x["volume"],
            reverse=True
        )

        top_symbols = [
            item["symbol"]
            for item in usdt_pairs[:TOP_SYMBOLS]
        ]

        if top_symbols:

            print(
                f"✅ Loaded "
                f"{len(top_symbols)} "
                f"Bybit USDT perpetual pairs."
            )

            return top_symbols

    except Exception as e:

        print(
            f"⚠️ Market fetch error: {e}"
        )

    return [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "XRP/USDT:USDT",
        "DOGE/USDT:USDT",
    ]


# ============================================================
# FETCH OHLCV
# ============================================================

async def fetch_ohlcv(
    symbol,
    timeframe,
    limit=100
):

    try:

        raw = await bybit.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            params={
                "category": "linear"
            },
        )

        if not raw:
            return None

        if len(raw) < 55:
            return None

        df = pd.DataFrame(
            raw,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ],
        )

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        for column in numeric_columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

        df = df.dropna().reset_index(
            drop=True
        )

        return df

    except Exception as e:

        print(
            f"\n⚠️ OHLCV error "
            f"{symbol} {timeframe}: {e}"
        )

        return None


# ============================================================
# 1H TREND / BIAS
# ============================================================

def analyze_1h_indicators(df_1h):

    if df_1h is None:
        return "NEUTRAL"

    if len(df_1h) < 55:
        return "NEUTRAL"

    df = df_1h.copy()

    df["EMA50"] = calculate_ema(
        df["close"],
        50
    )

    df["CCI50"] = calculate_cci(
        df,
        50
    )

    df["CCI7"] = calculate_cci(
        df,
        7
    )

    # Last CLOSED 1H candle
    last = df.iloc[-2]

    recent_cci7 = (
        df["CCI7"]
        .iloc[-5:-1]
    )

    if len(recent_cci7) < 3:
        return "NEUTRAL"

    # ========================================================
    # BULLISH
    # ========================================================

    bullish = (

        last["close"]
        > last["EMA50"]

        and

        last["CCI50"]
        > 0

        and

        recent_cci7.min()
        < -50

        and

        last["CCI7"]
        > -30
    )

    # ========================================================
    # BEARISH
    # ========================================================

    bearish = (

        last["close"]
        < last["EMA50"]

        and

        last["CCI50"]
        < 0

        and

        recent_cci7.max()
        > 50

        and

        last["CCI7"]
        < 30
    )

    if bullish:
        return "BULLISH"

    if bearish:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# CONFIRMED SWINGS
# ============================================================

def find_confirmed_swings(
    df,
    left=3,
    right=3
):

    highs = df["high"].values
    lows = df["low"].values

    swing_highs = []
    swing_lows = []

    for i in range(
        left,
        len(df) - right
    ):

        is_high = (
            all(
                highs[i] > highs[i - k]
                for k in range(1, left + 1)
            )
            and
            all(
                highs[i] > highs[i + k]
                for k in range(1, right + 1)
            )
        )

        is_low = (
            all(
                lows[i] < lows[i - k]
                for k in range(1, left + 1)
            )
            and
            all(
                lows[i] < lows[i + k]
                for k in range(1, right + 1)
            )
        )

        if is_high:
            swing_highs.append(i)

        if is_low:
            swing_lows.append(i)

    return swing_highs, swing_lows


# ============================================================
# BODY RATIO
# ============================================================

def candle_body_ratio(
    open_price,
    high_price,
    low_price,
    close_price
):

    candle_range = (
        high_price - low_price
    )

    if candle_range <= 0:
        return 0

    body = abs(
        close_price - open_price
    )

    return body / candle_range


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_bullish_liquidity_sweep(
    df,
    start_idx,
    end_idx,
    protected_low
):

    lows = df["low"].values
    closes = df["close"].values

    for i in range(
        start_idx,
        end_idx + 1
    ):

        swept = (
            lows[i]
            < protected_low
        )

        recovered = (
            closes[i]
            > protected_low
        )

        if swept and recovered:
            return i

    return None


def detect_bearish_liquidity_sweep(
    df,
    start_idx,
    end_idx,
    protected_high
):

    highs = df["high"].values
    closes = df["close"].values

    for i in range(
        start_idx,
        end_idx + 1
    ):

        swept = (
            highs[i]
            > protected_high
        )

        recovered = (
            closes[i]
            < protected_high
        )

        if swept and recovered:
            return i

    return None


# ============================================================
# FVG DETECTION
# ============================================================

def find_bullish_fvg(
    df,
    start_idx,
    end_idx
):

    highs = df["high"].values
    lows = df["low"].values

    zones = []

    for i in range(
        max(2, start_idx),
        end_idx + 1
    ):

        # Bullish FVG
        if lows[i] > highs[i - 2]:

            zones.append(
                {
                    "index": i,
                    "low": highs[i - 2],
                    "high": lows[i],
                }
            )

    return zones


def find_bearish_fvg(
    df,
    start_idx,
    end_idx
):

    highs = df["high"].values
    lows = df["low"].values

    zones = []

    for i in range(
        max(2, start_idx),
        end_idx + 1
    ):

        # Bearish FVG
        if highs[i] < lows[i - 2]:

            zones.append(
                {
                    "index": i,
                    "low": highs[i],
                    "high": lows[i - 2],
                }
            )

    return zones


# ============================================================
# ORDER BLOCK
# ============================================================

def find_bullish_order_block(
    df,
    breakout_idx
):

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    # Last bearish candle before bullish breakout

    start = breakout_idx - 1
    stop = max(
        -1,
        breakout_idx - 8
    )

    for i in range(
        start,
        stop,
        -1
    ):

        if closes[i] < opens[i]:

            return {
                "index": i,
                "low": lows[i],
                "high": highs[i],
            }

    return None


def find_bearish_order_block(
    df,
    breakout_idx
):

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    # Last bullish candle before bearish breakout

    start = breakout_idx - 1
    stop = max(
        -1,
        breakout_idx - 8
    )

    for i in range(
        start,
        stop,
        -1
    ):

        if closes[i] > opens[i]:

            return {
                "index": i,
                "low": lows[i],
                "high": highs[i],
            }

    return None


# ============================================================
# ZONE TOUCH
# ============================================================

def price_touches_zone(
    candle_high,
    candle_low,
    zone_low,
    zone_high,
    tolerance=0.0015
):

    if zone_low > zone_high:
        zone_low, zone_high = (
            zone_high,
            zone_low
        )

    expanded_low = (
        zone_low
        * (1 - tolerance)
    )

    expanded_high = (
        zone_high
        * (1 + tolerance)
    )

    return (
        candle_high >= expanded_low
        and
        candle_low <= expanded_high
    )


# ============================================================
# 5M CHoCH + SWEEP + FVG/OB + RETEST
# ============================================================

def check_5m_choch_and_retest(
    df_5m,
    bias_1h
):

    """
    FINAL 5M FLOW

    1. Closed candles only
    2. Confirmed swing structure
    3. Liquidity sweep
    4. Strong CHoCH body break
    5. Displacement
    6. FVG OR Order Block
    7. CURRENT CLOSED CANDLE retest
    8. Rejection candle
    9. Protected SL
    10. TP1 / TP2
    """

    empty_result = (
        None,
        None,
        None,
        None,
        None,
        None,
    )

    if df_5m is None:
        return empty_result

    if len(df_5m) < 80:
        return empty_result

    # ========================================================
    # REMOVE CURRENT FORMING CANDLE
    # ========================================================

    df = df_5m.iloc[:-1].copy()

    df.reset_index(
        drop=True,
        inplace=True
    )

    if len(df) < 70:
        return empty_result

    current_idx = len(df) - 1

    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    closes = df["close"].values

    # ========================================================
    # SWINGS
    # ========================================================

    swing_highs, swing_lows = (
        find_confirmed_swings(
            df,
            SWING_LEFT,
            SWING_RIGHT
        )
    )

    if (
        len(swing_highs) < 2
        or
        len(swing_lows) < 2
    ):
        return empty_result

    recent_highs = [
        i
        for i in swing_highs
        if i >= current_idx - 50
    ]

    recent_lows = [
        i
        for i in swing_lows
        if i >= current_idx - 50
    ]

    if (
        len(recent_highs) < 2
        or
        len(recent_lows) < 2
    ):
        return empty_result

    # ========================================================
    # BEARISH SETUP
    # ========================================================

    if bias_1h == "BEARISH":

        hh_idx = recent_highs[-1]

        previous_lows = [
            i
            for i in recent_lows
            if i < hh_idx
        ]

        if not previous_lows:
            return empty_result

        hl_idx = previous_lows[-1]

        protected_high = highs[hh_idx]

        choch_level = lows[hl_idx]

        # ====================================================
        # LIQUIDITY SWEEP
        # ====================================================

        sweep_start = max(
            hl_idx + 1,
            current_idx - STRUCTURE_LOOKBACK
        )

        # Don't use the current closed candle as sweep
        # because it must become the retest/rejection candle.
        sweep_end = current_idx - 2

        if sweep_end <= sweep_start:
            return empty_result

        sweep_idx = (
            detect_bearish_liquidity_sweep(
                df,
                sweep_start,
                sweep_end,
                protected_high
            )
        )

        if sweep_idx is None:
            return empty_result

        # ====================================================
        # CHoCH BREAK
        # ====================================================

        breakout_idx = None

        search_start = max(
            sweep_idx + 1,
            current_idx - STRUCTURE_LOOKBACK
        )

        for k in range(
            search_start,
            current_idx
        ):

            body_ratio = candle_body_ratio(
                opens[k],
                highs[k],
                lows[k],
                closes[k]
            )

            if (
                closes[k] < choch_level
                and
                closes[k] < opens[k]
                and
                body_ratio >= MIN_BODY_RATIO
            ):

                displacement = (
                    choch_level - closes[k]
                ) / choch_level

                if (
                    displacement
                    >= MIN_DISPLACEMENT
                ):

                    breakout_idx = k
                    break

        if breakout_idx is None:
            return empty_result

        # ====================================================
        # FVG
        # ====================================================

        fvg_zones = find_bearish_fvg(
            df,
            max(
                2,
                breakout_idx - 5
            ),
            breakout_idx
        )

        fvg = (
            fvg_zones[-1]
            if fvg_zones
            else None
        )

        # ====================================================
        # ORDER BLOCK
        # ====================================================

        order_block = (
            find_bearish_order_block(
                df,
                breakout_idx
            )
        )

        # At least ONE zone must exist.
        if (
            fvg is None
            and
            order_block is None
        ):
            return empty_result

        # ====================================================
        # RETEST WINDOW
        # ====================================================

        bars_after_break = (
            current_idx
            - breakout_idx
        )

        if bars_after_break < 1:
            return empty_result

        if bars_after_break > RETEST_MAX_BARS:
            return empty_result

        # ====================================================
        # ONLY CURRENT CLOSED CANDLE
        # ====================================================

        r = current_idx

        retest_high = highs[r]
        retest_low = lows[r]

        retest_open = opens[r]
        retest_close = closes[r]

        # ====================================================
        # CHoCH RETEST
        # ====================================================

        choch_touch = price_touches_zone(
            retest_high,
            retest_low,
            choch_level,
            choch_level,
            RETEST_TOLERANCE
        )

        if not choch_touch:
            return empty_result

        # ====================================================
        # FVG / OB RETEST
        #
        # IMPORTANT:
        # FVG OR OB
        # NOT FVG AND OB
        # ====================================================

        fvg_touch = False
        ob_touch = False

        if fvg is not None:

            fvg_touch = price_touches_zone(
                retest_high,
                retest_low,
                fvg["low"],
                fvg["high"],
                RETEST_TOLERANCE
            )

        if order_block is not None:

            ob_touch = price_touches_zone(
                retest_high,
                retest_low,
                order_block["low"],
                order_block["high"],
                RETEST_TOLERANCE
            )

        if not (
            fvg_touch
            or
            ob_touch
        ):
            return empty_result

        # ====================================================
        # BEARISH REJECTION
        # ====================================================

        candle_range = (
            retest_high
            - retest_low
        )

        if candle_range <= 0:
            return empty_result

        body = abs(
            retest_close
            - retest_open
        )

        upper_wick = (
            retest_high
            - max(
                retest_open,
                retest_close
            )
        )

        bearish_close = (
            retest_close
            < retest_open
        )

        body_ratio = (
            body
            / candle_range
        )

        rejection = (
            bearish_close
            and
            retest_close
            < choch_level
            and
            body_ratio >= 0.40
            and
            upper_wick >= body * 0.30
        )

        if not rejection:
            return empty_result

        # ====================================================
        # ENTRY
        # ====================================================

        entry = float(
            retest_close
        )

        # Protected SL
        structure_sl = (
            protected_high
            * 1.0035
        )

        retest_sl = (
            retest_high
            * 1.0020
        )

        sl = max(
            structure_sl,
            retest_sl
        )

        risk = sl - entry

        if risk <= 0:
            return empty_result

        risk_percent = (
            risk / entry
        )

        if not (
            MIN_RISK_PERCENT
            <= risk_percent
            <= MAX_RISK_PERCENT
        ):
            return empty_result

        tp1 = (
            entry
            - risk * TP1_R
        )

        tp2 = (
            entry
            - risk * TP2_R
        )

        setup_id = (
            f"SELL|"
            f"{int(df.iloc[breakout_idx]['timestamp'])}|"
            f"{int(df.iloc[r]['timestamp'])}"
        )

        return (
            "SELL",
            round(entry, 8),
            round(sl, 8),
            round(tp1, 8),
            round(tp2, 8),
            setup_id,
        )

    # ========================================================
    # BULLISH SETUP
    # ========================================================

    elif bias_1h == "BULLISH":

        ll_idx = recent_lows[-1]

        previous_highs = [
            i
            for i in recent_highs
            if i < ll_idx
        ]

        if not previous_highs:
            return empty_result

        lh_idx = previous_highs[-1]

        protected_low = lows[ll_idx]

        choch_level = highs[lh_idx]

        # ====================================================
        # LIQUIDITY SWEEP
        # ====================================================

        sweep_start = max(
            lh_idx + 1,
            current_idx - STRUCTURE_LOOKBACK
        )

        sweep_end = current_idx - 2

        if sweep_end <= sweep_start:
            return empty_result

        sweep_idx = (
            detect_bullish_liquidity_sweep(
                df,
                sweep_start,
                sweep_end,
                protected_low
            )
        )

        if sweep_idx is None:
            return empty_result

        # ====================================================
        # CHoCH BREAK
        # ====================================================

        breakout_idx = None

        search_start = max(
            sweep_idx + 1,
            current_idx - STRUCTURE_LOOKBACK
        )

        for k in range(
            search_start,
            current_idx
        ):

            body_ratio = candle_body_ratio(
                opens[k],
                highs[k],
                lows[k],
                closes[k]
            )

            if (
                closes[k] > choch_level
                and
                closes[k] > opens[k]
                and
                body_ratio >= MIN_BODY_RATIO
            ):

                displacement = (
                    closes[k] - choch_level
                ) / choch_level

                if (
                    displacement
                    >= MIN_DISPLACEMENT
                ):

                    breakout_idx = k
                    break

        if breakout_idx is None:
            return empty_result

        # ====================================================
        # FVG
        # ====================================================

        fvg_zones = find_bullish_fvg(
            df,
            max(
                2,
                breakout_idx - 5
            ),
            breakout_idx
        )

        fvg = (
            fvg_zones[-1]
            if fvg_zones
            else None
        )

        # ====================================================
        # ORDER BLOCK
        # ====================================================

        order_block = (
            find_bullish_order_block(
                df,
                breakout_idx
            )
        )

        # At least ONE zone must exist.
        if (
            fvg is None
            and
            order_block is None
        ):
            return empty_result

        # ====================================================
        # RETEST WINDOW
        # ====================================================

        bars_after_break = (
            current_idx
            - breakout_idx
        )

        if bars_after_break < 1:
            return empty_result

        if bars_after_break > RETEST_MAX_BARS:
            return empty_result

        # ====================================================
        # ONLY CURRENT CLOSED CANDLE
        # ====================================================

        r = current_idx

        retest_high = highs[r]
        retest_low = lows[r]

        retest_open = opens[r]
        retest_close = closes[r]

        # ====================================================
        # CHoCH RETEST
        # ====================================================

        choch_touch = price_touches_zone(
            retest_high,
            retest_low,
            choch_level,
            choch_level,
            RETEST_TOLERANCE
        )

        if not choch_touch:
            return empty_result

        # ====================================================
        # FVG / OB RETEST
        #
        # IMPORTANT:
        # FVG OR OB
        # ====================================================

        fvg_touch = False
        ob_touch = False

        if fvg is not None:

            fvg_touch = price_touches_zone(
                retest_high,
                retest_low,
                fvg["low"],
                fvg["high"],
                RETEST_TOLERANCE
            )

        if order_block is not None:

            ob_touch = price_touches_zone(
                retest_high,
                retest_low,
                order_block["low"],
                order_block["high"],
                RETEST_TOLERANCE
            )

        if not (
            fvg_touch
            or
            ob_touch
        ):
            return empty_result

        # ====================================================
        # BULLISH REJECTION
        # ====================================================

        candle_range = (
            retest_high
            - retest_low
        )

        if candle_range <= 0:
            return empty_result

        body = abs(
            retest_close
            - retest_open
        )

        lower_wick = (
            min(
                retest_open,
                retest_close
            )
            - retest_low
        )

        bullish_close = (
            retest_close
            > retest_open
        )

        body_ratio = (
            body
            / candle_range
        )

        rejection = (
            bullish_close
            and
            retest_close
            > choch_level
            and
            body_ratio >= 0.40
            and
            lower_wick >= body * 0.30
        )

        if not rejection:
            return empty_result

        # ====================================================
        # ENTRY
        # ====================================================

        entry = float(
            retest_close
        )

        # Protected SL
        structure_sl = (
            protected_low
            * 0.9965
        )

        retest_sl = (
            retest_low
            * 0.9980
        )

        sl = min(
            structure_sl,
            retest_sl
        )

        risk = entry - sl

        if risk <= 0:
            return empty_result

        risk_percent = (
            risk / entry
        )

        if not (
            MIN_RISK_PERCENT
            <= risk_percent
            <= MAX_RISK_PERCENT
        ):
            return empty_result

        tp1 = (
            entry
            + risk * TP1_R
        )

        tp2 = (
            entry
            + risk * TP2_R
        )

        setup_id = (
            f"BUY|"
            f"{int(df.iloc[breakout_idx]['timestamp'])}|"
            f"{int(df.iloc[r]['timestamp'])}"
        )

        return (
            "BUY",
            round(entry, 8),
            round(sl, 8),
            round(tp1, 8),
            round(tp2, 8),
            setup_id,
        )

    # ========================================================
    # NO SIGNAL
    # ========================================================

    return empty_result


# ============================================================
# SYMBOL DISPLAY
# ============================================================

def get_clean_symbol(symbol):

    try:

        pair = symbol.split(":")[0]

        return pair.replace(
            "/",
            ""
        )

    except Exception:

        return symbol.replace(
            "/",
            ""
        )


# ============================================================
# TELEGRAM BROADCAST
# ============================================================

async def broadcast_signal(
    symbol,
    side,
    entry,
    sl,
    tp1,
    tp2
):

    clean_pair = get_clean_symbol(
        symbol
    )

    direction_text = (
        "🟢 LONG"
        if side == "BUY"
        else
        "🔴 SHORT"
    )

    tv_chart_url = (
        "https://www.tradingview.com/chart/"
        f"?symbol=BYBIT:{clean_pair}.P"
    )

    message = (

        "🚨 <b>JK ANALYZING</b> 🚨\n\n"

        "<b>Exchange:</b> Bybit Futures\n"

        f"<b>Pair:</b> #{clean_pair}\n"

        f"<b>Direction:</b> {direction_text}\n\n"

        "<b>Confirmations Passed:</b>\n"

        "• 1H Trend & Momentum: Aligned\n"

        "• 5M Liquidity Sweep: Confirmed\n"

        "• 5M CHoCH: Strong Body Break\n"

        "• 5M Displacement: Confirmed\n"

        "• 5M FVG / Order Block: Confirmed\n"

        "• 5M CHoCH Retest: Confirmed\n"

        "• 5M Rejection: Confirmed Closed Candle\n\n"

        f"🎯 <b>Entry:</b> {entry}\n"

        f"🛑 <b>Stop Loss:</b> "
        f"{sl} (Swing Protected)\n"

        f"🎯 <b>Take Profit 1:</b> "
        f"{tp1} (1:{TP1_R})\n"

        f"🚀 <b>Take Profit 2:</b> "
        f"{tp2} (1:{TP2_R})\n\n"

        f"📊 <b>Chart:</b> "
        f"<a href='{tv_chart_url}'>"
        "Open on TradingView ↗"
        "</a>\n"
    )

    await tg_bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await save_trade(
        symbol,
        side,
        entry,
        sl,
        tp1,
        tp2,
    )

    print(
        f"\n🔥 SIGNAL SENT | "
        f"{clean_pair} | "
        f"{side} | "
        f"Entry={entry} | "
        f"SL={sl} | "
        f"TP1={tp1} | "
        f"TP2={tp2}"
    )


# ============================================================
# TELEGRAM CONNECTION TEST
# ============================================================

async def test_telegram():

    try:

        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "✅ <b>JK ANALYZING BOT CONNECTED</b>\n\n"
                "Scanner is now active.\n"
                "🟢 Closed Candle Only\n"
                "🛑 Protected SL\n"
                "🎯 TP1 1:1.5\n"
                "🚀 TP2 1:2.5"
            ),
            parse_mode="HTML",
        )

        print(
            "✅ Telegram connection test successful."
        )

        return True

    except Exception as e:

        print(
            f"❌ Telegram connection failed: {e}"
        )

        return False


# ============================================================
# WEEKLY PERFORMANCE REPORT
# ============================================================

async def send_weekly_report():

    try:

        data = (
            await get_weekly_performance_data()
        )

        total_signals = 0
        total_wins = 0
        total_losses = 0
        total_pnl = 0.0

        lines = []

        lines.append(
            "<code>"
            "Day | Sigs | W-L | Win% | Net PnL"
            "</code>"
        )

        lines.append(
            "<code>"
            "--------------------------------"
            "</code>"
        )

        for date_key, stats in data.items():

            sigs = stats["signals"]
            wins = stats["wins"]
            losses = stats["losses"]
            pnl = stats["pnl_r"]

            win_rate = (
                int(
                    (wins / sigs) * 100
                )
                if sigs > 0
                else 0
            )

            pnl_str = (
                f"+{pnl:.1f}R"
                if pnl >= 0
                else
                f"{pnl:.1f}R"
            )

            total_signals += sigs
            total_wins += wins
            total_losses += losses
            total_pnl += pnl

            lines.append(
                "<code>"
                f"{stats['day']:<3} | "
                f"{sigs:^4} | "
                f"{wins:^2}-"
                f"{losses:^2} | "
                f"{win_rate:>3}% | "
                f"{pnl_str:>7}"
                "</code>"
            )

        overall_win_rate = (
            int(
                (
                    total_wins
                    / total_signals
                ) * 100
            )
            if total_signals > 0
            else 0
        )

        total_pnl_str = (
            f"+{total_pnl:.1f}R"
            if total_pnl >= 0
            else
            f"{total_pnl:.1f}R"
        )

        status_icon = (
            "🟢"
            if total_pnl >= 0
            else
            "🔴"
        )

        lines.append(
            "<code>"
            "--------------------------------"
            "</code>"
        )

        lines.append(
            "<code>"
            f"TOT | "
            f"{total_signals:^4} | "
            f"{total_wins:^2}-"
            f"{total_losses:^2} | "
            f"{overall_win_rate:>3}% | "
            f"{total_pnl_str:>7}"
            "</code>"
        )

        report_message = (

            "📊 "
            "<b>JK ANALYZING — WEEKLY REPORT</b> "
            "📊\n"

            "<i>Automated Weekly Performance Sheet</i>\n\n"

            + "\n".join(lines)

            + "\n\n"

            f"💰 <b>Total Net Return:</b> "
            f"<code>{total_pnl_str}</code> "
            f"{status_icon}\n"

            f"🎯 <b>Accuracy Rate:</b> "
            f"<code>{overall_win_rate}%</code>\n"

            f"📅 <i>Report generated on "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            "</i>"
        )

        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=report_message,
            parse_mode="HTML",
        )

    except Exception as e:

        print(
            f"\n⚠️ Weekly report error: {e}"
        )


# ============================================================
# WEEKLY REPORT SCHEDULER
# ============================================================

async def schedule_weekly_report():

    while True:

        try:

            now = datetime.now(timezone.utc)

            if (
                now.weekday() == 6
                and now.hour == 23
                and now.minute >= 55
            ):

                await send_weekly_report()

                await asyncio.sleep(
                    3600
                )

            else:

                await asyncio.sleep(
                    60
                )

        except Exception as e:

            print(
                f"\n⚠️ Scheduler error: {e}"
            )

            await asyncio.sleep(
                60
            )


# ============================================================
# OPEN TRADE MONITOR
# ============================================================

async def monitor_open_trades():

    try:

        trades = (
            await get_open_trades()
        )

    except Exception as e:

        print(
            f"\n⚠️ Database open-trade error: {e}"
        )

        return

    for trade in trades:

        symbol = "UNKNOWN"

        try:

            (
                trade_id,
                symbol,
                side,
                entry,
                sl,
                tp1,
                tp2,
                tp1_hit,
                _
            ) = trade

            ticker = (
                await bybit.fetch_ticker(
                    symbol,
                    params={
                        "category": "linear"
                    },
                )
            )

            last_price = ticker.get(
                "last"
            )

            if last_price is None:
                continue

            last_price = float(
                last_price
            )

            # =================================================
            # BUY
            # =================================================

            if side == "BUY":

                # SL first
                if last_price <= sl:

                    await close_trade(
                        trade_id,
                        "CLOSED_LOSS"
                    )

                    print(
                        f"\n🔴 BUY SL HIT: "
                        f"{symbol}"
                    )

                # TP2
                elif last_price >= tp2:

                    await close_trade(
                        trade_id,
                        "CLOSED_PROFIT"
                    )

                    print(
                        f"\n🟢 BUY TP2 HIT: "
                        f"{symbol}"
                    )

                # TP1
                elif (
                    not tp1_hit
                    and
                    last_price >= tp1
                ):

                    await update_trade_tp1(
                        trade_id
                    )

                    print(
                        f"\n🎯 BUY TP1 HIT: "
                        f"{symbol}"
                    )

            # =================================================
            # SELL
            # =================================================

            elif side == "SELL":

                # SL first
                if last_price >= sl:

                    await close_trade(
                        trade_id,
                        "CLOSED_LOSS"
                    )

                    print(
                        f"\n🔴 SELL SL HIT: "
                        f"{symbol}"
                    )

                # TP2
                elif last_price <= tp2:

                    await close_trade(
                        trade_id,
                        "CLOSED_PROFIT"
                    )

                    print(
                        f"\n🟢 SELL TP2 HIT: "
                        f"{symbol}"
                    )

                # TP1
                elif (
                    not tp1_hit
                    and
                    last_price <= tp1
                ):

                    await update_trade_tp1(
                        trade_id
                    )

                    print(
                        f"\n🎯 SELL TP1 HIT: "
                        f"{symbol}"
                    )

        except Exception as e:

            print(
                f"\n⚠️ Trade monitor error "
                f"{symbol}: {e}"
            )

            continue


# ============================================================
# MAIN SCANNER
# ============================================================

async def main():

    # ========================================================
    # DATABASE
    # ========================================================

    await init_db()

    # ========================================================
    # TELEGRAM TEST
    # ========================================================

    telegram_ok = await test_telegram()

    if not telegram_ok:

        print(
            "\n❌ Telegram test failed."
        )

        print(
            "⚠️ Check TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID."
        )

        return

    # ========================================================
    # WEEKLY REPORT
    # ========================================================

    asyncio.create_task(
        schedule_weekly_report()
    )

    symbols = []

    # ========================================================
    # LOAD SYMBOLS
    # ========================================================

    while not symbols:

        try:

            symbols = (
                await get_top_symbols()
            )

        except Exception as e:

            print(
                f"\n⚠️ Symbol loading error: "
                f"{e}"
            )

            await asyncio.sleep(
                5
            )

    print(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 JK ANALYZING SCANNER ACTIVE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Top {len(symbols)} USDT Pairs\n\n"
        "1H EMA50 + CCI\n"
        "   ↓\n"
        "Liquidity Sweep\n"
        "   ↓\n"
        "5M CHoCH\n"
        "   ↓\n"
        "Displacement\n"
        "   ↓\n"
        "FVG OR Order Block\n"
        "   ↓\n"
        "Current Closed Candle Retest\n"
        "   ↓\n"
        "Rejection\n"
        "   ↓\n"
        "🎯 SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 Closed Candle Only\n"
        "🛑 Protected SL\n"
        "🎯 TP1 1:1.5\n"
        "🚀 TP2 1:2.5\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    # ========================================================
    # CONTINUOUS SCANNER
    # ========================================================

    while True:

        # Cycle statistics

        stats = {
            "scanned": 0,
            "bullish": 0,
            "bearish": 0,
            "neutral": 0,
            "signals": 0,
        }

        try:

            # =================================================
            # MONITOR EXISTING TRADES
            # =================================================

            await monitor_open_trades()

            total = len(symbols)

            # =================================================
            # SCAN ALL SYMBOLS
            # =================================================

            for idx, symbol in enumerate(
                symbols,
                1
            ):

                clean_name = (
                    get_clean_symbol(symbol)
                )

                print(
                    f"🔍 [{idx}/{total}] "
                    f"Scanning {clean_name}...",
                    end="\r",
                    flush=True
                )

                stats["scanned"] += 1

                try:

                    # =========================================
                    # 1H
                    # =========================================

                    df_1h = (
                        await fetch_ohlcv(
                            symbol,
                            "1h",
                            OHLCV_1H_LIMIT
                        )
                    )

                    if df_1h is None:
                        continue

                    # =========================================
                    # 5M
                    # =========================================

                    df_5m = (
                        await fetch_ohlcv(
                            symbol,
                            "5m",
                            OHLCV_5M_LIMIT
                        )
                    )

                    if df_5m is None:
                        continue

                    # =========================================
                    # 1H BIAS
                    # =========================================

                    bias = (
                        analyze_1h_indicators(
                            df_1h
                        )
                    )

                    if bias == "BULLISH":

                        stats["bullish"] += 1

                    elif bias == "BEARISH":

                        stats["bearish"] += 1

                    else:

                        stats["neutral"] += 1

                        await asyncio.sleep(
                            0.10
                        )

                        continue

                    # =========================================
                    # 5M SETUP
                    # =========================================

                    (
                        side,
                        entry,
                        sl,
                        tp1,
                        tp2,
                        setup_id,
                    ) = (
                        check_5m_choch_and_retest(
                            df_5m,
                            bias
                        )
                    )

                    # =========================================
                    # VALID SIGNAL
                    # =========================================

                    if (
                        side is not None
                        and
                        entry is not None
                        and
                        setup_id is not None
                    ):

                        full_setup_id = (
                            f"{symbol}|"
                            f"{setup_id}"
                        )

                        # =====================================
                        # DUPLICATE PROTECTION
                        # =====================================

                        if (
                            full_setup_id
                            in sent_setup_ids
                        ):

                            continue

                        # Mark before Telegram
                        sent_setup_ids.add(
                            full_setup_id
                        )

                        try:

                            await broadcast_signal(
                                symbol,
                                side,
                                entry,
                                sl,
                                tp1,
                                tp2
                            )

                            stats["signals"] += 1

                        except Exception as e:

                            # Allow retry if broadcast failed
                            sent_setup_ids.discard(
                                full_setup_id
                            )

                            print(
                                f"\n⚠️ Signal broadcast "
                                f"failed for "
                                f"{clean_name}: {e}"
                            )

                    # Small rate-limit protection

                    await asyncio.sleep(
                        0.15
                    )

                except Exception as e:

                    print(
                        f"\n⚠️ Error scanning "
                        f"{clean_name}: {e}"
                    )

                    continue

            # =================================================
            # CYCLE COMPLETE
            # =================================================

            print(
                "\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            print(
                "📊 SCAN REPORT"
            )

            print(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            print(
                f"Pairs scanned : "
                f"{stats['scanned']}"
            )

            print(
                f"🟢 1H Bullish : "
                f"{stats['bullish']}"
            )

            print(
                f"🔴 1H Bearish : "
                f"{stats['bearish']}"
            )

            print(
                f"⚪ 1H Neutral  : "
                f"{stats['neutral']}"
            )

            print(
                f"🚨 Signals     : "
                f"{stats['signals']}"
            )

            print(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            print(
                f"🔄 Completed scan "
                f"of {total} pairs."
            )

            print(
                f"⏳ Waiting "
                f"{SCAN_DELAY}s..."
            )

        except Exception as e:

            print(
                f"\n⚠️ Main loop error: {e}"
            )

            await asyncio.sleep(
                5
            )

        # ====================================================
        # NEXT SCAN
        # ====================================================

        await asyncio.sleep(
            SCAN_DELAY
        )


# ============================================================
# START
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

    except SystemExit:

        print(
            "\n🛑 Bot stopped."
        )

    except Exception as e:

        print(
            f"\n❌ Fatal error: {e}"
        )
