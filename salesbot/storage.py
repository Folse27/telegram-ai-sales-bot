"""Postgres access (asyncpg): schema, the connection pool and the queries shared by several modules."""
from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

import asyncpg

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clients (
    chat_id          bigint PRIMARY KEY,
    user_id          bigint,
    username         text,
    full_name        text,
    mode             text NOT NULL DEFAULT 'BOT' CHECK (mode IN ('BOT', 'HUMAN')),
    mode_changed_at  timestamptz NOT NULL DEFAULT now(),
    last_manager_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seen_updates (
    update_id  bigint PRIMARY KEY,
    seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id             bigserial PRIMARY KEY,
    chat_id        bigint NOT NULL REFERENCES clients (chat_id),
    role           text NOT NULL CHECK (role IN ('user', 'assistant', 'manager')),
    text           text NOT NULL,
    tg_message_id  bigint,
    answered       boolean,  -- user rows only: false until a reply batch covering it was delivered
    created_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS meta jsonb;  -- assistant rows: {"products": [...]} for retrieval
CREATE INDEX IF NOT EXISTS messages_chat_idx ON messages (chat_id, id);
CREATE INDEX IF NOT EXISTS messages_unanswered_idx ON messages (created_at) WHERE role = 'user' AND NOT answered;
CREATE TABLE IF NOT EXISTS orders (
    id               bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL REFERENCES clients (chat_id),
    status           text NOT NULL,
    data             jsonb NOT NULL DEFAULT '{}',
    payment_sent_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_one_editable ON orders (chat_id)
    WHERE status IN ('DRAFT', 'AWAITING_CONFIRMATION');
CREATE TABLE IF NOT EXISTS handoffs (
    id               bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL REFERENCES clients (chat_id),
    reason           text NOT NULL,
    department       text,
    summary          text,
    card_message_id  bigint,
    status           text NOT NULL DEFAULT 'open',
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz
);
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS taken_by text;
CREATE TABLE IF NOT EXISTS relay_map (
    admin_message_id  bigint PRIMARY KEY,
    chat_id           bigint NOT NULL,
    handoff_id        bigint REFERENCES handoffs (id),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  bigserial PRIMARY KEY,
    chat_id             bigint,
    model               text,
    attempt             int,
    input_tokens        int,
    output_tokens       int,
    cache_read_tokens   int,
    cache_write_tokens  int,
    latency_ms          int,
    status              text,
    validation          jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_created_idx ON llm_calls (created_at);
CREATE TABLE IF NOT EXISTS shop_files (     -- the shop data when SHOP_SOURCE=db (catalog, texts, examples)
    name        text PRIMARY KEY,
    body        text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photo_cache (    -- Telegram file_id per image source, filled on first send
    src         text PRIMARY KEY,
    file_id     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""

# Tables a tester's /clear wipes for one chat. Fixed names, never user input (they are formatted into SQL).
CHAT_TABLES = ("relay_map", "handoffs", "orders", "messages")


async def create_pool(dsn: str, schema: str | None = None) -> asyncpg.Pool:
    async def init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    async def use_schema(conn: asyncpg.Connection) -> None:
        # Neon's proxy drops search_path as a startup parameter, and the pool runs RESET ALL on release,
        # so the test schema is set on every acquire.
        await conn.execute(f'SET search_path TO "{schema}"')

    # No idle connections are kept (min_size=0, closed after 60 s idle) so a serverless database can scale to zero.
    return await asyncpg.create_pool(
        dsn, min_size=0, max_size=5, max_inactive_connection_lifetime=60, command_timeout=15, init=init,
        setup=use_schema if schema else None,
    )


class Storage:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._seen: OrderedDict[int, None] = OrderedDict()  # in-memory LRU in front of seen_updates

    @classmethod
    async def connect(cls, dsn: str, schema: str | None = None) -> Storage:
        """Open the pool and create tables if missing. `schema` isolates tests (one throwaway schema per case)."""
        if schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            finally:
                await conn.close()
        st = cls(await create_pool(dsn, schema))
        await st.pool.execute(SCHEMA_SQL)
        return st

    async def close(self) -> None:
        await self.pool.close()

    async def version(self) -> str:
        return str(await self.pool.fetchval("SELECT version()")).split(",")[0]

    async def cleanup(self) -> None:
        await self.pool.execute("DELETE FROM seen_updates WHERE seen_at < now() - interval '3 days'")

    async def first_seen(self, update_id: int) -> bool:
        """True the first time an update_id arrives; Telegram retries and restarts can resend."""
        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        if len(self._seen) > 2000:
            self._seen.popitem(last=False)
        row = await self.pool.fetchval(
            "INSERT INTO seen_updates (update_id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING update_id", update_id
        )
        return row is not None

    async def upsert_client(self, chat_id: int, user_id: int | None, username: str | None, full_name: str | None) -> None:
        await self.pool.execute(
            """INSERT INTO clients (chat_id, user_id, username, full_name) VALUES ($1, $2, $3, $4)
               ON CONFLICT (chat_id) DO UPDATE
               SET user_id = $2, username = $3, full_name = $4, last_seen_at = now()""",
            chat_id, user_id, username, full_name,
        )

    async def get_mode(self, chat_id: int) -> str:
        return await self.pool.fetchval("SELECT mode FROM clients WHERE chat_id = $1", chat_id) or "BOT"

    async def set_mode(self, chat_id: int, mode: str) -> None:
        await self.pool.execute(
            "UPDATE clients SET mode = $2, mode_changed_at = now() WHERE chat_id = $1", chat_id, mode
        )

    async def get_client(self, chat_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM clients WHERE chat_id = $1", chat_id)

    async def save_message(self, chat_id: int, role: str, text: str, tg_message_id: int | None = None,
                           answered: bool | None = None, meta: dict | None = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO messages (chat_id, role, text, tg_message_id, answered, meta) VALUES ($1, $2, $3, $4, $5, $6)"
            " RETURNING id",
            chat_id, role, text, tg_message_id, answered, meta,
        )

    async def wipe_chat(self, chat_id: int) -> None:
        """/clear for testers: forget this chat's history, orders and handoffs (the AI cost log is kept)."""
        async with self.pool.acquire() as conn, conn.transaction():
            for table in CHAT_TABLES:
                await conn.execute(f"DELETE FROM {table} WHERE chat_id = $1", chat_id)
            await conn.execute("UPDATE clients SET mode = 'BOT', mode_changed_at = now(), last_manager_at = NULL"
                               " WHERE chat_id = $1", chat_id)

    async def mark_answered(self, message_ids: list[int]) -> None:
        if message_ids:
            await self.pool.execute("UPDATE messages SET answered = true WHERE id = ANY($1::bigint[])", message_ids)

    async def history(self, chat_id: int, limit: int = 20, exclude_ids: list[int] | None = None) -> list[asyncpg.Record]:
        rows = await self.pool.fetch(
            "SELECT id, role, text, meta, created_at FROM messages WHERE chat_id = $1 AND NOT (id = ANY($3::bigint[]))"
            " ORDER BY id DESC LIMIT $2",
            chat_id, limit, exclude_ids or [],
        )
        return rows[::-1]

    async def unanswered_recent(self, seconds: int = 120) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, chat_id, text, tg_message_id FROM messages WHERE role = 'user' AND NOT answered"
            " AND created_at > now() - make_interval(secs => $1) ORDER BY id", seconds,
        )

    async def log_llm_call(self, chat_id: int, model: str, attempt: int, usage: Any, latency_ms: int,
                           status: str, validation: dict | None) -> None:
        await self.pool.execute(
            "INSERT INTO llm_calls (chat_id, model, attempt, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, latency_ms, status, validation) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            chat_id, model, attempt, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
            getattr(usage, "cache_read_input_tokens", None), getattr(usage, "cache_creation_input_tokens", None),
            latency_ms, status, validation,
        )

    async def llm_calls_since(self, hours: float) -> list[asyncpg.Record]:
        """AI calls of the last `hours` (0 = all time): /stats and the daily budget."""
        return await self.pool.fetch(
            "SELECT * FROM llm_calls WHERE $1 = 0 OR created_at > now() - make_interval(secs => $1 * 3600)",
            float(hours))

    async def replies_in_24h(self, chat_id: int) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM llm_calls WHERE chat_id = $1 AND status = 'ok'"
            " AND created_at > now() - interval '24 hours'", chat_id)
