from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import calendar
from datetime import datetime, timedelta


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


def generate_calendar(year: int | None = None, month: int | None = None) -> InlineKeyboardMarkup:
    """
    Генерация календаря на inline‑кнопках.
    Доступны только дни от сегодня и максимум на 30 дней вперёд.
    """
    now = datetime.now()
    max_date = now.date() + timedelta(days=30)

    year = year or now.year
    month = month or now.month

    keyboard: list[list[InlineKeyboardButton]] = []

    # =========================
    # 🔄 ПЕРЕКЛЮЧЕНИЕ МЕСЯЦЕВ
    # =========================
    keyboard.append(
        [
            InlineKeyboardButton(
                text="⬅",
                callback_data=f"prev_{year}_{month}",
            ),
            InlineKeyboardButton(
                text=_format_month_title(year, month),
                callback_data="ignore",
            ),
            InlineKeyboardButton(
                text="➡",
                callback_data=f"next_{year}_{month}",
            ),
        ]
    )

    # =========================
    # 📅 ДНИ НЕДЕЛИ
    # =========================
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append(
        [
            InlineKeyboardButton(text=day, callback_data="ignore")
            for day in days
        ]
    )

    # =========================
    # 📆 ДНИ МЕСЯЦА
    # =========================
    month_calendar = calendar.monthcalendar(year, month)

    for week in month_calendar:
        row: list[InlineKeyboardButton] = []

        for day in week:
            if day == 0:
                row.append(
                    InlineKeyboardButton(
                        text=" ",
                        callback_data="ignore",
                    )
                )
                continue

            selected_date = datetime(year, month, day).date()

            if selected_date < now.date() or selected_date > max_date:
                row.append(
                    InlineKeyboardButton(
                        text="❌",
                        callback_data="ignore",
                    )
                )
            else:
                row.append(
                    InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"date_{year}_{month}_{day}",
                    )
                )

        keyboard.append(row)

    # =========================
    # 🔙 КНОПКА НАЗАД
    # =========================
    keyboard.append(
        [
            InlineKeyboardButton(
                text="⬅ Назад в меню",
                callback_data="back_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def generate_time_slots_keyboard(date_str: str, slots: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """
    Генерация клавиатуры с временными слотами на выбранную дату.
    slots: список кортежей (slot_id, "HH:MM").
    """
    if not slots:
        # Пустая клавиатура с кнопкой "Назад"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅ Назад к календарю",
                        callback_data="back_to_calendar",
                    )
                ]
            ]
        )

    keyboard: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    for slot_id, time in slots:
        row.append(
            InlineKeyboardButton(
                text=time,
                callback_data=f"time_{slot_id}",
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton(
                text="⬅ Назад к календарю",
                callback_data="back_to_calendar",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)