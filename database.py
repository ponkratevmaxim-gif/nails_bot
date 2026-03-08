import os
from datetime import datetime

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL")
pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL is not set")
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool


# =========================
# CREATE TABLES
# =========================

async def init_db() -> None:
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS time_slots (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                is_available INTEGER NOT NULL DEFAULT 1,
                UNIQUE(date, time)
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(date, time)
            )
            """
        )


# =========================
# WORK WITH SLOTS
# =========================

async def add_working_day(date: str, default_times: list[str]) -> None:
    """
    Добавить рабочий день с набором стандартных слотов.
    Слоты создаются только если их ещё нет.
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        for t in default_times:
            await conn.execute(
                """
                INSERT INTO time_slots (date, time, is_available)
                VALUES ($1, $2, 1)
                ON CONFLICT (date, time) DO NOTHING
                """,
                date,
                t,
            )


async def add_time_slot(date: str, time: str) -> None:
    """Добавить один свободный слот (если ещё не существует)."""
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO time_slots (date, time, is_available)
            VALUES ($1, $2, 1)
            ON CONFLICT (date, time) DO NOTHING
            """,
            date,
            time,
        )


async def delete_time_slot(date: str, time: str) -> None:
    """Удалить слот полностью (включая уже недоступный)."""
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM time_slots WHERE date = $1 AND time = $2",
            date,
            time,
        )


async def close_day(date: str) -> None:
    """Полностью закрыть день (удалить все слоты этого дня)."""
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM time_slots WHERE date = $1",
            date,
        )


async def get_available_slots_by_date(date: str) -> list[tuple[int, str]]:
    """
    Получить список доступных слотов на дату.
    Возвращает список кортежей (slot_id, time).
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, time FROM time_slots
            WHERE date = $1 AND is_available = 1
            ORDER BY time
            """,
            date,
        )
    return [(row["id"], row["time"]) for row in rows]


async def mark_slot_unavailable(slot_id: int) -> None:
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(
            "UPDATE time_slots SET is_available = 0 WHERE id = $1",
            slot_id,
        )


async def mark_slot_available(date: str, time: str) -> None:
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE time_slots
            SET is_available = 1
            WHERE date = $1 AND time = $2
            """,
            date,
            time,
        )


async def get_dates_with_slots(start_date: str, end_date: str) -> list[str]:
    """
    Получить даты в интервале [start_date, end_date], где есть доступные слоты.
    Даты в формате YYYY-MM-DD.
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT date FROM time_slots
            WHERE date BETWEEN $1 AND $2 AND is_available = 1
            ORDER BY date
            """,
            start_date,
            end_date,
        )
    return [row["date"] for row in rows]


# =========================
# WORK WITH BOOKINGS
# =========================

async def create_booking(
    user_id: int,
    name: str,
    phone: str,
    date: str,
    time: str,
) -> int:
    """
    Создать запись для пользователя.
    Бросит исключение, если слот уже занят или у пользователя есть запись.
    Возвращает id новой записи.
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        async with conn.transaction():
            created_at = datetime.utcnow().isoformat()
            booking_id = await conn.fetchval(
                """
                INSERT INTO bookings (user_id, name, phone, date, time, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                user_id,
                name,
                phone,
                date,
                time,
                created_at,
            )

            await conn.execute(
                """
                UPDATE time_slots
                SET is_available = 0
                WHERE date = $1 AND time = $2
                """,
                date,
                time,
            )

            return int(booking_id)


async def get_user_booking(user_id: int):
    """
    Получить активную запись пользователя.
    Возвращает (id, date, time, name, phone) либо None.
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, date, time, name, phone
            FROM bookings
            WHERE user_id = $1
            """,
            user_id,
        )
    if row is None:
        return None
    return row["id"], row["date"], row["time"], row["name"], row["phone"]


async def cancel_booking(user_id: int) -> None:
    """
    Отменить запись пользователя и освободить слот.
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT date, time FROM bookings WHERE user_id = $1",
                user_id,
            )

            if row is None:
                return

            date = row["date"]
            time = row["time"]

            await conn.execute(
                "DELETE FROM bookings WHERE user_id = $1",
                user_id,
            )

            await conn.execute(
                """
                UPDATE time_slots
                SET is_available = 1
                WHERE date = $1 AND time = $2
                """,
                date,
                time,
            )


async def get_future_bookings():
    """
    Получить все будущие записи (для восстановления задач напоминаний).
    Возвращает список кортежей (id, user_id, date, time, created_at).
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, date, time, created_at
            FROM bookings
            """
        )
    return [
        (row["id"], row["user_id"], row["date"], row["time"], row["created_at"])
        for row in rows
    ]


async def get_day_schedule(date: str):
    """
    Расписание дня для админа:
    возвращает (time, is_available, name, phone, user_id).
    """
    conn_pool = await _get_pool()
    async with conn_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                ts.time,
                ts.is_available,
                b.name,
                b.phone,
                b.user_id
            FROM time_slots ts
            LEFT JOIN bookings b
                ON ts.date = b.date AND ts.time = b.time
            WHERE ts.date = $1
            ORDER BY ts.time
            """,
            date,
        )
    return [
        (row["time"], row["is_available"], row["name"], row["phone"], row["user_id"])
        for row in rows
    ]
