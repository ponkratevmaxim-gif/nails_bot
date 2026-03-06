import aiosqlite
from datetime import datetime

DB_NAME = "bookings.db"


# =========================
# 🚀 СОЗДАНИЕ ТАБЛИЦ
# =========================

async def init_db() -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица слотов (рабочие дни и времена)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS time_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                is_available INTEGER NOT NULL DEFAULT 1,
                UNIQUE(date, time)
            )
            """
        )

        # Таблица записей
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(date, time)
            )
            """
        )

        await db.commit()


# =========================
# 💾 РАБОТА СО СЛОТАМИ
# =========================

async def add_working_day(date: str, default_times: list[str]) -> None:
    """
    Добавить рабочий день с набором стандартных слотов.
    Слоты создаются только если их ещё нет.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        for t in default_times:
            await db.execute(
                """
                INSERT OR IGNORE INTO time_slots (date, time, is_available)
                VALUES (?, ?, 1)
                """,
                (date, t),
            )
        await db.commit()


async def add_time_slot(date: str, time: str) -> None:
    """Добавить один свободный слот (если ещё не существует)."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO time_slots (date, time, is_available)
            VALUES (?, ?, 1)
            """,
            (date, time),
        )
        await db.commit()


async def delete_time_slot(date: str, time: str) -> None:
    """Удалить слот полностью (включая уже недоступный)."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM time_slots WHERE date = ? AND time = ?",
            (date, time),
        )
        await db.commit()


async def close_day(date: str) -> None:
    """Полностью закрыть день (удалить все слоты этого дня)."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM time_slots WHERE date = ?",
            (date,),
        )
        await db.commit()


async def get_available_slots_by_date(date: str) -> list[tuple[int, str]]:
    """
    Получить список доступных слотов на дату.
    Возвращает список кортежей (slot_id, time).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id, time FROM time_slots
            WHERE date = ? AND is_available = 1
            ORDER BY time
            """,
            (date,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [(row[0], row[1]) for row in rows]


async def mark_slot_unavailable(slot_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE time_slots SET is_available = 0 WHERE id = ?",
            (slot_id,),
        )
        await db.commit()


async def mark_slot_available(date: str, time: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE time_slots
            SET is_available = 1
            WHERE date = ? AND time = ?
            """,
            (date, time),
        )
        await db.commit()


async def get_dates_with_slots(start_date: str, end_date: str) -> list[str]:
    """
    Получить даты в интервале [start_date, end_date], где есть доступные слоты.
    Даты в формате YYYY-MM-DD.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT DISTINCT date FROM time_slots
            WHERE date BETWEEN ? AND ? AND is_available = 1
            ORDER BY date
            """,
            (start_date, end_date),
        ) as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]


# =========================
# 💾 РАБОТА С ЗАПИСЯМИ
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
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN")
        try:
            created_at = datetime.utcnow().isoformat()
            cursor = await db.execute(
                """
                INSERT INTO bookings (user_id, name, phone, date, time, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, name, phone, date, time, created_at),
            )
            booking_id = cursor.lastrowid

            await db.execute(
                """
                UPDATE time_slots
                SET is_available = 0
                WHERE date = ? AND time = ?
                """,
                (date, time),
            )

            await db.commit()
            return booking_id
        except Exception:
            await db.rollback()
            raise


async def get_user_booking(user_id: int):
    """
    Получить активную запись пользователя.
    Возвращает (id, date, time, name, phone) либо None.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id, date, time, name, phone
            FROM bookings
            WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()


async def cancel_booking(user_id: int) -> None:
    """
    Отменить запись пользователя и освободить слот.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN")
        try:
            async with db.execute(
                "SELECT date, time FROM bookings WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                await db.rollback()
                return

            date, time = row

            await db.execute(
                "DELETE FROM bookings WHERE user_id = ?",
                (user_id,),
            )

            await db.execute(
                """
                UPDATE time_slots
                SET is_available = 1
                WHERE date = ? AND time = ?
                """,
                (date, time),
            )

            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_future_bookings():
    """
    Получить все будущие записи (для восстановления задач напоминаний).
    Возвращает список кортежей (id, user_id, date, time, created_at).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id, user_id, date, time, created_at
            FROM bookings
            """
        ) as cursor:
            rows = await cursor.fetchall()
    return rows


async def get_day_schedule(date: str):
    """
    Расписание дня для админа:
    возвращает (time, is_available, name, phone, user_id).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
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
            WHERE ts.date = ?
            ORDER BY ts.time
            """,
            (date,),
        ) as cursor:
            return await cursor.fetchall()