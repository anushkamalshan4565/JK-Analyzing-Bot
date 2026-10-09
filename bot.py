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

# එකම Candle එකට දෙවරක් alert යැවීම වැළැක්වීමට
last_alerted_candles = {}


# --- Indicator Calculations ---
def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def calculate_cci(df, length):
    tp = (df['high'] + df['low'] + df['close']) / 3
    sma = tp.rolling(window=length).mean()
    mad = tp.rolling(window=length).apply(
        lambda x: np.mean(np.abs(x - np.mean(x)))
    )
    mad = mad.replace(0, 0.00001)
    cci = (tp - sma) / (0.015 * mad)
    return cci


async def get_top_100_symbols():
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
        top_100 = [item['symbol'] for item in usdt_pairs[:100]]

        if len(top_100) > 0:
            print(f"✅ Successfully loaded {len(top_100)} Bybit USDT Pairs by Volume!", flush=True)
            return top_100

    except Exception as e:
        print(f"⚠️ Market fetch error: {e}", flush=True)

    return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]


async def fetch_ohlcv(symbol, timeframe, limit=100):
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


def analyze_1h_bias(df_1h):
    df_1h['EMA50'] = calculate_ema(df_1h['close'], 50)
    df_1h['CCI50'] = calculate_cci(df_1h, 50)
    df_1h['CCI7'] = calculate_cci(df_1h, 7)

    last = df_1h.iloc[-2]

    # CCI 50 Zero Line + CCI 7 Pullback Condition
    long_condition = (
        (last['close'] > last['EMA50']) and
        (last['CCI50'] > 0) and
        (df_1h['CCI7'].iloc[-4:-1].min() < -80)
    )

    short_condition = (
        (last['close'] < last['EMA50']) and
        (last['CCI50'] < 0) and
        (df_1h['CCI7'].iloc[-4:-1].max() > 80)
    )

    if long_condition:
        return "BULLISH"
    elif short_condition:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# VIDEO LOGIC: PULLBACK CANDLE COVER + AUTO TP / SL
# ============================================================

def check_pullback_candle_breakout(df_5m, bias_1h):
    if df_5m is None or len(df_5m) < 30:
        return None, None, None, None, None, None, None

    # CLOSED CANDLES ONLY
    df = df_5m.iloc[:-1].copy().reset_index(drop=True)
    current_idx = len(df) - 1
    current = df.iloc[current_idx]

    # --------------------------------------------------------
    # 1. LONG ALERT LOGIC (Pullback Low Breakout)
    # --------------------------------------------------------
    if bias_1h == "BULLISH":
        recent_window = df.iloc[max(0, current_idx - 15): current_idx]
        lowest_idx = recent_window['low'].idxmin()
        lowest_candle = df.iloc[lowest_idx]

        level_to_cover = lowest_candle['high']
        bars_since_low = current_idx - lowest_idx

        if 1 <= bars_since_low <= 5:
            if current['close'] > level_to_cover and current['close'] > current['open']:
                entry = round(float(current['close']), 6)
                level = round(float(level_to_cover), 6)

                # Stop Loss: Pullback Lowest Wick - 0.15% Buffer
                sl = round(float(lowest_candle['low']) * 0.9985, 6)
                risk = entry - sl

                if risk > 0 and (risk / entry) <= 0.04:
                    tp1 = round(entry + (risk * 2.0), 6)   # 1:2 Risk to Reward
                    tp2 = round(entry + (risk * 3.5), 6)   # 1:3.5 Risk to Reward
                    candle_time = int(current['timestamp'])
                    return "LONG", entry, sl, tp1, tp2, level, candle_time

    # --------------------------------------------------------
    # 2. SHORT ALERT LOGIC (Pullback High Breakout)
    # --------------------------------------------------------
    elif bias_1h == "BEARISH":
        recent_window = df.iloc[max(0, current_idx - 15): current_idx]
        highest_idx = recent_window['high'].idxmax()
        highest_candle = df.iloc[highest_idx]

        level_to_cover = highest_candle['low']
        bars_since_high = current_idx - highest_idx

        if 1 <= bars_since_high <= 5:
            if current['close'] < level_to_cover and current['close'] < current['open']:
                entry = round(float(current['close']), 6)
                level = round(float(level_to_cover), 6)

                # Stop Loss: Pullback Highest Wick + 0.15% Buffer
                sl = round(float(highest_candle['high']) * 1.0015, 6)
                risk = sl - entry

                if risk > 0 and (risk / entry) <= 0.04:
                    tp1 = round(entry - (risk * 2.0), 6)   # 1:2 Risk to Reward
                    tp2 = round(entry - (risk * 3.5), 6)   # 1:3.5 Risk to Reward
                    candle_time = int(current['timestamp'])
                    return "SHORT", entry, sl, tp1, tp2, level, candle_time

    return None, None, None, None, None, None, None


# ============================================================
# TELEGRAM BROADCAST (WITH TP & SL)
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
        f"🚨 <b>PULLBACK BREAKOUT ALERT</b> 🚨\n\n"
        f"<b>Coin:</b> #{clean_pair} (Bybit Futures)\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"🎯 <b>Entry:</b> <code>{entry}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl}</code>\n"
        f"🎯 <b>Take Profit 1 (1:2):</b> <code>{tp1}</code>\n"
        f"🚀 <b>Take Profit 2 (1:3.5):</b> <code>{tp2}</code>\n\n"
        f"<b>Trigger Details:</b>\n"
        f"• 1H Trend & CCI 50/7: Aligned\n"
        f"• 5M Pullback Level: <code>{level}</code>\n"
        f"• Status: <b>Level Covered & Candle Closed</b> ✅\n\n"
        f"📊 <b>Chart:</b> <a href='{tv_chart_url}'>Open on TradingView ↗</a>\n"
    )

    try:
        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        print(f"\n🔥 [ALERT SENT] {clean_pair} {side} | Entry: {entry} | SL: {sl} | TP1: {tp1}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram broadcast failed: {e}", flush=True)


# ============================================================
# MAIN SCANNER LOOP
# ============================================================

async def main():
    try:
        await tg_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="🚀 <b>Pullback Candle-Breakout Bot is LIVE!</b>\nScanning 75 Pairs with Auto TP & SL Targets...",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"⚠️ Startup message failed: {e}", flush=True)

    symbols = []
    while not symbols:
        try:
            symbols = await get_top_100_symbols()
        except Exception:
            print("Connecting to Bybit... retrying in 5s.", flush=True)
            await asyncio.sleep(5)

    print("🚀 Scanner Active: Pullback Candle Cover + Auto TP/SL Calculation...", flush=True)

    while True:
        try:
            total = len(symbols)

            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(':')[0]
                print(f"🔍 [{idx}/{total}] Scanning: {clean_name}...", end="\r", flush=True)

                try:
                    df_1h = await fetch_ohlcv(symbol, '1h', limit=60)
                    df_5m = await fetch_ohlcv(symbol, '5m', limit=60)

                    if df_1h is not None and df_5m is not None:
                        bias_1h = analyze_1h_bias(df_1h)

                        if bias_1h != "NEUTRAL":
                            (
                                side,
                                entry,
                                sl,
                                tp1,
                                tp2,
                                level,
                                candle_time
                            ) = check_pullback_candle_breakout(df_5m, bias_1h)

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
