import os
import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
from telegram import Bot

from database import init_db, save_trade, get_open_trades, update_trade_tp1, close_trade

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
        tickers = await bybit.fetch_tickers(
            params={'category': 'linear'}
        )

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
                    usdt_pairs.append({
                        'symbol': symbol,
                        'volume': float(vol)
                    })

        usdt_pairs.sort(
            key=lambda x: x['volume'],
            reverse=True
        )

        top_75 = [
            item['symbol']
            for item in usdt_pairs[:75]
        ]

        if len(top_75) > 0:
            print(
                f"✅ Successfully loaded "
                f"{len(top_75)} Bybit USDT Pairs by Volume!"
            )
            return top_75

    except Exception as e:
        print(f"⚠️ Market fetch error: {e}")

    return [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "XRP/USDT:USDT",
        "DOGE/USDT:USDT"
    ]


async def fetch_ohlcv(symbol, timeframe, limit=100):
    try:
        raw = await bybit.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            params={'category': 'linear'}
        )

        if not raw or len(raw) < 55:
            return None

        df = pd.DataFrame(
            raw,
            columns=[
                'timestamp',
                'open',
                'high',
                'low',
                'close',
                'volume'
            ]
        )

        return df

    except Exception:
        return None


def analyze_1h_indicators(df_1h):
    df_1h['EMA50'] = calculate_ema(
        df_1h['close'],
        50
    )

    df_1h['CCI50'] = calculate_cci(
        df_1h,
        50
    )

    df_1h['CCI7'] = calculate_cci(
        df_1h,
        7
    )

    last = df_1h.iloc[-2]

    long_condition = (
        (last['close'] > last['EMA50']) and
        (last['CCI50'] > 0) and
        (df_1h['CCI7'].iloc[-4:-1].min() < -100)
    )

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


# ============================================================
# VALID 5M SMC CHoCH + RETEST
# ============================================================

def check_5m_choch_and_retest(df_5m, bias_1h):
    if df_5m is None or len(df_5m) < 30:
        return None, None, None, None, None

    highs = df_5m['high'].values
    lows = df_5m['low'].values
    closes = df_5m['close'].values
    opens = df_5m['open'].values

    current_idx = len(df_5m) - 1

    swing_highs = []
    swing_lows = []

    for i in range(2, current_idx - 1):
        if (
            highs[i] > highs[i - 1]
            and highs[i] > highs[i + 1]
        ):
            swing_highs.append(i)

        if (
            lows[i] < lows[i - 1]
            and lows[i] < lows[i + 1]
        ):
            swing_lows.append(i)

    # 1. BULLISH VALID CHoCH
    if bias_1h == "BULLISH":
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return None, None, None, None, None

        for h_pos in range(len(swing_highs) - 1, 0, -1):
            previous_high_idx = swing_highs[h_pos - 1]
            lh_idx = swing_highs[h_pos]

            if highs[lh_idx] >= highs[previous_high_idx]:
                continue

            lows_between = [
                x for x in swing_lows
                if previous_high_idx < x < lh_idx
            ]

            if not lows_between:
                continue

            current_ll_idx = lows_between[-1]

            previous_lows = [
                x for x in swing_lows
                if x < previous_high_idx
            ]

            if not previous_lows:
                continue

            previous_low_idx = previous_lows[-1]

            if lows[current_ll_idx] >= lows[previous_low_idx]:
                continue

            choch_level = highs[lh_idx]
            breakout_idx = None

            for k in range(current_ll_idx + 1, current_idx):
                if (
                    closes[k] > choch_level
                    and closes[k] > opens[k]
                ):
                    breakout_idx = k
                    break

            if breakout_idx is None:
                continue

            curr = df_5m.iloc[-1]

            if current_idx <= breakout_idx:
                continue

            retest_condition = (
                curr['low'] <= choch_level * 1.0015
                and curr['close'] >= choch_level * 0.998
            )

            if not retest_condition:
                continue

            sl_level = df_5m['low'].iloc[-12:].min()
            sl = round(sl_level * 0.999, 4)
            entry = round(curr['close'], 4)
            risk = entry - sl

            if risk > 0 and (risk / entry) < 0.035:
                tp1 = round(entry + (risk * 2), 4)
                tp2 = round(entry + (risk * 3.5), 4)
                return "BUY", entry, sl, tp1, tp2

            return None, None, None, None, None

    # 2. BEARISH VALID CHoCH
    elif bias_1h == "BEARISH":
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return None, None, None, None, None

        for l_pos in range(len(swing_lows) - 1, 0, -1):
            previous_low_idx = swing_lows[l_pos - 1]
            hl_idx = swing_lows[l_pos]

            if lows[hl_idx] <= lows[previous_low_idx]:
                continue

            highs_between = [
                x for x in swing_highs
                if previous_low_idx < x < hl_idx
            ]

            if not highs_between:
                continue

            current_hh_idx = highs_between[-1]

            previous_highs = [
                x for x in swing_highs
                if x < previous_low_idx
            ]

            if not previous_highs:
                continue

            previous_high_idx = previous_highs[-1]

            if highs[current_hh_idx] <= highs[previous_high_idx]:
                continue

            choch_level = lows[hl_idx]
            breakout_idx = None

            for k in range(current_hh_idx + 1, current_idx):
                if (
                    closes[k] < choch_level
                    and closes[k] < opens[k]
                ):
                    breakout_idx = k
                    break

            if breakout_idx is None:
                continue

            curr = df_5m.iloc[-1]

            if current_idx <= breakout_idx:
                continue

            retest_condition = (
                curr['high'] >= choch_level * 0.9985
                and curr['close'] <= choch_level * 1.002
            )

            if not retest_condition:
                continue

            sl_level = df_5m['high'].iloc[-12:].max()
            sl = round(sl_level * 1.001, 4)
            entry = round(curr['close'], 4)
            risk = sl - entry

            if risk > 0 and (risk / entry) < 0.035:
                tp1 = round(entry - (risk * 2), 4)
                tp2 = round(entry - (risk * 3.5), 4)
                return "SELL", entry, sl, tp1, tp2

            return None, None, None, None, None

    return None, None, None, None, None


# ============================================================
# TELEGRAM
# ============================================================

async def broadcast_signal(symbol, side, entry, sl, tp1, tp2):
    pair_display = symbol.split(':')[0]
    direction_text = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

    msg = (
        f"🚨 <b>JK Analyzing</b> 🚨\n\n"
        f"<b>Exchange:</b> Bybit Futures\n"
        f"<b>Pair:</b> #{pair_display.replace('/', '')}\n"
        f"<b>Direction:</b> {direction_text}\n\n"
        f"<b>Conformations Passed:</b>\n"
        f"• 1H 50 EMA & 50 CCI: Confirmed\n"
        f"• 1H 7 CCI Exhaustion: Completed\n"
        f"• 5M CHoCH: Candle Body Breakout Confirmed\n"
        f"• 5M Retest: Key Level Retested\n\n"
        f"🎯 <b>Entry:</b> {entry}\n"
        f"🛑 <b>Stop Loss:</b> {sl}\n"
        f"🎯 <b>Take Profit 1:</b> {tp1} (1:2)\n"
        f"🚀 <b>Take Profit 2:</b> {tp2} (1:3.5+)\n"
    )

    await tg_bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode="HTML"
    )

    await save_trade(symbol, side, entry, sl, tp1, tp2)

    print(
        f"\n🔥 [VALID CHoCH SIGNAL] "
        f"Sent to Telegram: {pair_display} {side}"
    )


# ============================================================
# OPEN TRADE MONITOR (Background Only)
# ============================================================

async def monitor_open_trades():
    trades = await get_open_trades()

    for trade in trades:
        t_id, sym, side, entry, sl, tp1, tp2, tp1_hit, _ = trade

        try:
            ticker = await bybit.fetch_ticker(
                sym,
                params={'category': 'linear'}
            )
            last_price = ticker['last']

            if side == "BUY":
                if not tp1_hit and last_price >= tp1:
                    await update_trade_tp1(t_id)

                elif last_price >= tp2:
                    # TP2 Hit -> Trade Closed (Now eligible for new signals)
                    await close_trade(t_id, "CLOSED_PROFIT")

                elif last_price <= sl:
                    # SL Hit -> Trade Closed (Now eligible for new signals)
                    await close_trade(t_id, "CLOSED_LOSS")

            elif side == "SELL":
                if not tp1_hit and last_price <= tp1:
                    await update_trade_tp1(t_id)

                elif last_price <= tp2:
                    # TP2 Hit -> Trade Closed (Now eligible for new signals)
                    await close_trade(t_id, "CLOSED_PROFIT")

                elif last_price >= sl:
                    # SL Hit -> Trade Closed (Now eligible for new signals)
                    await close_trade(t_id, "CLOSED_LOSS")

        except Exception:
            continue


# ============================================================
# MAIN
# ============================================================

async def main():
    await init_db()

    symbols = []

    while not symbols:
        try:
            symbols = await get_top_75_symbols()
        except Exception:
            print("Connecting to Bybit... retrying in 5s.")
            await asyncio.sleep(5)

    print(
        "🚀 Scanner Active: Accurate 1H SMC + 5M Valid CHoCH "
        "(One Trade per Coin until TP2 or SL Hit)..."
    )

    while True:
        try:
            # 1. Update active trades in background
            await monitor_open_trades()

            # 2. Database එකෙන් දැනට ක්‍රියාත්මක වන open trades ඇති කාසි ලැයිස්තුව ලබා ගැනීම
            open_trades = await get_open_trades()
            active_symbols = {trade[1] for trade in open_trades}

            total = len(symbols)

            for idx, symbol in enumerate(symbols, 1):
                clean_name = symbol.split(':')[0]
                print(
                    f"🔍 [{idx}/{total}] Scanning: {clean_name}...",
                    end="\r"
                )

                # මෙම coin එක දැනටමත් active trade එකක ඇත්නම් (TP2 හෝ SL නොවී), scan කිරීම මඟහරින්න
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
                                # Signal එක යැවූ වහාම active_symbols එකට එකතු කිරීම
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
