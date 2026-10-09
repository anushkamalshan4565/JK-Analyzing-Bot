import os
import asyncio
from datetime import datetime, timezone
import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
from telegram import Bot

# --- Configurations ---
TELEGRAM_BOT_TOKEN = "8983892388:AAG5rvlx_b0C6hIKkElHuQVs5ZlW2Vw89GI"
TELEGRAM_CHAT_ID = "-1004306671705"

bybit = ccxt.bybit({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'linear',
    }
})
tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# එකම Candle එකට එක දිගට alert නොයැවීමට
last_alerted_candles = {}


# --- Indicator Calculations ---
def calculate_cci(df, length):
    tp = (df['high'] + df['low'] + df['close']) / 3
    sma = tp.rolling(window=length).mean()
    mad = tp.rolling(window=length).apply(
        lambda x: np.mean(np.abs(x - np.mean(x)))
    )
    mad = mad.replace(0, 0.00001)
    return (tp - sma) / (0.015 * mad)


async def get_top_75_symbols():
    try:
        markets = await bybit.load_markets()
        tickers = await bybit.fetch_tickers(params={'category': 'linear'})
        usdt_pairs = []

        for symbol, ticker in tickers.items():
            market = markets.get(symbol)
            if (
                market
                and market.get('linear')
                and market.get('contract')
                and market.get('settle') == 'USDT'
            ):
                vol = ticker.get('quoteVolume') or 0
                if vol > 0:
                    usdt_pairs.append({'symbol': symbol, 'volume': float(vol)})

        usdt_pairs.sort(key=lambda x: x['volume'], reverse=True)
        top_75 = [item['symbol'] for item in usdt_pairs[:75]]

        if len(top_75) > 0:
            print(f"✅ Loaded {len(top_75)} Bybit USDT Pairs!", flush=True)
            return top_75

    except Exception as e:
        print(f"⚠️ Market fetch error: {e}", flush=True)

    return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]


async def fetch_ohlcv(symbol, timeframe="5m", limit=100):
    try:
        raw = await bybit.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, params={'category': 'linear'})
        if not raw or len(raw) < 60:
            return None

        df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception as e:
        if "10006" in str(e) or "Rate Limit" in str(e):
            print(f"⚠️ Rate limit on {symbol}, pausing 5s...", flush=True)
            await asyncio.sleep(5)
        return None


# ============================================================
# EXACT VIDEO LOGIC: PULLBACK CANDLE HIGH/LOW BREAK BY BODY CLOSE
# ============================================================

def check_pullback_candle_breakout(df_5m):
    if df_5m is None or len(df_5m) < 60:
        return None, None, None, None, None, None, None

    # සෑදී අවසන් වූ (CLOSED) CANDLES පමණක් ගනී (forming candle එක ඉවත් කරයි)
    df = df_5m.iloc[:-1].copy().reset_index(drop=True)

    # 5M Indicators
    df['CCI50'] = calculate_cci(df, 50)
    df['CCI7'] = calculate_cci(df, 7)

    current_idx = len(df) - 1
    curr = df.iloc[current_idx]

    # --------------------------------------------------------
    # 🟢 1. LONG SETUP (IMG_5337 Video Logic)
    # --------------------------------------------------------
    if curr['CCI50'] > 0:
        # පසුගිය candles 15 තුළ CCI7 එක -80 ට වඩා අඩුවී හැරුණු අවස්ථාව
        lookback = df.iloc[max(0, current_idx - 15): current_idx]
        min_cci7_idx = lookback['CCI7'].idxmin()

        if lookback.loc[min_cci7_idx, 'CCI7'] < -80:
            # CCI 7 dip එක ගහපු අවම පහළට ගිය Candle එක (Lowest Wick Point)
            pullback_candle = df.iloc[min_cci7_idx]
            level_to_break = pullback_candle['high']

            bars_passed = current_idx - min_cci7_idx

            # Pullback එකෙන් පසු candles 1 සිට 4ක් ඇතුළත
            if 1 <= bars_passed <= 4:
                # අනිවාර්යයෙන්ම Candle Body එකෙන්ම High එක කඩා උඩින් CLOSE විය යුතුයි
                if curr['close'] > level_to_break and curr['close'] > curr['open']:
                    entry = round(float(curr['close']), 6)
                    level = round(float(level_to_break), 6)
                    sl = round(float(pullback_candle['low']) * 0.9985, 6)
                    risk = entry - sl

                    if risk > 0 and (risk / entry) <= 0.04:
                        tp1 = round(entry + (risk * 2.0), 6)
                        tp2 = round(entry + (risk * 3.5), 6)
                        candle_time = int(curr['timestamp'])
                        return "LONG", entry, sl, tp1, tp2, level, candle_time

    # --------------------------------------------------------
    # 🔴 2. SHORT SETUP (IMG_5336 Video Logic)
    # --------------------------------------------------------
    if curr['CCI50'] < 0:
        # පසුගිය candles 15 තුළ CCI7 එක +80 ට වඩා වැඩිවී හැරුණු අවස්ථාව
        lookback = df.iloc[max(0, current_idx - 15): current_idx]
        max_cci7_idx = lookback['CCI7'].idxmax()

        if lookback.loc[max_cci7_idx, 'CCI7'] > 80:
            # CCI 7 peak එක ගහපු උපරිම ඉහළට ගිය Candle එක (Highest Wick Point)
            pullback_candle = df.iloc[max_cci7_idx]
            level_to_break = pullback_candle['low']

            bars_passed = current_idx - max_cci7_idx

            # Pullback එකෙන් පසු candles 1 සිට 4ක් ඇතුළත
            if 1 <= bars_passed <= 4:
                # අනිවාර්යයෙන්ම Candle Body එකෙන්ම Low එක කඩා යටින් CLOSE විය යුතුයි
                if curr['close'] < level_to_break and curr['close'] < curr['open']:
                    entry = round(float(curr['close']), 6)
                    level = round(float(level_to_break), 6)
                    sl = round(float(pullback_candle['high']) * 1.0015, 6)
                    risk = sl - entry

                    if risk > 0 and (risk / entry) <= 0.04:
                        tp1 = round(entry - (risk * 2.0), 6)
                        tp2 = round(entry - (risk * 3.5), 6)
                        candle_time = int(curr['timestamp'])
                        return "SHORT", entry, sl, tp1, tp2, level, candle_time

    return None, None, None, None, None, None, None


# ============================================================
# TELEGRAM BROADCAST
# ============================================================

async def broadcast_alert(symbol, side, entry, sl, tp1, tp2, level, candle_time):
    global last_alerted_candles

    alert_key = f"{symbol}_{side}"
    if last_alerted_candles.get(alert_key) == candle_time:
        return

    last_alerted_candles[alert_key] = candle_time

    pair_display = symbol.split(':')[0]
    clean_pair = pair_display.replace('/', '')
    direction_text = "🟢 BUY / LONG SETUP" if side == "LONG" else "🔴 SELL / SHORT SETUP"

    tv_chart_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{clean_pair}.P"

    msg = (
        f"🚨 <b>JK ANALYZING — CONFIRMED BREAKOUT</b> 🚨\n\n"
        f"<b>Coin:</b> #{clean_pair} (Bybit Futures)\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"🎯 <b>Entry:</b> <code>{entry}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl}</code>\n"
        f"🎯 <b>Take Profit 1 (1:2):</b> <code>{tp1}</code>\n"
        f"🚀 <b>Take Profit 2 (1:3.5):</b> <code>{tp2}</code>\n\n"
        f"<b>Trigger Details:</b>\n"
        f"• 5M CCI 50: Zero Line Filter Passed ✅\n"
        f"• 5M CCI 7: Pullback Hook Completed ✅\n"
        f"• Broken Level: <code>{level}</code>\n"
        f"• Status: <b>Level Covered & Candle Closed by Body</b> ✅\n\n"
        f"📊 <b>Chart:</b> <a href='{tv_chart_url}'>Open on TradingView ↗</a>\n"
    )

    try:
        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        print(f"\n🔥 [CONFIRMED ALERT SENT] {clean_pair} {side} at {entry}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram broadcast failed: {e}", flush=True)


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

async def main():
    try:
        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="🚀 <b>JK Analyzing Bot is LIVE!</b>\nOnly Closed Candle Body Breakouts will be Alerted...",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"⚠️ Startup alert failed: {e}", flush=True)

    symbols = []
    while not symbols:
        try:
            symbols = await get_top_75_symbols()
        except Exception:
            print("Connecting to Bybit... retrying in 5s.", flush=True)
            await asyncio.sleep(5)

    print("🚀 Scanner Active: Closed Candle Body Breakouts Only...", flush=True)

    while True:
        try:
            total = len(symbols)

            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(':')[0]
                print(f"🔍 [{idx}/{total}] Scanning: {clean_name}...", end="\r", flush=True)

                try:
                    df_5m = await fetch_ohlcv(symbol, '5m', limit=60)

                    if df_5m is not None:
                        (
                            side,
                            entry,
                            sl,
                            tp1,
                            tp2,
                            level,
                            candle_time
                        ) = check_pullback_candle_breakout(df_5m)

                        if side:
                            await broadcast_alert(
                                symbol,
                                side,
                                entry,
                                sl,
                                tp1,
                                tp2,
                                level,
                                candle_time
                            )

                    await asyncio.sleep(0.35)

                except Exception:
                    continue

        except Exception as e:
            print(f"\n⚠️ Main loop alert: {e}", flush=True)
            await asyncio.sleep(5)

        await asyncio.sleep(15)


# ============================================================
# START SCRIPT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped by user.", flush=True)
