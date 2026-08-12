"""
Aegis | Server Guardian — слой работы с базой данных (SQLite/aiosqlite).

ВАЖНО про Render: если ты не подключил persistent disk, файл БД
может обнулиться при передеплое. Смотри инструкцию в README по
подключению Render Disk или переносу на внешний Postgres.
"""

import json
import time
import aiosqlite

import config

_db: aiosqlite.Connection | None = None


async def init_db():
    """Открывает соединение и создаёт таблицы, если их нет. Вызывать один раз при старте."""
    global _db
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER,
            antinuke_enabled INTEGER DEFAULT 1,
            antispam_enabled INTEGER DEFAULT 1,
            raidjoin_enabled INTEGER DEFAULT 1,
            panic_mode INTEGER DEFAULT 0,
            antinuke_punishment TEXT DEFAULT 'strip_roles',
            spam_punishment TEXT DEFAULT 'mute',
            dm_owner_on_threat INTEGER DEFAULT 1,
            settings_json TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS whitelist_users (
            guild_id INTEGER,
            user_id INTEGER,
            added_by INTEGER,
            added_at INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS whitelist_roles (
            guild_id INTEGER,
            role_id INTEGER,
            added_by INTEGER,
            added_at INTEGER,
            PRIMARY KEY (guild_id, role_id)
        );

        CREATE TABLE IF NOT EXISTS incident_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            action_type TEXT,
            details TEXT,
            punishment TEXT,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            created_by INTEGER,
            created_at INTEGER,
            data_json TEXT
        );

        CREATE TABLE IF NOT EXISTS stats (
            guild_id INTEGER,
            metric TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, metric)
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS economy (
            guild_id INTEGER,
            user_id INTEGER,
            balance INTEGER DEFAULT 0,
            last_daily INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )
    await _db.commit()


def db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _db


# ─── guild_config ────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "log_channel_id": None,
    "antinuke_enabled": 1,
    "antispam_enabled": 1,
    "raidjoin_enabled": 1,
    "panic_mode": 0,
    "antinuke_punishment": config.DEFAULT_ANTINUKE_PUNISHMENT,
    "spam_punishment": config.DEFAULT_SPAM_PUNISHMENT,
    "dm_owner_on_threat": 1,
}


async def get_guild_config(guild_id: int) -> dict:
    cur = await db().execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
    row = await cur.fetchone()
    if row is None:
        await db().execute("INSERT INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        await db().commit()
        return {**DEFAULT_CONFIG, "guild_id": guild_id}
    return dict(row)


async def set_guild_config(guild_id: int, **kwargs):
    await get_guild_config(guild_id)  # ensure row exists
    keys = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [guild_id]
    await db().execute(f"UPDATE guild_config SET {keys} WHERE guild_id = ?", values)
    await db().commit()


# ─── whitelist ───────────────────────────────────────────────────

async def add_whitelist_user(guild_id: int, user_id: int, added_by: int):
    await db().execute(
        "INSERT OR REPLACE INTO whitelist_users (guild_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, added_by, int(time.time())),
    )
    await db().commit()


async def remove_whitelist_user(guild_id: int, user_id: int):
    await db().execute(
        "DELETE FROM whitelist_users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    await db().commit()


async def is_user_whitelisted(guild_id: int, user_id: int) -> bool:
    cur = await db().execute(
        "SELECT 1 FROM whitelist_users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    return await cur.fetchone() is not None


async def list_whitelist_users(guild_id: int) -> list[int]:
    cur = await db().execute("SELECT user_id FROM whitelist_users WHERE guild_id = ?", (guild_id,))
    return [r["user_id"] for r in await cur.fetchall()]


async def add_whitelist_role(guild_id: int, role_id: int, added_by: int):
    await db().execute(
        "INSERT OR REPLACE INTO whitelist_roles (guild_id, role_id, added_by, added_at) VALUES (?, ?, ?, ?)",
        (guild_id, role_id, added_by, int(time.time())),
    )
    await db().commit()


async def remove_whitelist_role(guild_id: int, role_id: int):
    await db().execute(
        "DELETE FROM whitelist_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
    )
    await db().commit()


async def is_role_whitelisted(guild_id: int, role_id: int) -> bool:
    cur = await db().execute(
        "SELECT 1 FROM whitelist_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
    )
    return await cur.fetchone() is not None


async def list_whitelist_roles(guild_id: int) -> list[int]:
    cur = await db().execute("SELECT role_id FROM whitelist_roles WHERE guild_id = ?", (guild_id,))
    return [r["role_id"] for r in await cur.fetchall()]


# ─── incident_log ────────────────────────────────────────────────

async def log_incident(guild_id: int, user_id: int, action_type: str, details: str, punishment: str):
    await db().execute(
        "INSERT INTO incident_log (guild_id, user_id, action_type, details, punishment, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, action_type, details, punishment, int(time.time())),
    )
    await db().commit()
    await bump_stat(guild_id, action_type)


async def get_recent_incidents(guild_id: int, limit: int = 20) -> list[dict]:
    cur = await db().execute(
        "SELECT * FROM incident_log WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
        (guild_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


# ─── stats ───────────────────────────────────────────────────────

async def bump_stat(guild_id: int, metric: str, by: int = 1):
    await db().execute(
        "INSERT INTO stats (guild_id, metric, count) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, metric) DO UPDATE SET count = count + ?",
        (guild_id, metric, by, by),
    )
    await db().commit()


async def get_stats(guild_id: int) -> dict[str, int]:
    cur = await db().execute("SELECT metric, count FROM stats WHERE guild_id = ?", (guild_id,))
    return {r["metric"]: r["count"] for r in await cur.fetchall()}


# ─── backups ─────────────────────────────────────────────────────

async def save_backup(guild_id: int, created_by: int, data: dict) -> int:
    cur = await db().execute(
        "INSERT INTO backups (guild_id, created_by, created_at, data_json) VALUES (?, ?, ?, ?)",
        (guild_id, created_by, int(time.time()), json.dumps(data)),
    )
    await db().commit()
    return cur.lastrowid


async def list_backups(guild_id: int, limit: int = 10) -> list[dict]:
    cur = await db().execute(
        "SELECT id, created_by, created_at FROM backups WHERE guild_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (guild_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_backup(backup_id: int, guild_id: int) -> dict | None:
    cur = await db().execute(
        "SELECT * FROM backups WHERE id = ? AND guild_id = ?", (backup_id, guild_id)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    result["data"] = json.loads(result["data_json"])
    return result


# ─── warnings ────────────────────────────────────────────────────

async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    cur = await db().execute(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, moderator_id, reason, int(time.time())),
    )
    await db().commit()
    return cur.lastrowid


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    cur = await db().execute(
        "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
        (guild_id, user_id),
    )
    return [dict(r) for r in await cur.fetchall()]


async def remove_warning(warning_id: int, guild_id: int) -> bool:
    cur = await db().execute(
        "DELETE FROM warnings WHERE id = ? AND guild_id = ?", (warning_id, guild_id)
    )
    await db().commit()
    return cur.rowcount > 0


async def clear_warnings(guild_id: int, user_id: int) -> int:
    cur = await db().execute(
        "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    await db().commit()
    return cur.rowcount


# ─── economy (для мини-игр) ───────────────────────────────────────

async def _ensure_account(guild_id: int, user_id: int):
    await db().execute(
        "INSERT OR IGNORE INTO economy (guild_id, user_id, balance, last_daily) VALUES (?, ?, 0, 0)",
        (guild_id, user_id),
    )


async def get_balance(guild_id: int, user_id: int) -> int:
    await _ensure_account(guild_id, user_id)
    await db().commit()
    cur = await db().execute(
        "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    row = await cur.fetchone()
    return row["balance"] if row else 0


async def change_balance(guild_id: int, user_id: int, delta: int) -> int:
    """Изменяет баланс на delta (может быть отрицательным). Возвращает новый баланс. Не уходит ниже нуля."""
    await _ensure_account(guild_id, user_id)
    await db().execute(
        "UPDATE economy SET balance = MAX(0, balance + ?) WHERE guild_id = ? AND user_id = ?",
        (delta, guild_id, user_id),
    )
    await db().commit()
    return await get_balance(guild_id, user_id)


async def get_last_daily(guild_id: int, user_id: int) -> int:
    await _ensure_account(guild_id, user_id)
    await db().commit()
    cur = await db().execute(
        "SELECT last_daily FROM economy WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    row = await cur.fetchone()
    return row["last_daily"] if row else 0


async def set_last_daily(guild_id: int, user_id: int, ts: int):
    await _ensure_account(guild_id, user_id)
    await db().execute(
        "UPDATE economy SET last_daily = ? WHERE guild_id = ? AND user_id = ?",
        (ts, guild_id, user_id),
    )
    await db().commit()


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    cur = await db().execute(
        "SELECT user_id, balance FROM economy WHERE guild_id = ? ORDER BY balance DESC LIMIT ?",
        (guild_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]
