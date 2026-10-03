import aiosqlite
from datetime import datetime, timedelta, timezone

DB_NAME = "trades.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp1_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP
            )
        """)
        await db.commit()

async def save_trade(symbol, side, entry, sl, tp1, tp2):
    async with aiosqlite.connect(DB_NAME) as db:
        now_utc = datetime.now(timezone.utc)
        await db.execute("""
            INSERT INTO trades (symbol, side, entry, sl, tp1, tp2, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, entry, sl, tp1, tp2, now_utc))
        await db.commit()

async def get_open_trades():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT id, symbol, side, entry, sl, tp1, tp2, tp1_hit, status 
            FROM trades 
            WHERE status = 'OPEN'
        """) as cursor:
            return await cursor.fetchall()

async def update_trade_tp1(trade_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE trades SET tp1_hit = 1 WHERE id = ?", (trade_id,))
        await db.commit()

async def close_trade(trade_id, status):
    async with aiosqlite.connect(DB_NAME) as db:
        now_utc = datetime.now(timezone.utc)
        await db.execute("""
            UPDATE trades 
            SET status = ?, closed_at = ? 
            WHERE id = ?
        """, (status, now_utc, trade_id))
        await db.commit()

async def get_weekly_performance_data():
    """
    පසුගිය දින 7 තුළ දිනපතා Signals, Wins, Losses සහ PnL ගණනය කිරීම
    """
    async with aiosqlite.connect(DB_NAME) as db:
        now_utc = datetime.now(timezone.utc)
        seven_days_ago = now_utc - timedelta(days=7)
        async with db.execute("""
            SELECT date(created_at), status, tp1_hit 
            FROM trades 
            WHERE created_at >= ?
            ORDER BY created_at ASC
        """, (seven_days_ago,)) as cursor:
            rows = await cursor.fetchall()

    daily_stats = {}
    for i in range(7):
        day_date = (seven_days_ago + timedelta(days=i+1)).strftime('%Y-%m-%d')
        day_name = (seven_days_ago + timedelta(days=i+1)).strftime('%a')
        daily_stats[day_date] = {
            'day': day_name,
            'signals': 0,
            'wins': 0,
            'losses': 0,
            'pnl_r': 0.0
        }

    for row in rows:
        d_str, status, tp1_hit = row
        if d_str in daily_stats:
            daily_stats[d_str]['signals'] += 1
            if status == 'CLOSED_PROFIT':
                daily_stats[d_str]['wins'] += 1
                daily_stats[d_str]['pnl_r'] += 2.5  # Full TP2 (1:2.5 RR)
            elif status == 'CLOSED_LOSS':
                daily_stats[d_str]['losses'] += 1
                daily_stats[d_str]['pnl_r'] -= 1.0  # SL hit (-1R)
            elif status == 'OPEN' and tp1_hit:
                daily_stats[d_str]['wins'] += 1
                daily_stats[d_str]['pnl_r'] += 1.5  # TP1 Partial (1:1.5 RR)

    return daily_stats
