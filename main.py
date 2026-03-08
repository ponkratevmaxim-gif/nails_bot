import asyncio
import calendar
import logging
import os
import re
from datetime import datetime, timedelta
from aiosqlite import connect as sqlite_connect
from pathlib import Path
from secrets import token_urlsafe

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from booking_calendar import generate_time_slots_keyboard
from database import (
    add_time_slot,
    add_working_day,
    cancel_booking,
    close_day,
    get_available_slots_by_date,
    get_dates_with_slots,
    get_day_schedule,
    get_future_bookings,
    get_user_booking,
    init_db,
    create_booking,
    delete_time_slot,
)


logging.basicConfig(level=logging.INFO)


# =========================
# 🔐 НАСТРОЙКИ
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_STR = os.getenv("ADMIN_ID")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/nasstxm_nails")
ENABLE_CHANNEL_NOTIFICATIONS = os.getenv("ENABLE_CHANNEL_NOTIFICATIONS", "0")
CONTACT_TELEGRAM_URL = os.getenv("CONTACT_TELEGRAM_URL", "https://t.me/nasstxm")
CONTACT_PHONE = os.getenv("CONTACT_PHONE", "+7 967 789 35 59")
ADMIN_CONTACT_URL = os.getenv("ADMIN_CONTACT_URL", "https://t.me/mspops")
STUDENTS_WORKS_DIR = os.getenv("STUDENTS_WORKS_DIR", "students_works")
PAYMENT_PHONE = os.getenv("PAYMENT_PHONE")
PAYMENT_NAME = os.getenv("PAYMENT_NAME")
PREPAYMENT_AMOUNT = os.getenv("PREPAYMENT_AMOUNT", "500")
PRICE_PHOTO = os.getenv("PRICE_PHOTO")
SALON_ADDRESS = os.getenv("SALON_ADDRESS")
SALON_LATITUDE_STR = os.getenv("SALON_LATITUDE")
SALON_LONGITUDE_STR = os.getenv("SALON_LONGITUDE")
CHANNEL_NOTIFICATIONS_ENABLED = ENABLE_CHANNEL_NOTIFICATIONS.lower() in {
    "1",
    "true",
    "yes",
    "on",
}

if not BOT_TOKEN or not ADMIN_ID_STR:
    raise ValueError(
        "Проверь .env — должны быть заданы BOT_TOKEN, ADMIN_ID"
    )
if not PAYMENT_PHONE or not PAYMENT_NAME:
    raise ValueError(
        "Проверь .env — должны быть заданы PAYMENT_PHONE, PAYMENT_NAME"
    )
if CHANNEL_NOTIFICATIONS_ENABLED and not CHANNEL_ID_STR:
    raise ValueError(
        "Проверь .env — при ENABLE_CHANNEL_NOTIFICATIONS=1 нужно задать CHANNEL_ID"
    )

ADMIN_ID = int(ADMIN_ID_STR)
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR else None
try:
    SALON_LATITUDE = float(SALON_LATITUDE_STR) if SALON_LATITUDE_STR else None
    SALON_LONGITUDE = float(SALON_LONGITUDE_STR) if SALON_LONGITUDE_STR else None
except ValueError:
    SALON_LATITUDE = None
    SALON_LONGITUDE = None

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()
scheduler = AsyncIOScheduler()
pending_payments: dict[str, dict] = {}
DB_NAME = "bookings.db"


# =========================
# FSM ДЛЯ ЗАПИСИ
# =========================


class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()



class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_broadcast_confirm = State()
    waiting_admin_date = State()
    waiting_admin_time = State()
    waiting_cancel_reason = State()

# =========================
# 📱 ГЛАВНОЕ МЕНЮ (Reply)
# =========================


def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="💅 Прайс"), KeyboardButton(text="📷 Работы")],
        [KeyboardButton(text="📞 Связаться"), KeyboardButton(text="✨ Обучение")],
        [KeyboardButton(text="❌ Отменить запись")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text="Админ-панель🔒")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def admin_panel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Даты"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="💰 Ожидают подтверждения"), KeyboardButton(text="📊 Сколько записей")],
            [KeyboardButton(text="📋 Список окон"), KeyboardButton(text="🗑 Удалить все окна")],
            [KeyboardButton(text="⬅️ В главное меню")],
        ],
        resize_keyboard=True,
    )


def admin_dates_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить время", callback_data="admin_date_add")],
            [InlineKeyboardButton(text="➖ Удалить время", callback_data="admin_date_delete")],
            [InlineKeyboardButton(text="🗑 Удалить все окна дня", callback_data="admin_day_clear")],
        ]
    )



def admin_confirm_inline(confirm_data: str, cancel_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=confirm_data),
                InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data),
            ]
        ]
    )


def admin_broadcast_confirm_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data="admin_broadcast_confirm_send"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_confirm_cancel"),
            ]
        ]
    )

def education_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Как проходит обучение")],
            [KeyboardButton(text="💅 Работы учениц")],
            [KeyboardButton(text="💎 Тарифы обучения")],
            [KeyboardButton(text="⬅️ Вернуться обратно")],
        ],
        resize_keyboard=True,
    )


def booking_back_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Вернуться к записи",
                    callback_data="booking_restart",
                )
            ]
        ]
    )


def pending_payment_inline(payment_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатил(а) ✅", callback_data=f"paid_{payment_token}")],
            [InlineKeyboardButton(text="✉️Скинуть чек", url=ADMIN_CONTACT_URL)],
            [InlineKeyboardButton(text="Отменить заявку ❌", callback_data=f"cancel_pending_{payment_token}")],
            [InlineKeyboardButton(text="Вернуться к записи", callback_data="booking_restart")],
        ]
    )


def education_back_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться обратно", callback_data="edu_back")]
        ]
    )


def find_pending_token_by_user(user_id: int) -> str | None:
    for token, pending in pending_payments.items():
        if pending["user_id"] == user_id and not pending["confirmed"]:
            return token
    return None


def resolve_price_photo_path() -> Path | None:
    if not PRICE_PHOTO:
        return None

    raw = PRICE_PHOTO.strip()
    if not raw:
        return None

    project_root = Path(__file__).resolve().parent
    folder = Path(raw)
    if not folder.is_absolute():
        folder = project_root / folder

    if not folder.exists() or not folder.is_dir():
        return None

    image_paths: list[Path] = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        image_paths.extend(sorted(folder.glob(ext)))

    if image_paths:
        return image_paths[0]
    return None


def _format_month_title(year: int, month: int) -> str:
    months = [
        "",
        "Январь",
        "Февраль",
        "Март",
        "Апрель",
        "Май",
        "Июнь",
        "Июль",
        "Август",
        "Сентябрь",
        "Октябрь",
        "Ноябрь",
        "Декабрь",
    ]
    return f"{months[month]} {year}"


async def _available_dates_set() -> set[str]:
    start = datetime.now().date()
    end = start + timedelta(days=30)
    dates = await get_dates_with_slots(
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )
    return set(dates)


async def render_calendar(
    year: int | None = None,
    month: int | None = None,
    admin_mode: bool = False,
) -> InlineKeyboardMarkup:
    now = datetime.now()
    max_date = now.date() + timedelta(days=30)
    year = year or now.year
    month = month or now.month

    available_dates = set() if admin_mode else await _available_dates_set()

    keyboard: list[list[InlineKeyboardButton]] = []
    keyboard.append(
        [
            InlineKeyboardButton(text="⬅", callback_data=f"prev_{year}_{month}"),
            InlineKeyboardButton(text=_format_month_title(year, month), callback_data="ignore"),
            InlineKeyboardButton(text="➡", callback_data=f"next_{year}_{month}"),
        ]
    )

    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(text=day, callback_data="ignore") for day in days])

    for week in calendar.monthcalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
                continue

            selected_date = datetime(year, month, day).date()
            date_str = selected_date.strftime("%Y-%m-%d")

            if selected_date < now.date() or selected_date > max_date:
                row.append(InlineKeyboardButton(text="❌", callback_data="ignore"))
            elif admin_mode or date_str in available_dates:
                row.append(
                    InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"date_{year}_{month}_{day}",
                    )
                )
            else:
                row.append(InlineKeyboardButton(text="*️⃣", callback_data="ignore"))

        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="⬅ Назад в меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# =========================
# 🔔 НАПОМИНАНИЯ
# =========================


async def send_reminder(user_id: int, time_str: str) -> None:
    try:
        await bot.send_message(
            user_id,
            f"Напоминаем, что вы записаны на наращивание ресниц завтра в {time_str}.\n"
            f"Ждём вас ❤️",
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка при отправке напоминания: %s", e)


async def safe_send_message(chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramBadRequest as e:
        logging.warning("Не удалось отправить сообщение в chat_id=%s: %s", chat_id, e)
        return False
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка при отправке сообщения в chat_id=%s: %s", chat_id, e)
        return False



async def register_user(user_id: int, username: str | None, first_name: str | None) -> None:
    async with sqlite_connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        created_at = datetime.utcnow().isoformat()
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """
            ,(user_id, username, first_name, created_at)
        )
        await db.commit()


async def get_registered_user_ids() -> list[int]:
    async with sqlite_connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_all_bookings_summary():
    async with sqlite_connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT user_id, name, phone, date, time
            FROM bookings
            ORDER BY date, time
            """
        ) as cursor:
            return await cursor.fetchall()

def schedule_reminders_for_booking(
    booking_id: int,
    user_id: int,
    date_str: str,
    time_str: str,
) -> None:
    """
    Планируем напоминания за 24 и 12 часов, если ещё не поздно.
    """
    visit_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    now = datetime.utcnow()

    reminder_24 = visit_dt - timedelta(hours=24)
    reminder_12 = visit_dt - timedelta(hours=12)

    if reminder_24 > now:
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=reminder_24,
            args=[user_id, time_str],
            id=f"booking_{booking_id}_24",
            replace_existing=True,
        )

    if reminder_12 > now:
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=reminder_12,
            args=[user_id, time_str],
            id=f"booking_{booking_id}_12",
            replace_existing=True,
        )


def cancel_reminders_for_booking(booking_id: int) -> None:
    for suffix in ("24", "12"):
        job_id = f"booking_{booking_id}_{suffix}"
        job = scheduler.get_job(job_id)
        if job:
            job.remove()


async def restore_reminders() -> None:
    rows = await get_future_bookings()
    now = datetime.now()

    for booking_id, user_id, date_str, time_str, created_at in rows:
        try:
            visit_dt = datetime.strptime(
                f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            continue

        if visit_dt <= now:
            continue

        # Восстанавливаем задачи, как если бы создавали их сейчас
        schedule_reminders_for_booking(
            booking_id=booking_id,
            user_id=user_id,
            date_str=date_str,
            time_str=time_str,
        )


# =========================
# 🚀 /start
# =========================


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await register_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    await message.answer(
        "Привет 💖\n"
        "Добро пожаловать в студию маникюра!\n\n"
        "Выберите раздел:",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(F.text == "📞 Связаться")
async def contact_handler(message: Message) -> None:
    await message.answer(
        "📞 Связаться с нами:\n\n"
        f"Telegram: {CONTACT_TELEGRAM_URL}\n"
        f"Телефон: {CONTACT_PHONE}",
    )


# =========================
# 🎓 ОБУЧЕНИЕ
# =========================


@dp.message(F.text == "✨ Обучение")
async def education_entry(message: Message) -> None:
    await message.answer(
        "Раздел обучения. Выберите интересующий пункт:",
        reply_markup=education_menu(),
    )


@dp.message(F.text == "⬅️ Вернуться обратно")
async def education_back_to_main(message: Message) -> None:
    await message.answer("Выберите раздел:", reply_markup=main_menu(message.from_user.id))


@dp.callback_query(F.data == "edu_back")
async def education_back_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Раздел обучения. Выберите интересующий пункт:",
        reply_markup=education_menu(),
    )
    await callback.answer()

@dp.message(F.text == "📚 Как проходит обучение")
async def education_how_it_works(message: Message) -> None:
    text = (
        "Как проходит обучение?👩‍🎓\n\n"
        "Я создала условия, в которых ты будешь чувствовать себя максимально комфортно:\n\n"
        "🍕 Вкусные обеды — тебе не нужно думать о перекусе, я всё продумала за тебя.\n\n"
        "✨ Атмосфера — работаем без стресса, в поддержке и вдохновении.\n\n"
        "📚 Теория + Практика — только актуальные знания, никакой воды.\n\n"
        "🎁 Подарки — каждая ученица получает набор фрез, которыми мы работаем на курсе.\n\n"
        "📃 Сертификат — по окончании ты получаешь сертификат, подтверждающий твои знания."
    )
    await message.answer(text, reply_markup=education_back_inline())

@dp.message(F.text == "💅 Работы учениц")
async def education_students_works(message: Message) -> None:
    text = (
        "Первые работы моих учениц!\n\n"
        "Здесь ты можешь увидеть, какие результаты показывают девочки уже на курсе.\n"
        "Мы отрабатываем и форму «квадрат», «миндаль».\n"
        "Листай фото ниже и убедись, что научиться делать чисто и красиво может каждая!"
    )
    await message.answer(text, reply_markup=education_back_inline())

    works_dir = Path(STUDENTS_WORKS_DIR)
    image_paths = []
    if works_dir.exists() and works_dir.is_dir():
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            image_paths.extend(sorted(works_dir.glob(ext)))

    if image_paths:
        for image_path in image_paths:
            await message.answer_photo(photo=FSInputFile(str(image_path)))
    else:
        await message.answer(
            "Пока фото не добавлены. Загрузите изображения в папку "
            f"<code>{works_dir}</code>, и они будут отправляться автоматически.",
            reply_markup=education_back_inline(),
        )


@dp.message(F.text == "💎 Тарифы обучения")
async def education_tariff(message: Message) -> None:
    text = (
        "Выбирай свой тариф обучения:\n\n"
        "1. Тариф BASE\n"
        "• 2 дня (онлайн теория + практика)\n"
        "• 2 модели (квадрат и миндаль)\n"
        "• Вкусный обед и вся база маникюра\n"
        "• Бонус: сертификат и набор фрез в подарок!\n\n"
        "2. Тариф STANDARD\n"
        "• 3 дня обучения\n"
        "• 2 модели на курсе\n"
        "• Фишка: +1 отработка через месяц для закрепления знаний!\n"
        "• Вкусный обед и вся база\n"
        "• Бонусы: сертификат, набор фрез + Чек-лист по привлечению клиентов!\n\n"
        "3. Тариф «МАСТЕР ПОД КЛЮЧ»\n"
        "• 4 дня интенсивного обучения\n"
        "• 3 модели на курсе\n"
        "• Фишка: +2 отработки через месяц\n"
        "• ЛИЧНОЕ ВЕДЕНИЕ (1 месяц): раскручиваем твои соцсети и набираем клиентскую базу вместе!\n"
        "• Качественная база, после которой не нужно идти на повышение.\n"
        "• Бонусы: сертификат, набор фрез + ДОСТУП В ГРУППУ С МАТЕРИАЛАМИ.\n\n"
        "В МАРТЕ действует специальная скидка на любой тариф!\n"
        "Чтобы узнать цену со скидкой и забронировать место — жми кнопку «Задать вопрос»."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Задать вопрос", url=ADMIN_CONTACT_URL)],
            [InlineKeyboardButton(text="⬅️ Вернуться обратно", callback_data="edu_back")],
        ]
    )
    await message.answer(text, reply_markup=kb)

# =========================
# 💅 ПРАЙС
# =========================


@dp.message(F.text == "💅 Прайс")
async def price_handler(message: Message) -> None:
    await message.answer("💅 Прайс:")

    if not PRICE_PHOTO:
        await message.answer(
            "Фото прайса не настроено. Добавьте переменную PRICE_PHOTO в .env.",
        )
        return

    local_price_photo = resolve_price_photo_path()
    try:
        if local_price_photo is not None:
            await message.answer_photo(photo=FSInputFile(str(local_price_photo)))
        else:
            await message.answer(
                "В папке из PRICE_PHOTO не найдено изображений (.jpg/.jpeg/.png/.webp).",
            )
            return
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка при отправке прайса: %s", e)
        await message.answer(
            "Не удалось отправить фото прайса. Проверьте папку PRICE_PHOTO и доступ к файлам.",
        )


# =========================
# 📷 РАБОТЫ
# =========================


@dp.message(F.text == "📷 Работы")
async def works_handler(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👉🏻 Перейти в канал",
                    url=CHANNEL_LINK,
                )
            ]
        ]
    )

    await message.answer(
        "Работы можно посмотреть тут 👇🏻",
        reply_markup=keyboard,
    )


# =========================
# 🔒 АДМИН-ПАНЕЛЬ
# =========================


@dp.message(F.text == "Админ-панель🔒")
async def admin_panel_open(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Админ-панель:", reply_markup=admin_panel_menu())


@dp.message(F.text == "⬅️ В главное меню")
async def admin_panel_back(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Выберите раздел:", reply_markup=main_menu(message.from_user.id))


@dp.message(F.text == "📅 Даты")
async def admin_dates_entry(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "Управление датами. Нажмите кнопку ниже:",
        reply_markup=admin_dates_menu(),
    )


@dp.callback_query(F.data == "admin_date_add")
async def admin_pick_date_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_admin_date)
    await state.update_data(admin_action="add")
    await callback.message.answer(
        "Выберите дату:",
        reply_markup=await render_calendar(admin_mode=True),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_date_delete")
async def admin_pick_date_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_admin_date)
    await state.update_data(admin_action="delete")
    await callback.message.answer(
        "Выберите дату:",
        reply_markup=await render_calendar(admin_mode=True),
    )
    await callback.answer()



@dp.callback_query(F.data == "admin_day_clear")
async def admin_pick_date_clear_day(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_admin_date)
    await state.update_data(admin_action="clear_day")
    await callback.message.answer(
        "Выберите дату для удаления всех свободных окон:",
        reply_markup=await render_calendar(admin_mode=True),
    )
    await callback.answer()

@dp.callback_query(StateFilter(AdminStates.waiting_admin_date), F.data.startswith("date_"))
async def admin_select_date(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    _, year, month, day = callback.data.split("_")
    date_obj = datetime(int(year), int(month), int(day)).date()
    date_str = date_obj.strftime("%Y-%m-%d")
    data = await state.get_data()
    action = data.get("admin_action", "add")

    if action == "clear_day":
        await close_day(date_str)
        await state.clear()
        await callback.message.answer(
            f"🗑 Все свободные окна на <b>{date_obj.strftime('%d.%m.%Y')}</b> удалены.",
            reply_markup=admin_panel_menu(),
        )
        await callback.answer()
        return

    await state.update_data(admin_date=date_str)
    await state.set_state(AdminStates.waiting_admin_time)
    data = await state.get_data()
    action = data.get("admin_action", "add")
    action_text = "добавления" if action == "add" else "удаления"
    await callback.message.answer(
        f"Дата выбрана: <b>{date_obj.strftime('%d.%m.%Y')}</b>\n"
        f"Введите время слота для {action_text} в формате <b>HH:MM</b> (например, 13:30):",
    )
    await callback.answer()


@dp.message(AdminStates.waiting_admin_time)
async def admin_set_time(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    time_str = message.text.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_str):
        await message.answer("Некорректный формат времени. Пример: 13:30")
        return

    data = await state.get_data()
    date_str = data.get("admin_date")
    action = data.get("admin_action", "add")
    if not date_str:
        await state.clear()
        await message.answer("Дата не найдена. Повторите выбор.", reply_markup=admin_panel_menu())
        return

    if action == "delete":
        await delete_time_slot(date_str, time_str)
        await message.answer(
            f"🗑 Удален слот: <b>{date_str} {time_str}</b>",
            reply_markup=admin_panel_menu(),
        )
    else:
        await add_time_slot(date_str, time_str)
        await message.answer(
            f"✅ Добавлен свободный слот: <b>{date_str} {time_str}</b>",
            reply_markup=admin_panel_menu(),
        )
    await state.clear()


@dp.message(F.text == "📢 Рассылка")
async def admin_broadcast_entry(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await message.answer("Введите текст рассылки:")


@dp.message(AdminStates.waiting_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    text = message.html_text or message.text
    if not text:
        await message.answer("Введите текстовое сообщение для рассылки.")
        return

    await state.update_data(broadcast_text=text)
    await state.set_state(AdminStates.waiting_broadcast_confirm)
    await message.answer(
        f"Предпросмотр рассылки:\n\n{text}\n\nОтправить это сообщение всем пользователям?",
        reply_markup=admin_broadcast_confirm_inline(),
    )


@dp.callback_query(F.data == "admin_broadcast_confirm_send")
async def admin_broadcast_confirm_send(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await state.clear()
        await callback.message.answer("Текст рассылки не найден.", reply_markup=admin_panel_menu())
        await callback.answer()
        return

    user_ids = await get_registered_user_ids()
    sent_count = 0
    fail_count = 0
    for user_id in user_ids:
        ok = await safe_send_message(user_id, text)
        if ok:
            sent_count += 1
        else:
            fail_count += 1

    await callback.message.answer(
        f"Рассылка завершена. Отправлено: <b>{sent_count}</b>, ошибок: <b>{fail_count}</b>",
        reply_markup=admin_panel_menu(),
    )
    await state.clear()
    await callback.answer("Рассылка отправлена")


@dp.callback_query(F.data == "admin_broadcast_confirm_cancel")
async def admin_broadcast_confirm_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    await state.clear()
    await callback.message.answer("Рассылка отменена.", reply_markup=admin_panel_menu())
    await callback.answer()


@dp.message(F.text == "🗑 Удалить все окна")
async def admin_delete_all_free_slots_entry(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "Вы уверены, что хотите удалить ВСЕ свободные окна?\n\nЭто действие нельзя отменить.",
        reply_markup=admin_confirm_inline(
            "admin_free_slots_delete_confirm",
            "admin_free_slots_delete_cancel",
        ),
    )


@dp.callback_query(F.data == "admin_free_slots_delete_confirm")
async def admin_delete_all_free_slots_confirm(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    dates = await get_dates_with_slots("0001-01-01", "9999-12-31")
    for date_str in dates:
        slots = await get_available_slots_by_date(date_str)
        for _, time_str in slots:
            await delete_time_slot(date_str, time_str)
    await callback.message.answer("Все свободные окна удалены.", reply_markup=admin_panel_menu())
    await callback.answer()


@dp.callback_query(F.data == "admin_free_slots_delete_cancel")
async def admin_delete_all_free_slots_cancel(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    await callback.message.answer("Удаление отменено.", reply_markup=admin_panel_menu())
    await callback.answer()


@dp.message(F.text == "📋 Список окон")
async def admin_free_slots_list(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    dates = await get_dates_with_slots("0001-01-01", "9999-12-31")
    if not dates:
        await message.answer("Свободных окон нет.", reply_markup=admin_panel_menu())
        return

    lines = ["Свободные окна:\n"]
    for date_str in dates:
        slots = await get_available_slots_by_date(date_str)
        if not slots:
            continue
        date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m")
        times = "; ".join(time_str for _, time_str in slots)
        lines.append(f"{date_fmt} — {times}")

    if len(lines) == 1:
        await message.answer("Свободных окон нет.", reply_markup=admin_panel_menu())
        return

    await message.answer("\n".join(lines), reply_markup=admin_panel_menu())

@dp.message(F.text == "💰 Ожидают подтверждения")
async def admin_pending_count(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    pending_count = sum(1 for p in pending_payments.values() if not p.get("confirmed"))
    await message.answer(
        f"Сейчас ожидают подтверждения оплаты: <b>{pending_count}</b>",
        reply_markup=admin_panel_menu(),
    )


@dp.message(F.text == "📊 Сколько записей")
async def admin_bookings_stats(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    bookings = await get_all_bookings_summary()
    if not bookings:
        await message.answer("Активных записей нет.", reply_markup=admin_panel_menu())
        return

    lines = [f"Всего активных записей: <b>{len(bookings)}</b>\n"]
    keyboard_rows = []
    for user_id, name, phone, date_str, time_str in bookings:
        lines.append(
            f"• <b>{date_str} {time_str}</b> — {name} ({phone}), id:<code>{user_id}</code>"
        )
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"❌ Отменить запись {date_str} {time_str}",
                callback_data=f"admin_cancel_pick_{user_id}",
            )
        ])

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )
    await message.answer("Действия админа:", reply_markup=admin_panel_menu())


@dp.callback_query(F.data.startswith("admin_cancel_pick_"))
async def admin_cancel_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ.", show_alert=True)
        return

    try:
        target_user_id = int(callback.data[len("admin_cancel_pick_"):])
    except ValueError:
        await callback.answer("Некорректный ID.", show_alert=True)
        return

    booking = await get_user_booking(target_user_id)
    if not booking:
        await callback.answer("Запись не найдена или уже отменена.", show_alert=True)
        return

    booking_id, date_str, time_str, name, phone = booking
    await state.update_data(
        cancel_user_id=target_user_id,
        cancel_booking_id=booking_id,
        cancel_date=date_str,
        cancel_time=time_str,
        cancel_name=name,
        cancel_phone=phone,
    )
    await state.set_state(AdminStates.waiting_cancel_reason)
    await callback.message.answer(
        f"Введите причину отмены для записи:\n<b>{date_str} {time_str}</b> — {name}",
    )
    await callback.answer()


@dp.message(AdminStates.waiting_cancel_reason)
async def admin_cancel_reason(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина слишком короткая. Введите подробнее.")
        return

    data = await state.get_data()
    target_user_id = data.get("cancel_user_id")
    booking_id = data.get("cancel_booking_id")
    date_str = data.get("cancel_date")
    time_str = data.get("cancel_time")
    name = data.get("cancel_name")
    phone = data.get("cancel_phone")

    if not target_user_id:
        await state.clear()
        await message.answer("Не удалось найти запись для отмены.", reply_markup=admin_panel_menu())
        return

    await cancel_booking(target_user_id)
    if booking_id:
        cancel_reminders_for_booking(booking_id)

    await message.answer(
        "✅ Запись отменена\n\n"
        f"👤 {name} ({phone})\n"
        f"📅 {date_str}\n"
        f"⏰ {time_str}\n"
        f"📝 Причина: {reason}",
        reply_markup=admin_panel_menu(),
    )

    await safe_send_message(
        target_user_id,
        "❌ Ваша запись была отменена\n\n"
        f"Дата: <b>{date_str}</b>\n"
        f"Время: <b>{time_str}</b>\n"
        f"Причина: {reason}",
    )

    await state.clear()


# =========================
# 📅 ЗАПИСАТЬСЯ (открыть календарь)
# =========================


@dp.message(F.text == "📅 Записаться")
async def open_calendar_message(message: Message) -> None:
    user_id = message.from_user.id
    booking = await get_user_booking(user_id)
    if booking:
        _, date_str, time_str, _, _ = booking
        await message.answer(
            f"У вас уже есть запись:\n\n"
            f"<b>{date_str} в {time_str}</b>\n\n"
            f"Сначала отмените текущую запись, чтобы создать новую.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    for pending in pending_payments.values():
        if pending["user_id"] == user_id and not pending["confirmed"]:
            await message.answer(
                "У вас уже есть заявка на бронь времени. Дождитесь подтверждения оплаты админом.",
                reply_markup=main_menu(message.from_user.id),
            )
            return

    await message.answer(
        "Выберите дату:",
        reply_markup=await render_calendar(),
    )

@dp.callback_query(F.data == "back_menu")
async def back_menu(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Выберите раздел:",
        reply_markup=main_menu(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_calendar")
async def back_to_calendar(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Выберите дату:",
        reply_markup=await render_calendar(),
    )
    await callback.answer()


@dp.callback_query(F.data == "booking_restart")
async def booking_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    pending_token = find_pending_token_by_user(callback.from_user.id)
    if pending_token:
        await callback.answer(
            "Сначала отмените текущую заявку кнопкой «Отменить заявку ❌».",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "Выберите дату:",
        reply_markup=await render_calendar(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel_pending_"))
async def cancel_pending_request(callback: CallbackQuery) -> None:
    payment_token = callback.data[len("cancel_pending_"):]
    pending = pending_payments.get(payment_token)
    if not pending:
        await callback.answer("Заявка уже отменена или не найдена.", show_alert=True)
        return

    if callback.from_user.id != pending["user_id"]:
        await callback.answer("Эта кнопка не для вас.", show_alert=True)
        return

    pending_payments.pop(payment_token, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Заявка отменена. Можете выбрать новую дату и время.",
        reply_markup=main_menu(callback.from_user.id),
    )

    if pending.get("notified"):
        date_human = datetime.strptime(pending["date_str"], "%Y-%m-%d").strftime("%d.%m.%Y")
        await safe_send_message(
            ADMIN_ID,
            "❌ <b>Запись была отменена</b>\n\n"
            f"Имя: <b>{pending['name']}</b>\n"
            f"ID: <code>{pending['user_id']}</code>\n"
            f"Дата: <b>{date_human}</b>\n"
            f"Время: <b>{pending['time_str']}</b>",
        )

    await callback.answer()


# =========================
# 🔄 ПЕРЕКЛЮЧЕНИЕ МЕСЯЦЕВ
# =========================


@dp.callback_query(F.data.startswith("prev_"))
async def prev_month(callback: CallbackQuery, state: FSMContext) -> None:
    _, year, month = callback.data.split("_")
    year_i, month_i = int(year), int(month)

    if month_i == 1:
        month_i = 12
        year_i -= 1
    else:
        month_i -= 1

    admin_mode = await state.get_state() == AdminStates.waiting_admin_date.state
    await callback.message.edit_reply_markup(
        reply_markup=await render_calendar(year_i, month_i, admin_mode=admin_mode),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("next_"))
async def next_month(callback: CallbackQuery, state: FSMContext) -> None:
    _, year, month = callback.data.split("_")
    year_i, month_i = int(year), int(month)

    if month_i == 12:
        month_i = 1
        year_i += 1
    else:
        month_i += 1

    admin_mode = await state.get_state() == AdminStates.waiting_admin_date.state
    await callback.message.edit_reply_markup(
        reply_markup=await render_calendar(year_i, month_i, admin_mode=admin_mode),
    )
    await callback.answer()


# =========================
# 📆 ВЫБОР ДАТЫ
# =========================


@dp.callback_query(F.data.startswith("date_"))
async def select_date(callback: CallbackQuery) -> None:
    _, year, month, day = callback.data.split("_")
    date_obj = datetime(
        int(year),
        int(month),
        int(day),
    ).date()
    date_str_db = date_obj.strftime("%Y-%m-%d")
    slots = await get_available_slots_by_date(date_str_db)

    if not slots:
        await callback.answer(
            "На эту дату нет свободных слотов. Выберите другой день.",
            show_alert=True,
        )
        return

    kb = generate_time_slots_keyboard(date_str_db, slots)

    await callback.message.edit_text(
        f"Вы выбрали дату: <b>{date_obj.strftime('%d.%m.%Y')}</b>\n"
        f"Теперь выберите время:",
        reply_markup=kb,
    )
    await callback.answer()


# =========================
# ⏰ ВЫБОР ВРЕМЕНИ (FSM)
# =========================


@dp.callback_query(F.data.startswith("time_"))
async def select_time(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    existing = await get_user_booking(user_id)
    if existing:
        await callback.answer(
            "У вас уже есть активная запись. Сначала отмените её.",
            show_alert=True,
        )
        return

    for pending in pending_payments.values():
        if pending["user_id"] == user_id and not pending["confirmed"]:
            await callback.answer(
                "У вас уже есть заявка на бронь. Дождитесь подтверждения оплаты.",
                show_alert=True,
            )
            return

    _, slot_id_str = callback.data.split("_")
    slot_id = int(slot_id_str)

    # Найдём дату/время этого слота
    slots_date = None
    slots_time = None

    # Небольшой обход: получим дату/время для выбранного слота
    # (можно оптимизировать отдельной функцией в БД при необходимости)
    now = datetime.now().date()
    max_date = now + timedelta(days=30)
    date_iter = now
    while date_iter <= max_date and (slots_date is None):
        date_str = date_iter.strftime("%Y-%m-%d")
        slots = await get_available_slots_by_date(date_str)
        for s_id, s_time in slots:
            if s_id == slot_id:
                slots_date = date_str
                slots_time = s_time
                break
        date_iter += timedelta(days=1)

    if slots_date is None or slots_time is None:
        await callback.answer(
            "Этот слот больше недоступен. Пожалуйста, выберите другую дату.",
            show_alert=True,
        )
        return

    await state.update_data(
        slot_id=slot_id,
        date_str=slots_date,
        time_str=slots_time,
    )

    await callback.message.edit_text(
        f"📅 Вы выбрали:\n\n"
        f"<b>{datetime.strptime(slots_date, '%Y-%m-%d').strftime('%d.%m.%Y')} в {slots_time}</b>\n\n"
        f"Пожалуйста, отправьте своё <b>имя</b>:",
        reply_markup=booking_back_inline(),
    )
    await state.set_state(BookingStates.waiting_for_name)
    await callback.answer()

@dp.message(BookingStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Имя слишком короткое. Введите, пожалуйста, ещё раз.", reply_markup=booking_back_inline())
        return

    await state.update_data(name=name)
    await state.set_state(BookingStates.waiting_for_phone)
    await message.answer("Теперь отправьте, пожалуйста, ваш <b>номер телефона</b>:", reply_markup=booking_back_inline())


@dp.message(BookingStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    if len(phone) < 5:
        await message.answer("Номер телефона выглядит некорректно. Введите ещё раз:", reply_markup=booking_back_inline())
        return

    data = await state.get_data()
    date_str = data["date_str"]
    time_str = data["time_str"]
    name = data["name"]

    user = message.from_user
    user_id = user.id

    existing = await get_user_booking(user_id)
    if existing:
        await message.answer(
            "У вас уже есть активная запись. Новая заявка не создана.",
            reply_markup=main_menu(message.from_user.id),
        )
        await state.clear()
        return

    for pending in pending_payments.values():
        if pending["user_id"] == user_id and not pending["confirmed"]:
            await message.answer(
                "У вас уже есть заявка на бронь времени. Дождитесь подтверждения оплаты админом.",
                reply_markup=main_menu(message.from_user.id),
            )
            await state.clear()
            return

    payment_token = token_urlsafe(9)
    pending_payments[payment_token] = {
        "user_id": user_id,
        "username": user.username,
        "name": name,
        "phone": phone,
        "date_str": date_str,
        "time_str": time_str,
        "notified": False,
        "confirmed": False,
    }

    date_human = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")

    await message.answer(
        f"Для записи необходимо внести бронирование <b>{PREPAYMENT_AMOUNT}₽</b> 🤍\n"
        "(Стоимость бронирования входит в цену маникюра)\n"
        "❗️Бронирование времени — это отдельная услуга, она не возвращается.\n"
        "Но по договоренности можем перенести процедуру.\n"
        "Если переносите запись день в день или несколько раз — оплата сгорает.\n"
        f"Номер для оплаты:\n<b>{PAYMENT_PHONE}</b> ({PAYMENT_NAME})\n\n"
        "После оплаты отправьте чек и нажмите кнопку ниже.",
        reply_markup=pending_payment_inline(payment_token),
    )
    await message.answer(
        f"Заявка на запись: <b>{date_human} в {time_str}</b>",
        reply_markup=main_menu(message.from_user.id),
    )

    await state.clear()


# =========================
# 💸 ПРЕДОПЛАТА
# =========================


@dp.callback_query(F.data.startswith("paid_"))
async def client_paid(callback: CallbackQuery) -> None:
    payment_token = callback.data[5:]
    pending = pending_payments.get(payment_token)

    if not pending:
        await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return

    if callback.from_user.id != pending["user_id"]:
        await callback.answer("Эта кнопка не для вас.", show_alert=True)
        return

    if pending["confirmed"]:
        await callback.answer("Оплата уже подтверждена.", show_alert=True)
        return

    if pending["notified"]:
        await callback.answer("Вы уже отправили уведомление об оплате.", show_alert=True)
        return

    pending["notified"] = True
    date_human = datetime.strptime(pending["date_str"], "%Y-%m-%d").strftime("%d.%m.%Y")
    username = pending["username"] or "нет"
    if username != "нет":
        username = f"@{username}"

    admin_text = (
        "💸 <b>Клиент сообщил об оплате!</b>\n\n"
        f"Имя: <b>{pending['name']}</b>\n"
        f"Username: <b>{username}</b>\n"
        f"ID: <code>{pending['user_id']}</code>\n"
        f"Телефон: <b>{pending['phone']}</b>\n\n"
        f"Дата: <b>{date_human}</b>\n"
        f"Время: <b>{pending['time_str']}</b>"
    )
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=f"confirm_payment_{payment_token}",
                )
            ]
        ]
    )
    sent_to_admin = await safe_send_message(ADMIN_ID, admin_text, reply_markup=admin_kb)
    if not sent_to_admin:
        pending["notified"] = False
        await callback.answer(
            "Не удалось отправить уведомление администратору. Попробуйте позже.",
            show_alert=True,
        )
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Спасибо, отметили оплату. Ожидайте подтверждения администратора."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_payment_"))
async def admin_confirm_payment(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только администратор может подтвердить оплату.", show_alert=True)
        return

    payment_token = callback.data[len("confirm_payment_"):]
    pending = pending_payments.get(payment_token)
    if not pending:
        await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    if pending["confirmed"]:
        await callback.answer("Оплата уже подтверждена.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    existing = await get_user_booking(pending["user_id"])
    if existing:
        await callback.answer("У клиента уже есть запись. Подтверждение отменено.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        pending_payments.pop(payment_token, None)
        return

    available_slots = await get_available_slots_by_date(pending["date_str"])
    if pending["time_str"] not in [slot_time for _, slot_time in available_slots]:
        await callback.answer("Слот уже занят. Подтверждение невозможно.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        pending_payments.pop(payment_token, None)
        try:
            await bot.send_message(
                pending["user_id"],
                "К сожалению, выбранный слот уже занят. Выберите новую дату и время.",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        booking_id = await create_booking(
            user_id=pending["user_id"],
            name=pending["name"],
            phone=pending["phone"],
            date=pending["date_str"],
            time=pending["time_str"],
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка при подтверждении оплаты: %s", e)
        await callback.answer("Не удалось создать запись. Попробуйте позже.", show_alert=True)
        return

    pending["confirmed"] = True
    pending_payments.pop(payment_token, None)

    # Планируем напоминания только после подтверждения оплаты админом.
    schedule_reminders_for_booking(
        booking_id=booking_id,
        user_id=pending["user_id"],
        date_str=pending["date_str"],
        time_str=pending["time_str"],
    )

    date_human = datetime.strptime(pending["date_str"], "%Y-%m-%d").strftime("%d.%m.%Y")
    channel_text = (
        "📣 <b>Новая запись</b>\n\n"
        f"📅 <b>{date_human}</b>\n"
        f"⏰ <b>{pending['time_str']}</b>\n"
        f"👤 {pending['name']}"
    )
    if CHANNEL_NOTIFICATIONS_ENABLED and CHANNEL_ID is not None:
        sent_to_channel = await safe_send_message(CHANNEL_ID, channel_text)
        if not sent_to_channel:
            await callback.message.answer(
                "Внимание: запись сохранена, но сообщение в канал не отправлено "
                "(проверьте CHANNEL_ID и права бота)."
            )

    await safe_send_message(
        pending["user_id"],
        "Запись подтверждена 🤍\n\n"
        f"Дата: <b>{date_human}</b>\n"
        f"Время: <b>{pending['time_str']}</b>\n\n"
        "Ждём вас!",
    )

    if SALON_ADDRESS:
        await safe_send_message(
            pending["user_id"],
            f"📍 Адрес салона:\n<b>{SALON_ADDRESS}</b>",
        )

    if SALON_LATITUDE is not None and SALON_LONGITUDE is not None:
        try:
            await bot.send_location(
                chat_id=pending["user_id"],
                latitude=SALON_LATITUDE,
                longitude=SALON_LONGITUDE,
            )
        except Exception as e:  # noqa: BLE001
            logging.exception("Ошибка при отправке геолокации: %s", e)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Оплата подтверждена. Запись сохранена.")
    await callback.answer()

# =========================
# ❌ ОТМЕНА ЗАПИСИ
# =========================


@dp.message(F.text == "❌ Отменить запись")
async def cancel_booking_message(message: Message) -> None:
    user_id = message.from_user.id
    booking = await get_user_booking(user_id)
    pending_token = find_pending_token_by_user(user_id)

    if not booking and not pending_token:
        await message.answer("У вас нет активной записи или заявки.")
        return

    if pending_token:
        pending = pending_payments.pop(pending_token, None)
        await message.answer(
            "❌ Ваша заявка на запись отменена.",
            reply_markup=main_menu(message.from_user.id),
        )
        if pending and pending.get("notified"):
            date_human = datetime.strptime(pending["date_str"], "%Y-%m-%d").strftime("%d.%m.%Y")
            await safe_send_message(
                ADMIN_ID,
                "❌ <b>Запись была отменена</b>\n\n"
                f"Имя: <b>{pending['name']}</b>\n"
                f"ID: <code>{pending['user_id']}</code>\n"
                f"Дата: <b>{date_human}</b>\n"
                f"Время: <b>{pending['time_str']}</b>",
            )
        return

    booking_id, date_str, time_str, name, phone = booking

    await cancel_booking(user_id)
    cancel_reminders_for_booking(booking_id)

    await message.answer(
        "❌ Ваша запись отменена.\n\n"
        f"<b>{date_str} в {time_str}</b> снова доступно для бронирования.",
        reply_markup=main_menu(message.from_user.id),
    )

    await safe_send_message(
        ADMIN_ID,
        "❌ <b>Запись отменена клиентом</b>\n\n"
        f"👤 Имя: <b>{name}</b>\n"
        f"📞 Телефон: <b>{phone}</b>\n"
        f"📅 {date_str}\n"
        f"⏰ {time_str}",
    )


# =========================
# 👑 АДМИН-КОМАНДЫ
# =========================


def _admin_only(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@dp.message(Command("add_day"))
async def admin_add_day(message: Message) -> None:
    if not _admin_only(message):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование: /add_day YYYY-MM-DD HH:MM [HH:MM ...]\n"
            "Например: <code>/add_day 2026-03-10 10:00 12:00 14:00</code>",
        )
        return

    date_str = parts[1]
    custom_times = parts[2:]
    await add_working_day(date_str, custom_times)
    await message.answer(
        f"✅ Добавлен день <b>{date_str}</b> со слотами: {', '.join(custom_times)}",
    )



@dp.message(Command("add_slot"))
async def admin_add_slot(message: Message) -> None:
    if not _admin_only(message):
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Использование: /add_slot YYYY-MM-DD HH:MM\n"
            "Например: <code>/add_slot 2026-03-10 19:00</code>",
        )
        return

    date_str, time_str = parts[1], parts[2]
    await add_time_slot(date_str, time_str)
    await message.answer(
        f"✅ Добавлен слот <b>{date_str} {time_str}</b>",
    )


@dp.message(Command("del_slot"))
async def admin_del_slot(message: Message) -> None:
    if not _admin_only(message):
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Использование: /del_slot YYYY-MM-DD HH:MM",
        )
        return

    date_str, time_str = parts[1], parts[2]
    await delete_time_slot(date_str, time_str)
    await message.answer(
        f"✅ Удалён слот <b>{date_str} {time_str}</b>",
    )


@dp.message(Command("close_day"))
async def admin_close_day(message: Message) -> None:
    if not _admin_only(message):
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /close_day YYYY-MM-DD")
        return

    date_str = parts[1]
    await close_day(date_str)
    await message.answer(
        f"✅ День <b>{date_str}</b> полностью закрыт (все слоты удалены).",
    )


@dp.message(Command("day"))
async def admin_day_schedule(message: Message) -> None:
    if not _admin_only(message):
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /day YYYY-MM-DD")
        return

    date_str = parts[1]
    rows = await get_day_schedule(date_str)
    if not rows:
        await message.answer(f"На дату <b>{date_str}</b> нет слотов.")
        return

    lines = [f"📅 Расписание на <b>{date_str}</b>:\n"]
    for time_str, is_available, name, phone, user_id in rows:
        if is_available:
            lines.append(f"🕒 <b>{time_str}</b> — свободно")
        else:
            lines.append(
                f"🕒 <b>{time_str}</b> — занято ({name}, {phone}, id:{user_id})"
            )

    await message.answer("\n".join(lines))


@dp.message(Command("cancel_user"))
async def admin_cancel_user(message: Message) -> None:
    if not _admin_only(message):
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /cancel_user USER_ID")
        return

    try:
        target_user_id = int(parts[1])
    except ValueError:
        await message.answer("USER_ID должен быть числом.")
        return

    booking = await get_user_booking(target_user_id)
    if not booking:
        await message.answer("У пользователя нет активной записи.")
        return

    booking_id, date_str, time_str, name, phone = booking

    await cancel_booking(target_user_id)
    cancel_reminders_for_booking(booking_id)

    await message.answer(
        "✅ Запись клиента отменена:\n\n"
        f"👤 {name} ({phone})\n"
        f"📅 {date_str}\n"
        f"⏰ {time_str}",
    )

    try:
        await bot.send_message(
            target_user_id,
            "Ваша запись была отменена администратором. "
            "При необходимости запишитесь заново.",
        )
    except Exception:  # noqa: BLE001
        pass


# =========================
# ▶ ЗАПУСК
# =========================


async def main() -> None:
    await init_db()
    scheduler.start()
    await restore_reminders()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

