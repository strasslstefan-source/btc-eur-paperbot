
import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ["DATABASE_URL"]


def connect():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10
    )


def initialize():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY,
                cash DOUBLE PRECISION NOT NULL,
                entries INTEGER NOT NULL DEFAULT 0,
                position JSONB,
                last_exit TIMESTAMPTZ,
                test_start TIMESTAMPTZ,
                test_end TIMESTAMPTZ,
                halted BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id BIGSERIAL PRIMARY KEY,
                time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                event TEXT NOT NULL,
                details JSONB
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS equity (
                id BIGSERIAL PRIMARY KEY,
                time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                cash DOUBLE PRECISION,
                equity DOUBLE PRECISION,
                pnl DOUBLE PRECISION,
                signal TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id BIGSERIAL PRIMARY KEY,
                side TEXT,
                entry_time TIMESTAMPTZ,
                exit_time TIMESTAMPTZ,
                entry_price DOUBLE PRECISION,
                exit_price DOUBLE PRECISION,
                quantity DOUBLE PRECISION,
                pnl DOUBLE PRECISION,
                costs DOUBLE PRECISION,
                reason TEXT
            )
        """)
