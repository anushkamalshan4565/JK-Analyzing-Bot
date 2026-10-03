import pandas as pd
import numpy as np

from telegram import Bot

from database import (
    init_db,
    save_trade,
    get_open_trades,
    update_trade_tp1,
    close_trade,
    get_weekly_performance_data
)


# ============================================================
# CONFIGURATIONS
# ============================================================

TELEGRAM_BOT_TOKEN = "8983892388:AAFjwABLqLR6tvEtHHD2CUuF2r8x90JMU-w"
TELEGRAM_CHAT_ID = "-1004306671705"


# ============================================================
# TOKEN VALIDATION
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN environment variable is not set."
    )


# ============================================================
# BYBIT
# ============================================================

bybit = ccxt.bybit({
    "enableRateLimit": True,
    "options": {
        "defaultType": "linear"
    }
})

tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def calculate_cci(df, length):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(window=length).mean()
    mad = tp.rolling(window=length).apply(
        lambda x: np.mean(np.abs(x - np.mean(x)))
    )
    mad = mad.replace(0, 0.00001)
    cci = (tp - sma) / (0.015 * mad)
    return cci


# ============================================================
# GET TOP 100 USDT PAIRS
# ============================================================

async def get_top_100_symbols():
    try:
        markets = await bybit.load_markets()
        tickers = await bybit.fetch_tickers(params={"category": "linear"})
        usdt_pairs = []

        for symbol, ticker in tickers.items():
            market = markets.get(symbol)
            if (
                market
                and market.get("linear")
                and market.get("contract")
                and market.get("settle") == "USDT"
            ):
                vol = ticker.get("quoteVolume") or 0
                if vol > 0:
                    usdt_pairs.append({
                        "symbol": symbol,
                        "volume": float(vol)
                    })

        usdt_pairs.sort(key=lambda x: x["volume"], reverse=True)
        top_100 = [item["symbol"] for item in usdt_pairs[:100]]

        if len(top_100) > 0:
            print(
                f"✅ Successfully loaded {len(top_100)} Bybit USDT Perpetual Pairs by Volume!",
                flush=True
            )
            return top_100

    except Exception as e:
        print(f"⚠️ Market fetch error: {e}", flush=True)

    return [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "XRP/USDT:USDT",
        "DOGE/USDT:USDT"
    ]


# ============================================================
# FETCH OHLCV
# ============================================================

async def fetch_ohlcv(symbol, timeframe, limit=100):
    try:
        raw = await bybit.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            params={"category": "linear"}
        )

        if not raw or len(raw) < 55:
            return None

        df = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        return df

    except Exception as e:
        return None


# ============================================================
# 1H TREND / BIAS
# ============================================================

def analyze_1h_indicators(df_1h):
    df_1h["EMA50"] = calculate_ema(df_1h["close"], 50)
    df_1h["CCI50"] = calculate_cci(df_1h, 50)
    df_1h["CCI7"] = calculate_cci(df_1h, 7)

    last = df_1h.iloc[-2]

    long_condition = (
        last["close"] > last["EMA50"]
        and last["CCI50"] > 0
        and df_1h["CCI7"].iloc[-4:-1].min() < -80
    )

    short_condition = (
        last["close"] < last["EMA50"]
        and last["CCI50"] < 0
        and df_1h["CCI7"].iloc[-4:-1].max() > 80
    )

    if long_condition:
        return "BULLISH"
    elif short_condition:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# RELIABLE 5M SMC CHoCH + RETEST
# ============================================================

def check_5m_choch_and_retest(df_5m, bias_1h):
    if df_5m is None or len(df_5m) < 80:
        return (None, None, None, None, None)

    df = df_5m.iloc[:-1].copy()
    df.reset_index(drop=True, inplace=True)

    if len(df) < 70:
        return (None, None, None, None, None)

    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    closes = df["close"].values

    current_idx = len(df) - 1

    SWING_LEFT = 3
    SWING_RIGHT = 3
    LOOKBACK = 45
    STRUCTURE_LOOKBACK = 25
    MIN_BODY_RATIO = 0.55
    MIN_DISPLACEMENT = 0.0010
    RETEST_MAX_BARS = 8
    RETEST_TOLERANCE = 0.0015

    swing_highs = []
    swing_lows = []

    for i in range(SWING_LEFT, len(df) - SWING_RIGHT):
        is_swing_high = (
            all(highs[i] > highs[i - k] for k in range(1, SWING_LEFT + 1))
            and all(highs[i] > highs[i + k] for k in range(1, SWING_RIGHT + 1))
        )
        is_swing_low = (
            all(lows[i] < lows[i - k] for k in range(1, SWING_LEFT + 1))
            and all(lows[i] < lows[i + k] for k in range(1, SWING_RIGHT + 1))
        )

        if is_swing_high:
            swing_highs.append(i)
        if is_swing_low:
            swing_lows.append(i)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return (None, None, None, None, None)

    recent_highs = [i for i in swing_highs if i >= current_idx - LOOKBACK]
    recent_lows = [i for i in swing_lows if i >= current_idx - LOOKBACK]

    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return (None, None, None, None, None)

    # ---------------- BEARISH CHoCH ----------------
    if bias_1h == "BEARISH":
        hh_idx = recent_highs[-1]
        previous_lows = [i for i in recent_lows if i < hh_idx]

        if not previous_lows:
            return (None, None, None, None, None)

        hl_idx = previous_lows[-1]
        choch_level = lows[hl_idx]

        breakout_idx = None
        search_start = max(hl_idx + 1, current_idx - STRUCTURE_LOOKBACK)

        for k in range(search_start, current_idx + 1):
            candle_range = highs[k] - lows[k]
            if candle_range <= 0:
                continue

            body = abs(closes[k] - opens[k])
            body_ratio = body / candle_range
            displacement = (choch_level - closes[k]) / choch_level
            bearish_body = closes[k] < opens[k]

            if (
                closes[k] < choch_level
                and bearish_body
                and body_ratio >= MIN_BODY_RATIO
                and displacement >= MIN_DISPLACEMENT
            ):
                breakout_idx = k
                break

        if breakout_idx is None:
            return (None, None, None, None, None)

        bars_after_break = current_idx - breakout_idx
        if bars_after_break < 1 or bars_after_break > RETEST_MAX_BARS:
            return (None, None, None, None, None)

        for r in range(breakout_idx + 1, current_idx + 1):
            retest_high = highs[r]
            retest_close = closes[r]
            retest_open = opens[r]

            touched_level = (
                retest_high >= choch_level * (1 - RETEST_TOLERANCE)
                and retest_high <= choch_level * (1 + RETEST_TOLERANCE)
            )
            wick_retest = (
                retest_high >= choch_level
                and retest_close < choch_level
            )

            if not (touched_level or wick_retest):
                continue

            candle_range = retest_high - lows[r]
            if candle_range <= 0:
                continue

            body = abs(retest_close - retest_open)
            upper_wick = retest_high - max(retest_open, retest_close)
            bearish_close = retest_close < retest_open
            body_ratio = body / candle_range

            rejection = (
                bearish_close
                and retest_close < choch_level
                and body_ratio >= 0.40
                and upper_wick >= body * 0.30
            )

            if not rejection:
                continue

            entry = round(retest_close, 4)
            structure_sl = highs[hh_idx] * 1.0035
            retest_sl = retest_high * 1.0020
            sl = round(max(structure_sl, retest_sl), 4)
            risk = sl - entry

            if risk <= 0:
                continue

            risk_percent = risk / entry
            if not (0.0025 <= risk_percent <= 0.035):
                continue

            tp1 = round(entry - (risk * 1.5), 4)
            tp2 = round(entry - (risk * 2.5), 4)

            return ("SELL", entry, sl, tp1, tp2)

    # ---------------- BULLISH CHoCH ----------------
    elif bias_1h == "BULLISH":
        ll_idx = recent_lows[-1]
        previous_highs = [i for i in recent_highs if i < ll_idx]

        if not previous_highs:
            return (None, None, None, None, None)

        lh_idx = previous_highs[-1]
        choch_level = highs[lh_idx]

        breakout_idx = None
        search_start = max(lh_idx + 1, current_idx - STRUCTURE_LOOKBACK)

        for k in range(search_start, current_idx + 1):
            candle_range = highs[k] - lows[k]
            if candle_range <= 0:
                continue

            body = abs(closes[k] - opens[k])
            body_ratio = body / candle_range
            displacement = (closes[k] - choch_level) / choch_level
            bullish_body = closes[k] > opens[k]

            if (
                closes[k] > choch_level
                and bullish_body
                and body_ratio >= MIN_BODY_RATIO
                and displacement >= MIN_DISPLACEMENT
            ):
                breakout_idx = k
                break

        if breakout_idx is None:
            return (None, None, None, None, None)

        bars_after_break = current_idx - breakout_idx
        if bars_after_break < 1 or bars_after_break > RETEST_MAX_BARS:
            return (None, None, None, None, None)

        for r in range(breakout_idx + 1, current_idx + 1):
            retest_low = lows[r]
            retest_close = closes[r]
            retest_open = opens[r]

            touched_level = (
                retest_low >= choch_level * (1 - RETEST_TOLERANCE)
                and retest_low <= choch_level * (1 + RETEST_TOLERANCE)
            )
            wick_retest = (
                retest_low <= choch_level
                and retest_close > choch_level
            )

            if not (touched_level or wick_retest):
                continue

            candle_range = highs[r] - retest_low
            if candle_range <= 0:
                continue

            body = abs(retest_close - retest_open)
            lower_wick = min(retest_open, retest_close) - retest_low
            bullish_close = retest_close > retest_open
            body_ratio = body / candle_range

            rejection = (
                bullish_close
                and retest_close > choch_level
                and body_ratio >= 0.40
                and lower_wick >= body * 0.30
            )

            if not rejection:
                continue

            entry = round(retest_close, 4)
            structure_sl = lows[ll_idx] * 0.9965
            retest_sl = retest_low * 0.9980
            sl = round(min(structure_sl, retest_sl), 4)
            risk = entry - sl

            if risk <= 0:
                continue

            risk_percent = risk / entry
            if not (0.0025 <= risk_percent <= 0.035):
                continue

            tp1 = round(entry + (risk * 1.5), 4)
            tp2 = round(entry + (risk * 2.5), 4)

            return ("BUY", entry, sl, tp1, tp2)

    return (None, None, None, None, None)


# ============================================================
# TELEGRAM BROADCAST
# ============================================================

async def broadcast_signal(symbol, side, entry, sl, tp1, tp2):
    pair_display = symbol.split(":")[0]
    clean_pair = pair_display.replace("/", "")

    direction_text = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
    tv_chart_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{clean_pair}.P"

    msg = (
        f"🚨 <b>JK Analyzing</b> 🚨\n\n"
        f"<b>Exchange:</b> Bybit Futures\n"
        f"<b>Pair:</b> #{clean_pair}\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"<b>Confirmations Passed:</b>\n"
        f"• 1H Trend & Momentum: Aligned\n"
        f"• 5M Major CHoCH: Confirmed Body Break\n"
        f"• 5M CHoCH: Closed Candle Confirmation\n"
        f"• 5M Retest: Level Confirmed\n"
        f"• 5M Rejection: Confirmed Closed Candle\n\n"
        f"🎯 <b>Entry:</b> {entry}\n"
        f"🛑 <b>Stop Loss:</b> {sl} (Swing Protected)\n"
        f"🎯 <b>Take Profit 1:</b> {tp1} (1:1.5)\n"
        f"🚀 <b>Take Profit 2:</b> {tp2} (1:2.5)\n\n"
        f"📊 <b>Chart:</b> <a href='{tv_chart_url}'>Open on TradingView ↗</a>\n"
    )

    await tg_bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    await save_trade(symbol, side, entry, sl, tp1, tp2)

    print(
        f"\n🔥 [5M CHoCH + RETEST SIGNAL] Sent to Telegram: {pair_display} {side}",
        flush=True
    )


# ============================================================
# WEEKLY PERFORMANCE REPORT
# ============================================================

async def send_weekly_report():
    data = await get_weekly_performance_data()

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

        lines.append(
            f"<code>{stats['day']:<3} | {sigs:^4} | {w:^2}-{l:^2} | {win_rate:>3}% | {pnl_str:>7}</code>"
        )

    overall_win_rate = int((total_wins / total_signals) * 100) if total_signals > 0 else 0
    tot_pnl_str = f"+{total_pnl:.1f}R" if total_pnl >= 0 else f"{total_pnl:.1f}R"
    status_icon = "🟢" if total_pnl >= 0 else "🔴"

    lines.append("<code>-----------------------------------</code>")
    lines.append(
        f"<code>TOT | {total_signals:^4} | {total_wins:^2}-{total_losses:^2} | {overall_win_rate:>3}% | {tot_pnl_str:>7}</code>"
    )

    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_msg = (
        f"📊 <b>JK ANALYZING — WEEKLY REPORT</b> 📊\n"
        f"<i>Automated Weekly Performance Sheet</i>\n\n"
        + "\n".join(lines)
        + "\n\n"
        f"💰 <b>Total Net Return:</b> <code>{tot_pnl_str}</code> {status_icon}\n"
        f"🎯 <b>Accuracy Rate:</b> <code>{overall_win_rate}%</code>\n"
        f"📅 <i>Report generated on {now_date}</i>"
    )

    await tg_bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=report_msg,
        parse_mode="HTML"
    )


async def schedule_weekly_report():
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 23 and now.minute >= 55:
            await send_weekly_report()
            await asyncio.sleep(3600)
        await asyncio.sleep(60)


# ============================================================
# OPEN TRADE MONITOR
# ============================================================

async def monitor_open_trades():
    trades = await get_open_trades()

    for trade in trades:
        t_id, sym, side, entry, sl, tp1, tp2, tp1_hit, _ = trade

        try:
            ticker = await bybit.fetch_ticker(
                sym,
                params={"category": "linear"}
            )
            last_price = ticker["last"]

            if side == "BUY":
                if last_price <= sl:
                    await close_trade(t_id, "CLOSED_LOSS")
                elif last_price >= tp2:
                    await close_trade(t_id, "CLOSED_PROFIT")
                elif not tp1_hit and last_price >= tp1:
                    await update_trade_tp1(t_id)

            elif side == "SELL":
                if last_price >= sl:
                    await close_trade(t_id, "CLOSED_LOSS")
                elif last_price <= tp2:
                    await close_trade(t_id, "CLOSED_PROFIT")
                elif not tp1_hit and last_price <= tp1:
                    await update_trade_tp1(t_id)

        except Exception:
            continue


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

async def main():
    await init_db()

    asyncio.create_task(schedule_weekly_report())

    symbols = []

    while not symbols:
        try:
            symbols = await get_top_100_symbols()
        except Exception:
            print("Connecting to Bybit... retrying in 5s.", flush=True)
            await asyncio.sleep(5)

    print(
        "\n🚀 Scanner Active"
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        "\nTOP 100 Bybit USDT Perpetual Pairs"
        "\n1H Bias → 5M CHoCH → Displacement → Retest → Rejection"
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        "\n🟢 Closed-Candle Confirmation"
        "\n🎯 Protected SL"
        "\n📊 TP1 1:1.5"
        "\n🚀 TP2 1:2.5"
        "\n🔄 Multiple Signals Allowed"
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
        flush=True
    )

    while True:
        try:
            await monitor_open_trades()

            total = len(symbols)

            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(":")[0]

                print(
                    f"🔍 [{idx}/{total}] Scanning: {clean_name}...",
                    flush=True
                )

                try:
                    df_1h = await fetch_ohlcv(symbol, "1h", limit=60)
                    df_5m = await fetch_ohlcv(symbol, "5m", limit=100)

                    if df_1h is not None and df_5m is not None:
                        bias_1h = analyze_1h_indicators(df_1h)

                        if bias_1h != "NEUTRAL":
                            (
                                side,
                                entry,
                                sl,
                                tp1,
                                tp2
                            ) = check_5m_choch_and_retest(df_5m, bias_1h)

                            if side and entry:
                                await broadcast_signal(
                                    symbol,
                                    side,
                                    entry,
                                    sl,
                                    tp1,
                                    tp2
                                )

                        await asyncio.sleep(0.3)

                except Exception as e:
                    continue

            print(
                f"\n🔄 Completed 1 cycle of {total} pairs. Waiting 20s for next cycle...",
                flush=True
            )

        except Exception as e:
            print(f"\n⚠️ Main loop alert: {e}", flush=True)
            await asyncio.sleep(5)

        await asyncio.sleep(20)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped by user.", flush=True)
