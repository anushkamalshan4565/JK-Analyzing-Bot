import aiosqlite

DB_NAME = "trades.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS active_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp1_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'OPEN'
            )
        ''')
        await db.commit()

async def save_trade(symbol, side, entry, sl, tp1, tp2):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO active_trades (symbol, side, entry, sl, tp1, tp2)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (symbol, side, entry, sl, tp1, tp2))
        await db.commit()

async def get_open_trades():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM active_trades WHERE status = 'OPEN'") as cursor:
            return await cursor.fetchall()

async def update_trade_tp1(trade_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE active_trades SET tp1_hit = 1 WHERE id = ?", (trade_id,))
        await db.commit()

async def close_trade(trade_id, status):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE active_trades SET status = ? WHERE id = ?", (status, trade_id))
        await db.commit()