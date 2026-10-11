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

# එකම Candle එකට දෙවරක් Alert යැවීම වැළැක්වීමට
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


async def get_top_symbols(limit=100):
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
        top_pairs = [item['symbol'] for item in usdt_pairs[:limit]]

        if len(top_pairs) > 0:
            print(f"✅ Successfully loaded {len(top_pairs)} Bybit USDT Pairs by Volume!", flush=True)
            return top_pairs

    except Exception as e:
        print(f"⚠️ Market fetch error: {e}", flush=True)

    return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]


async def fetch_ohlcv(symbol, timeframe="1h", limit=80):
    try:
        raw = await bybit.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, params={'category': 'linear'})
        if not raw or len(raw) < 55:
            return None

        return pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    except Exception as e:
        if "10006" in str(e) or "Rate Limit" in str(e):
            print(f"⚠️ Rate limit warning on {symbol}! Pausing briefly...", flush=True)
            await asyncio.sleep(5)
        return None


# ============================================================
# 1H EXACT CHART LOGIC (1H CCI 50/7 + CANDLE BREAK BY BODY)
# ============================================================

def check_1h_pullback_candle_breakout(df_1h):
    if df_1h is None or len(df_1h) < 55:
        return None, None, None, None, None, None, None

    # CLOSED CANDLES ONLY (දැනට දුවන candle එක හැර අවසන් වූ closed candle එක පමණක් ගනී)
    df = df_1h.iloc[:-1].copy().reset_index(drop=True)

    # 1H Indicators
    df['CCI50'] = calculate_cci(df, 50)
    df['CCI7'] = calculate_cci(df, 7)

    current_idx = len(df) - 1
    curr = df.iloc[current_idx]

    # --------------------------------------------------------
    # 🟢 1H LONG SETUP
    # --------------------------------------------------------
    if curr['CCI50'] > 0:
        # පසුගිය candles 15 තුළ CCI 7 Oversold (< -80) වී තිබිය යුතුය
        lookback = df.iloc[max(0, current_idx - 15): current_idx]
        if lookback['CCI7'].min() < -80:
            # එම Pullback එකේ පහළම Low එක ඇති Candle එක සොයාගැනීම
            lowest_idx = lookback['low'].idxmin()
            lowest_candle = df.iloc[lowest_idx]

            # Breakout විය යුතු Level එක (Lowest Candle එකේ High එක)
            level_to_break = float(lowest_candle['high'])
            bars_since_low = current_idx - lowest_idx

            # Lowest candle එකෙන් පසු candles 1 සිට 5ක් ඇතුළත breakout එක සිදුවිය යුතුයි
            if 1 <= bars_since_low <= 5:
                # Candle එක Bullish (Green) වී, එහි Close මිල Level එකට වඩා ඉහළින් Close වීම
                if curr['close'] > level_to_break and curr['close'] > curr['open']:
                    entry = round(float(curr['close']), 6)
                    level = round(level_to_break, 6)
                    # Stop loss එක Pullback හි Lowest Low wick එකට මඳක් පහළින්
                    sl = round(float(lowest_candle['low']) * 0.9985, 6)
                    risk = entry - sl

                    if risk > 0 and (risk / entry) <= 0.05:
                        tp1 = round(entry + (risk * 2.0), 6)
                        tp2 = round(entry + (risk * 3.5), 6)
                        candle_time = int(curr['timestamp'])
                        return "LONG", entry, sl, tp1, tp2, level, candle_time

    # --------------------------------------------------------
    # 🔴 1H SHORT SETUP
    # --------------------------------------------------------
    if curr['CCI50'] < 0:
        # පසුගිය candles 15 තුළ CCI 7 Overbought (> 80) වී තිබිය යුතුය
        lookback = df.iloc[max(0, current_idx - 15): current_idx]
        if lookback['CCI7'].max() > 80:
            # එම Pullback එකේ ඉහළම High එක ඇති Candle එක සොයාගැනීම
            highest_idx = lookback['high'].idxmax()
            highest_candle = df.iloc[highest_idx]

            # Breakout විය යුතු Level එක (Highest Candle එකේ Low Wick එක)
            level_to_break = float(highest_candle['low'])
            bars_since_high = current_idx - highest_idx

            if 1 <= bars_since_high <= 5:
                # Candle එක Bearish (Red) වී, එහි Close මිල Level එකට වඩා පහළින් Close වීම
                if curr['close'] < level_to_break and curr['close'] < curr['open']:
                    entry = round(float(curr['close']), 6)
                    level = round(level_to_break, 6)
                    # Stop loss එක Pullback හි Highest High wick එකට මඳක් ඉහළින්
                    sl = round(float(highest_candle['high']) * 1.0015, 6)
                    risk = sl - entry

                    if risk > 0 and (risk / entry) <= 0.05:
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

    break_desc = "Candle High Broken & Closed Above" if side == "LONG" else "Candle Low Broken & Closed Below"

    msg = (
        f"🚨 <b>JK ANALYZING — 1H CONFIRMED SETUP</b> 🚨\n\n"
        f"<b>Coin:</b> #{clean_pair} (Bybit Futures)\n"
        f"<b>Timeframe:</b> <b>1H (1-Hour)</b>\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"🎯 <b>Entry:</b> <code>{entry}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl}</code>\n"
        f"🎯 <b>Take Profit 1 (1:2):</b> <code>{tp1}</code>\n"
        f"🚀 <b>Take Profit 2 (1:3.5):</b> <code>{tp2}</code>\n\n"
        f"<b>Trigger Confirmations:</b>\n"
        f"• 1H CCI 50 & CCI 7: 100% Aligned ✅\n"
        f"• 1H Pullback Level: <code>{level}</code>\n"
        f"• Breakout Status: <b>{break_desc} by Body</b> ✅\n"
        f"• Bar Status: <b>1H Closed Candle Confirmed</b>\n\n"
        f"📊 <b>Chart:</b> <a href='{tv_chart_url}'>Open on TradingView ↗</a>\n"
    )

    try:
        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        print(f"\n🔥 [1H SETUP SENT] {clean_pair} {side} at {entry}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram broadcast failed: {e}", flush=True)


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

async def main():
    try:
        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="🚀 <b>JK Analyzing — 1H Timeframe Scanner LIVE!</b>\nScanning Top 100 Pairs directly on 1H Chart...",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"⚠️ Startup message failed: {e}", flush=True)

    symbols = []
    while not symbols:
        try:
            symbols = await get_top_symbols(limit=100)
        except Exception:
            print("Connecting to Bybit... retrying in 5s.", flush=True)
            await asyncio.sleep(5)

    print("🚀 Scanner Active: 1H Closed Body Pullback Breakout Strategy...", flush=True)

    while True:
        try:
            total = len(symbols)

            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(':')[0]
                print(f"🔍 [{idx}/{total}] Scanning: {clean_name} (1H)...", end="\r", flush=True)

                try:
                    # 1H OHLCV දත්ත ලබා ගනී
                    df_1h = await fetch_ohlcv(symbol, '1h', limit=70)

                    if df_1h is not None:
                        (
                            side,
                            entry,
                            sl,
                            tp1,
                            tp2,
                            level,
                            candle_time
                        ) = check_1h_pullback_candle_breakout(df_1h)

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

        # 1H එකක් නිසා scan එකක් අවසානයේ තත්පර 30ක් රැඳී සිට නැවත ස්කෑන් කරයි
        await asyncio.sleep(30)


# ============================================================
# START SCRIPT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped by user.", flush=True)
