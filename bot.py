import os
import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
from telegram import Bot

from database import init_db, save_trade, get_open_trades, update_trade_tp1, close_trade
from pnl_card import generate_pnl_card

# --- Configurations ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8083892308:AAF4G6EghNjj5uqPNfUmn6Qap3kZAZNqQdIYH")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004306671705")

bybit = ccxt.bybit({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'linear',
    }
})
tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Signal spam වීම වැළැක්වීමට Cooldown Tracker
alerted_cooldown = {}

# --- Indicator Calculations ---
def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_cci(df, length):
    tp = (df['high'] + df['low'] + df['close']) / 3
    sma = tp.rolling(window=length).mean()
    mad = tp.rolling(window=length).apply(lambda x: np.mean(np.abs(x - np.mean(x))))
    mad = mad.replace(0, 0.00001)
    cci = (tp - sma) / (0.015 * mad)
    return cci

async def get_top_75_symbols():
    try:
        markets = await bybit.load_markets()
        tickers = await bybit.fetch_tickers(params={'category': 'linear'})
        
        usdt_pairs = []
        for symbol, ticker in tickers.items():
            market = markets.get(symbol)
            if market and market.get('linear') and market.get('contract') and market.get('settle') == 'USDT':
                vol = ticker.get('quoteVolume') or 0
                if vol > 0:
                    usdt_pairs.append({'symbol': symbol, 'volume': float(vol)})
        
        usdt_pairs.sort(key=lambda x: x['volume'], reverse=True)
        top_75 = [item['symbol'] for item in usdt_pairs[:75]]
        if len(top_75) > 0:
            print(f"✅ Successfully loaded {len(top_75)} Bybit USDT Pairs by Volume!")
            return top_75
    except Exception as e:
        print(f"⚠️ Market fetch error: {e}")
        
    return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]

async def fetch_ohlcv(symbol, timeframe, limit=100):
    try:
        raw = await bybit.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, params={'category': 'linear'})
        if not raw or len(raw) < 55:
            return None
        df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception:
        return None

def analyze_1h_indicators(df_1h):
    """
    1-Hour Chart Conformations:
    Long: Close > 50 EMA, 50 CCI > 0, 7 CCI was Oversold (<-100) & turning up
    Short: Close < 50 EMA, 50 CCI < 0, 7 CCI was Overbought (>100) & turning down
    """
    df_1h['EMA50'] = calculate_ema(df_1h['close'], 50)
    df_1h['CCI50'] = calculate_cci(df_1h, 50)
    df_1h['CCI7']  = calculate_cci(df_1h, 7)

    # අවසන් closed candle එක පරීක්ෂාව (-2)
    last = df_1h.iloc[-2]

    # Long Setup on 1H
    long_condition = (
        (last['close'] > last['EMA50']) and
        (last['CCI50'] > 0) and
        (df_1h['CCI7'].iloc[-4:-1].min() < -100)
    )

    # Short Setup on 1H
    short_condition = (
        (last['close'] < last['EMA50']) and
        (last['CCI50'] < 0) and
        (df_1h['CCI7'].iloc[-4:-1].max() > 100)
    )

    if long_condition:
        return "BULLISH"
    elif short_condition:
        return "BEARISH"
    return "NEUTRAL"

def check_5m_choch_and_retest(df_5m, bias_1h):
    """
    5-Minute Chart SMC Rules:
    1. CHoCH: Candle close beyond Swing Level
    2. Retest: Current pullback touches the broken swing zone
    """
    # පෙර හැදුණු Candles වලින් Swing High/Low නිර්ණය
    swing_high = df_5m['high'].iloc[-20:-4].max()
    swing_low = df_5m['low'].iloc[-20:-4].min()

    # පසුගිය කැන්ඩල් 3 තුළ CHoCH එකක් සිදුවී ඇතිදැයි බැලීම
    recent_closes = df_5m['close'].iloc[-4:-1]
    current_candle = df_5m.iloc[-1]

    # --- Bullish Conformation ---
    if bias_1h == "BULLISH":
        had_choch = (recent_closes > swing_high).any()
        # Broken swing high zone එක retest කිරීම
        valid_retest = (current_candle['low'] <= swing_high * 1.002) and (current_candle['close'] >= swing_high * 0.998)

        if had_choch and valid_retest:
            sl = round(swing_low * 0.9985, 4)
            entry = round(current_candle['close'], 4)
            risk = entry - sl
            if risk > 0 and (risk / entry) < 0.04:  # SL එක 4% කට වඩා වැඩි නම් risk එක වැඩියි
                tp1 = round(entry + (risk * 2), 4)
                tp2 = round(entry + (risk * 3.5), 4)  # 1:3+ RR
                return "BUY", entry, sl, tp1, tp2

    # --- Bearish Conformation ---
    elif bias_1h == "BEARISH":
        had_choch = (recent_closes < swing_low).any()
        # Broken swing low zone එක retest කිරීම
        valid_retest = (current_candle['high'] >= swing_low * 0.998) and (current_candle['close'] <= swing_low * 1.002)

        if had_choch and valid_retest:
            sl = round(swing_high * 1.0015, 4)
            entry = round(current_candle['close'], 4)
            risk = sl - entry
            if risk > 0 and (risk / entry) < 0.04:
                tp1 = round(entry - (risk * 2), 4)
                tp2 = round(entry - (risk * 3.5), 4)  # 1:3+ RR
                return "SELL", entry, sl, tp1, tp2

    return None, None, None, None, None

async def broadcast_signal(symbol, side, entry, sl, tp1, tp2):
    pair_display = symbol.split(':')[0]
    direction_text = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
    
    msg = (
        f"🚨 <b>HIGH-PROBABILITY SMC SIGNAL</b> 🚨\n\n"
        f"<b>Exchange:</b> Bybit Futures\n"
        f"<b>Pair:</b> #{pair_display.replace('/', '')}\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"<b>Conformations Passed:</b>\n"
        f"• 1H EMA 50 Trend Filter: Passed\n"
        f"• 1H 50 CCI Zero Line: Valid\n"
        f"• 1H 7 CCI Exhaustion: Completed\n"
        f"• 5M CHoCH: Structure Broken\n"
        f"• 5M Retest: Key Level Touched\n\n"
        f"🎯 <b>Entry:</b> {entry}\n"
        f"🛑 <b>Stop Loss:</b> {sl}\n"
        f"🎯 <b>Take Profit 1:</b> {tp1} (1:2)\n"
        f"🚀 <b>Take Profit 2:</b> {tp2} (1:3.5+)\n"
    )
    await tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML")
    await save_trade(symbol, side, entry, sl, tp1, tp2)
    print(f"\n🔥 [VALID SIGNAL] Sent to Telegram: {pair_display} {side}")

async def monitor_open_trades():
    trades = await get_open_trades()
    for trade in trades:
        t_id, sym, side, entry, sl, tp1, tp2, tp1_hit, _ = trade
        try:
            ticker = await bybit.fetch_ticker(sym, params={'category': 'linear'})
            last_price = ticker['last']
            pair_clean = sym.split(':')[0]

            if side == "BUY":
                raw_roi = ((last_price - entry) / entry) * 100
                pnl_pct = raw_roi * 10

                if not tp1_hit and last_price >= tp1:
                    card = generate_pnl_card(pair_clean, side, entry, tp1, pnl_pct, "TP 1 HIT")
                    await tg_bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=card, caption=f"🎯 {pair_clean} TP 1 Achieved!")
                    await update_trade_tp1(t_id)

                elif last_price >= tp2:
                    card = generate_pnl_card(pair_clean, side, entry, tp2, pnl_pct, "TP 2 (1:3.5) HIT")
                    await tg_bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=card, caption=f"🚀 {pair_clean} Target (1:3.5) Hit!")
                    await close_trade(t_id, "CLOSED_PROFIT")

                elif last_price <= sl:
                    loss_pct = (((sl - entry) / entry) * 100) * 10
                    card = generate_pnl_card(pair_clean, side, entry, sl, loss_pct, "STOP LOSS")
                    await tg_bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=card, caption=f"🛑 {pair_clean} Stop Loss Triggered.")
                    await close_trade(t_id, "CLOSED_LOSS")

            elif side == "SELL":
                raw_roi = ((entry - last_price) / entry) * 100
                pnl_pct = raw_roi * 10

                if not tp1_hit and last_price <= tp1:
                    card = generate_pnl_card(pair_clean, side, entry, tp1, pnl_pct, "TP 1 HIT")
                    await tg_bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=card, caption=f"🎯 {pair_clean} TP 1 Achieved!")
                    await update_trade_tp1(t_id)

                elif last_price <= tp2:
                    card = generate_pnl_card(pair_clean, side, entry, tp2, pnl_pct, "TP 2 (1:3.5) HIT")
                    await tg_bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=card, caption=f"🚀 {pair_clean} Target (1:3.5) Hit!")
                    await close_trade(t_id, "CLOSED_PROFIT")

                elif last_price >= sl:
                    loss_pct = (((entry - sl) / entry) * 100) * 10
                    card = generate_pnl_card(pair_clean, side, entry, sl, loss_pct, "STOP LOSS")
                    await tg_bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=card, caption=f"🛑 {pair_clean} Stop Loss Triggered.")
                    await close_trade(t_id, "CLOSED_LOSS")

        except Exception:
            continue

async def main():
    await init_db()
    
    symbols = []
    while not symbols:
        try:
            symbols = await get_top_75_symbols()
        except Exception:
            print("Connecting to Bybit... retrying in 5s.")
            await asyncio.sleep(5)
            
    print("🚀 Scanner Active: Accurate 1H SMC + 5M CHoCH & Retest verification...")

    while True:
        try:
            await monitor_open_trades()

            total = len(symbols)
            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(':')[0]
                print(f"🔍 [{idx}/{total}] Scanning: {clean_name}...", end="\r")

                try:
                    df_1h = await fetch_ohlcv(symbol, '1h', limit=60)
                    df_5m = await fetch_ohlcv(symbol, '5m', limit=60)

                    if df_1h is not None and df_5m is not None:
                        bias_1h = analyze_1h_indicators(df_1h)

                        if bias_1h != "NEUTRAL":
                            side, entry, sl, tp1, tp2 = check_5m_choch_and_retest(df_5m, bias_1h)

                            if side and entry:
                                # එකම coin එකට විනාඩි 60ක් යනකම් නැවත signal නොයැවීම (Cooldown)
                                now = asyncio.get_event_loop().time()
                                last_alert_time = alerted_cooldown.get(symbol, 0)
                                
                                if now - last_alert_time > 3600:
                                    await broadcast_signal(symbol, side, entry, sl, tp1, tp2)
                                    alerted_cooldown[symbol] = now

                    await asyncio.sleep(0.3)
                except Exception:
                    continue

            print(f"\n🔄 Completed 1 cycle of {total} pairs. Waiting 20s for next cycle...")

        except Exception as e:
            print(f"\n⚠️️ Main loop alert: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(20)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped by user.")