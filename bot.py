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
    get_weekly_performance_data
)

# --- Configurations ---
TELEGRAM_BOT_TOKEN = "8983892388:AAFs6EgNNj5uqPNfUmn6Qap3kZAzNGq6IYM"
TELEGRAM_CHAT_ID = "-1004306671705"

bybit = ccxt.bybit({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'linear',
    }
})
tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)


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

        return pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    except Exception:
        return None


def analyze_1h_indicators(df_1h):
    df_1h['EMA50'] = calculate_ema(df_1h['close'], 50)
    df_1h['CCI50'] = calculate_cci(df_1h, 50)
    df_1h['CCI7'] = calculate_cci(df_1h, 7)

    last = df_1h.iloc[-2]

    # Strong Trend Conformation (No Counter-Trend Trading)
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
# HIGH WIN-RATE SMC CHoCH + REJECTION RETEST LOGIC
# ============================================================

def check_5m_choch_and_retest(df_5m, bias_1h):
    if df_5m is None or len(df_5m) < 50:
        return None, None, None, None, None

    highs = df_5m['high'].values
    lows = df_5m['low'].values
    closes = df_5m['close'].values
    opens = df_5m['open'].values

    current_idx = len(df_5m) - 1

    # 1. Major Swing Points හඳුනා ගැනීම
    swing_highs = []
    swing_lows = []

    for i in range(3, current_idx - 2):
        if all(highs[i] > highs[i - k] for k in range(1, 4)) and all(highs[i] > highs[i + k] for k in range(1, 4)):
            swing_highs.append(i)
        if all(lows[i] < lows[i - k] for k in range(1, 4)) and all(lows[i] < lows[i + k] for k in range(1, 4)):
            swing_lows.append(i)

    # --------------------------------------------------------
    # BEARISH SETUP (SELL)
    # --------------------------------------------------------
    if bias_1h == "BEARISH":
        recent_sh = [i for i in swing_highs if i > current_idx - 35]
        if not recent_sh:
            return None, None, None, None, None

        hh_idx = max(recent_sh, key=lambda x: highs[x])
        valid_hls = [i for i in swing_lows if i < hh_idx and i > hh_idx - 20]
        if not valid_hls:
            return None, None, None, None, None

        hl_idx = valid_hls[-1]
        choch_level = lows[hl_idx]

        # Candle Body එකකින් HL එක කැඩීම
        breakout_idx = None
        for k in range(hh_idx + 1, current_idx):
            if closes[k] < choch_level and closes[k] < opens[k]:
                breakout_idx = k
                break

        # Breakout එක සිදු වී candles 12ක් ඇතුළත Fresh Retest විය යුතුයි
        if breakout_idx is not None and (current_idx - breakout_idx) <= 12:
            curr = df_5m.iloc[-1]
            prev = df_5m.iloc[-2]

            # Level එක Touch වීම සහ Rejection එකක් තහවුරු වීම (Bearish Confirmation Candle)
            level_touched = (prev['high'] >= choch_level * 0.9985 and prev['close'] <= choch_level * 1.003) or \
                            (curr['high'] >= choch_level * 0.9985 and curr['close'] <= choch_level * 1.003)
            
            rejection_candle = curr['close'] < curr['open']  # Bearish close අනිවාර්යයි

            if level_touched and rejection_candle:
                # සැබෑ Major Swing High එකට ඉහළින් 0.35% ක Safe Buffer SL එකක්
                sl = round(highs[hh_idx] * 1.0035, 4)
                entry = round(curr['close'], 4)
                risk = sl - entry

                # Safe Risk to Reward (අවම 0.3%, උපරිම 3.0% risk)
                if risk > 0 and 0.003 <= (risk / entry) <= 0.035:
                    tp1 = round(entry - (risk * 1.5), 4)  # High-probability 1:1.5 TP1
                    tp2 = round(entry - (risk * 2.5), 4)  # 1:2.5 TP2
                    return "SELL", entry, sl, tp1, tp2

    # --------------------------------------------------------
    # BULLISH SETUP (BUY)
    # --------------------------------------------------------
    elif bias_1h == "BULLISH":
        recent_sl = [i for i in swing_lows if i > current_idx - 35]
        if not recent_sl:
            return None, None, None, None, None

        ll_idx = min(recent_sl, key=lambda x: lows[x])
        valid_lhs = [i for i in swing_highs if i < ll_idx and i > ll_idx - 20]
        if not valid_lhs:
            return None, None, None, None, None

        lh_idx = valid_lhs[-1]
        choch_level = highs[lh_idx]

        # Candle Body එකකින් LH එක කැඩීම
        breakout_idx = None
        for k in range(ll_idx + 1, current_idx):
            if closes[k] > choch_level and closes[k] > opens[k]:
                breakout_idx = k
                break

        # Breakout එක සිදු වී candles 12ක් ඇතුළත Fresh Retest විය යුතුයි
        if breakout_idx is not None and (current_idx - breakout_idx) <= 12:
            curr = df_5m.iloc[-1]
            prev = df_5m.iloc[-2]

            # Level එක Touch වීම සහ Rejection එකක් තහවුරු වීම (Bullish Confirmation Candle)
            level_touched = (prev['low'] <= choch_level * 1.0015 and prev['close'] >= choch_level * 0.997) or \
                            (curr['low'] <= choch_level * 1.0015 and curr['close'] >= choch_level * 0.997)

            rejection_candle = curr['close'] > curr['open']  # Bullish close අනිවාර්යයි

            if level_touched and rejection_candle:
                # සැබෑ Major Swing Low එකට පහළින් 0.35% ක Safe Buffer SL එකක්
                sl = round(lows[ll_idx] * 0.9965, 4)
                entry = round(curr['close'], 4)
                risk = entry - sl

                if risk > 0 and 0.003 <= (risk / entry) <= 0.035:
                    tp1 = round(entry + (risk * 1.5), 4)  # High-probability 1:1.5 TP1
                    tp2 = round(entry + (risk * 2.5), 4)  # 1:2.5 TP2
                    return "BUY", entry, sl, tp1, tp2

    return None, None, None, None, None


# ============================================================
# TELEGRAM BROADCAST
# ============================================================

async def broadcast_signal(symbol, side, entry, sl, tp1, tp2):
    pair_display = symbol.split(':')[0]
    clean_pair = pair_display.replace('/', '')
    direction_text = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

    tv_chart_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{clean_pair}.P"

    msg = (
        f"🚨 <b>JK Analyzing</b> 🚨\n\n"
        f"<b>Exchange:</b> Bybit Futures\n"
        f"<b>Pair:</b> #{clean_pair}\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"<b>Conformations Passed:</b>\n"
        f"• 1H Trend & Momentum: Aligned\n"
        f"• 5M Major CHoCH: Body Breakout\n"
        f"• 5M Retest: Level Confirmed with Rejection Candle\n\n"
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
    print(f"\n🔥 [HIGH-WIN CHoCH SIGNAL] Sent to Telegram: {pair_display} {side}")


# ============================================================
# WEEKLY PERFORMANCE REPORT TASK
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
        sigs = stats['signals']
        w = stats['wins']
        l = stats['losses']
        pnl = stats['pnl_r']

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

    report_msg = (
        f"📊 <b>JK ANALYZING — WEEKLY REPORT</b> 📊\n"
        f"<i>Automated Weekly Performance Sheet</i>\n\n"
        + "\n".join(lines) + "\n\n"
        f"💰 <b>Total Net Return:</b> <code>{tot_pnl_str}</code> {status_icon}\n"
        f"🎯 <b>Accuracy Rate:</b> <code>{overall_win_rate}%</code>\n"
        f"📅 <i>Report generated on {datetime.utcnow().strftime('%Y-%m-%d')}</i>"
    )

    await tg_bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=report_msg,
        parse_mode="HTML"
    )


async def schedule_weekly_report():
    while True:
        now = datetime.utcnow()
        if now.weekday() == 6 and now.hour == 23 and now.minute >= 55:
            await send_weekly_report()
            await asyncio.sleep(3600)
        await asyncio.sleep(60)


# ============================================================
# OPEN TRADE MONITOR (Background Only)
# ============================================================

async def monitor_open_trades():
    trades = await get_open_trades()

    for trade in trades:
        t_id, sym, side, entry, sl, tp1, tp2, tp1_hit, _ = trade

        try:
            ticker = await bybit.fetch_ticker(sym, params={'category': 'linear'})
            last_price = ticker['last']

            if side == "BUY":
                if not tp1_hit and last_price >= tp1:
                    await update_trade_tp1(t_id)
                elif last_price >= tp2:
                    await close_trade(t_id, "CLOSED_PROFIT")
                elif last_price <= sl:
                    await close_trade(t_id, "CLOSED_LOSS")

            elif side == "SELL":
                if not tp1_hit and last_price <= tp1:
                    await update_trade_tp1(t_id)
                elif last_price <= tp2:
                    await close_trade(t_id, "CLOSED_PROFIT")
                elif last_price >= sl:
                    await close_trade(t_id, "CLOSED_LOSS")

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
            symbols = await get_top_75_symbols()
        except Exception:
            print("Connecting to Bybit... retrying in 5s.")
            await asyncio.sleep(5)

    print(
        "🚀 Scanner Active: Protected-SL SMC + Rejection Retest "
        "(Targeting 70%+ Win Rate)..."
    )

    while True:
        try:
            await monitor_open_trades()

            open_trades = await get_open_trades()
            active_symbols = {trade[1] for trade in open_trades}

            total = len(symbols)

            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(':')[0]
                print(
                    f"🔍 [{idx}/{total}] Scanning: {clean_name}...",
                    end="\r"
                )

                if symbol in active_symbols:
                    continue

                try:
                    df_1h = await fetch_ohlcv(symbol, '1h', limit=60)
                    df_5m = await fetch_ohlcv(symbol, '5m', limit=60)

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
                                active_symbols.add(symbol)

                    await asyncio.sleep(0.3)

                except Exception:
                    continue

            print(
                f"\n🔄 Completed 1 cycle of {total} pairs. "
                f"Waiting 20s for next cycle..."
            )

        except Exception as e:
            print(f"\n⚠️ Main loop alert: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(20)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped by user.")
