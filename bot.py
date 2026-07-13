"""
Telegram-бот для расшифровки зарплат · Коченевских бюро
Данные из Google Sheets через CSV. Python-telegram-bot v21+.
Версия 4 — лист паролей, авто-отдел, выбор года.
"""
import asyncio
import os

for key in list(os.environ):
    if "proxy" in key.lower():
        del os.environ[key]

import logging
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

import pandas as pd

# ── Настройка ───────────────────────────────────────────────────
TELEGRAM_TOKEN = "8511200367:AAEPY0SVgMaXUGy6iMDSWR1COy171-GoaWM"

SHEET_URL = "https://docs.google.com/spreadsheets/d/1kTSGJUmb2AOTovaIDtzC6e-EltiZeeOGV6yJr8FWs-Q/export?format=csv&gid={gid}"

PROXY_URL = os.environ.get("BOT_PROXY", "")

# GID вкладок
PASSWORDS_GID = 836125341

SHEETS = {
    "Производство (диз, виз, проектир)": 0,
    "Разработка (менеджеры)":           1130598027,
    "Реализация (комплектация)":        1657923656,
    "Реализация (стройка)":            1061837590,
    "Продажи":                          1105809423,
    "Административный персонал":        1161112667,
}

BACK = "🔙 Назад"
ANOTHER_MONTH = "📅 Другой месяц"
ANOTHER_YEAR = "📆 Другой год"

PASSWORD, YEAR, MONTH = range(3)

logging.basicConfig(format="%(asctime)s %(message)s", level=logging.INFO)


# ── Загрузка данных ─────────────────────────────────────────────

def load_csv(gid: int) -> pd.DataFrame:
    url = SHEET_URL.format(gid=gid)
    return pd.read_csv(url, dtype=str)


def safe_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and pd.isna(val):
        return ""
    return str(val).strip()


def load_passwords() -> dict[str, dict]:
    df = load_csv(PASSWORDS_GID)
    result = {}
    for _, row in df.iterrows():
        pwd = safe_str(row.get("Пароль"))
        if not pwd:
            continue
        result[pwd] = {
            "fio": safe_str(row.get("ФИО") or row.get("фио")),
            "short_name": safe_str(row.get("Обращение")),
            "department": safe_str(row.get("Отдел")),
        }
    logging.info("Загружено %s паролей", len(result))
    return result


def resolve_department(name: str) -> tuple[str | None, int | None]:
    if name in SHEETS:
        return name, SHEETS[name]
    name_lower = name.strip().lower()
    for key, gid in SHEETS.items():
        if name_lower in key.lower():
            return key, gid
    return None, None


def find_salary(department: str, fio: str, month: int, year: int) -> dict | None:
    _, gid = resolve_department(department)
    if gid is None:
        return None
    df = load_csv(gid)
    for _, row in df.iterrows():
        if safe_str(row.get("Месяц")) == str(month) and \
           safe_str(row.get("Год")) == str(year) and \
           safe_str(row.get("ФИО")) == fio:
            return row.to_dict()
    return None


def get_short_name(entry: dict) -> str:
    short = entry.get("short_name", "")
    if short:
        return short
    fio = entry.get("fio", "")
    if fio:
        parts = fio.split()
        return parts[1] if len(parts) >= 3 else parts[0]
    return "коллега"


def parse_money(val) -> str:
    if pd.isna(val):
        return "—"
    s = str(val).replace("\u00A0", "").replace(" ", "").replace(",", ".").strip()
    try:
        num = int(float(s))
        return f"{num:,} ₽".replace(",", " ")
    except (ValueError, OverflowError):
        return str(val)


# ── Форматирование ──────────────────────────────────────────────

MONTHS_RU = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

MONTH_NAMES = {name: i for i, name in enumerate(MONTHS_RU) if i > 0}


def format_response(department: str, data: dict, entry: dict) -> str:
    name = get_short_name(entry)
    month = data.get("Месяц", "?")
    year = data.get("Год", "?")
    comment = data.get("Комментарий", "")
    payout = parse_money(data.get("К выплате"))

    try:
        month_word = MONTHS_RU[int(month)]
    except (ValueError, IndexError):
        month_word = month

    header = f"📊 *{name}, твоя зарплата за {month_word} {year}*"

    dept = department
    if dept.startswith("Производство"):
        lines = [
            f"🏗 Проекты:  {parse_money(data.get('Проекты'))}",
            f"📝 Доп. работа:  {parse_money(data.get('Дополнительная работа'))}",
            f"🚚 Транспорт:  {parse_money(data.get('Транспортные расходы'))}",
        ]
    elif "Разработка" in dept:
        lines = [
            f"💰 Оклад:  {parse_money(data.get('Оклад'))}",
            f"📊 % мотивации:  {parse_money(data.get('% Мотивации'))}",
            f"🏆 KPI:  {parse_money(data.get('KPI'))}",
            f"🚚 Транспорт:  {parse_money(data.get('Возмещения и транспорт'))}",
        ]
    elif "комплектация" in dept:
        lines = [
            f"💰 Оклад:  {parse_money(data.get('Оклад'))}",
            f"📈 % с продаж:  {parse_money(data.get('% С продаж'))}",
            f"🎁 Доп выплаты:  {parse_money(data.get('Доп выплаты'))}",
            f"🚚 Транспорт:  {parse_money(data.get('Возмещения и транспорт'))}",
        ]
    elif "стройка" in dept:
        lines = [
            f"💰 Оклад:  {parse_money(data.get('Оклад'))}",
            f"📈 % от продаж:  {parse_money(data.get('% от продаж'))}",
            f"🏆 KPI:  {parse_money(data.get('KPI'))}",
            f"🏗 Мотивация проекты:  {parse_money(data.get('Мотивация проекты'))}",
            f"🚚 Транспорт:  {parse_money(data.get('Возмещения и транспорт'))}",
        ]
    elif "Продажи" in dept:
        lines = [
            f"💰 Оклад:  {parse_money(data.get('Оклад'))}",
            f"📈 % от продаж:  {parse_money(data.get('% от продаж'))}",
            f"🚚 Транспорт:  {parse_money(data.get('Возмещения и транспорт'))}",
        ]
    else:
        lines = [
            f"💰 Фикса:  {parse_money(data.get('Фикса'))}",
            f"📝 Доп работы:  {parse_money(data.get('Доп Работы'))}",
            f"🚚 Транспорт:  {parse_money(data.get('Возмещения и транспорт'))}",
        ]

    body = "\n".join(lines)
    total = f"💵 *К выплате:  {payout}*"

    response = f"{header}\n\n{body}\n\n{total}"

    if comment and str(comment).strip():
        response += f"\n\n📌 {comment}"

    return response


# ── Клавиатуры ──────────────────────────────────────────────────

def month_keyboard() -> list[list[str]]:
    return [
        ["Январь", "Февраль", "Март", "Апрель"],
        ["Май", "Июнь", "Июль", "Август"],
        ["Сентябрь", "Октябрь", "Ноябрь", "Декабрь"],
    ]


def year_keyboard() -> list[list[str]]:
    now = datetime.now().year
    return [[str(y) for y in range(now - 2, now + 1)]]


# ── Обработчики ─────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info("/start: новый диалог")

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Я помогу узнать расшифровку твоей зарплаты.\n"
        "Введи свой пароль 🔑",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PASSWORD


async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pwd = update.message.text.strip()

    # Загружаем пароли напрямую, а не из bot_data (надёжнее на вебхуке)
    try:
        passwords = load_passwords()
    except Exception as e:
        logging.error("Ошибка загрузки паролей в get_password: %s", e)
        await update.message.reply_text(
            "Не могу загрузить данные. Попробуй /start через минуту 🙏",
        )
        return ConversationHandler.END

    logging.info("Поиск пароля: «%s» среди %s записей", pwd, len(passwords))

    entry = passwords.get(pwd)

    if entry is None:
        await update.message.reply_text(
            "Не узнаю этот пароль 🤔 Попробуй ещё раз.\n\n"
            "Если потерял — спроси у своего руководителя или напиши Лизе @elizavetkkaa16.",
        )
        return PASSWORD

    context.user_data["entry"] = entry
    name = get_short_name(entry)

    keyboard = year_keyboard()
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\nЗа какой год показать зарплату?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return YEAR


async def get_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    try:
        year = int(text)
    except ValueError:
        await update.message.reply_text("Выбери год из списка 👆")
        return YEAR

    context.user_data["year"] = year

    keyboard = month_keyboard() + [[BACK]]
    await update.message.reply_text(
        f"Год: {year}. За какой месяц? 📆",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return MONTH


async def get_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == BACK:
        keyboard = year_keyboard()
        await update.message.reply_text(
            "За какой год показать?",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return YEAR

    month = MONTH_NAMES.get(update.message.text.strip().lower())

    if month is None:
        await update.message.reply_text("Выбери месяц из списка 👆")
        return MONTH

    entry = context.user_data["entry"]
    year = context.user_data["year"]

    try:
        data = find_salary(entry["department"], entry["fio"], month, year)
    except Exception as e:
        logging.error("Ошибка загрузки: %s", e)
        await update.message.reply_text(
            "Что-то пошло не так с загрузкой данных. Попробуй через пару минут или напиши Лизе @elizavetkkaa16 🙏",
            reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
        )
        return ConversationHandler.END

    if data is None:
        debug_info = (
            f"🔍 *Что ищу:*\n"
            f"• Отдел: {entry['department']}\n"
            f"• ФИО: «{entry['fio']}»\n"
            f"• Месяц: {month}\n"
            f"• Год: {year}\n\n"
        )
        try:
            _, gid = resolve_department(entry["department"])
            if gid is not None:
                df = load_csv(gid)
                months_found = sorted(df["Месяц"].dropna().unique().tolist()) if "Месяц" in df.columns else []
                years_found = sorted(df["Год"].dropna().unique().tolist()) if "Год" in df.columns else []
                fios = df["ФИО"].dropna().unique().tolist()[:10] if "ФИО" in df.columns else []
                debug_info += (
                    f"📋 *Что есть в таблице отдела:*\n"
                    f"• Месяцы: {', '.join(str(m) for m in months_found)}\n"
                    f"• Годы: {', '.join(str(y) for y in years_found)}\n"
                    f"• ФИО (первые 10): {', '.join(f'«{f}»' for f in fios)}"
                )
        except Exception:
            pass

        await update.message.reply_text(
            "За этот месяц данных пока нет 🤔\n\n" + debug_info,
            reply_markup=ReplyKeyboardMarkup(
                month_keyboard() + [[ANOTHER_YEAR], [BACK]],
                resize_keyboard=True,
            ),
        )
        return MONTH

    response = format_response(entry["department"], data, entry)
    await update.message.reply_text(
        response,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["✅ Всё верно"], ["❓ Есть вопросы"], [ANOTHER_MONTH], [ANOTHER_YEAR]],
            resize_keyboard=True,
        ),
    )
    return MONTH


async def handle_after_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text

    if text == ANOTHER_MONTH:
        keyboard = month_keyboard() + [[ANOTHER_YEAR]]
        await update.message.reply_text(
            "Конечно! За какой месяц? 📆",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return MONTH

    if text == ANOTHER_YEAR:
        keyboard = year_keyboard()
        await update.message.reply_text(
            "За какой год показать?",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return YEAR

    if text == BACK:
        keyboard = year_keyboard()
        await update.message.reply_text(
            "За какой год показать?",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return YEAR

    if "верно" in text.lower():
        await update.message.reply_text(
            "Супер! Рада, что всё сошлось 💚",
            reply_markup=ReplyKeyboardMarkup(
                [[ANOTHER_MONTH], [ANOTHER_YEAR], ["/start"]],
                resize_keyboard=True,
            ),
        )
        return MONTH
    elif "вопрос" in text.lower():
        await update.message.reply_text(
            "Поняла! Напиши Лизе @elizavetkkaa16 — она поможет разобраться ✍️",
            reply_markup=ReplyKeyboardMarkup(
                [[ANOTHER_MONTH], [ANOTHER_YEAR], ["/start"]],
                resize_keyboard=True,
            ),
        )
        return MONTH
    else:
        return await get_month(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Без проблем! /start — когда понадоблюсь снова 👋",
        reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
    )
    return ConversationHandler.END


async def handle_month_or_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip() if update.message.text else ""

    if text in [ANOTHER_MONTH, ANOTHER_YEAR, BACK] \
       or "верно" in text.lower() \
       or "вопрос" in text.lower():
        return await handle_after_view(update, context)

    return await get_month(update, context)


async def handle_year_or_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip() if update.message.text else ""

    if text == BACK:
        await update.message.reply_text("Введи свой пароль 🔑")
        return PASSWORD

    return await get_year(update, context)


# ── Запуск ──────────────────────────────────────────────────────

async def main():
    proxy = PROXY_URL if PROXY_URL else None
    request_args = dict(http_version="1.1", connect_timeout=15, read_timeout=45, write_timeout=10)
    if proxy:
        request_args["proxy"] = proxy
        proxy_label = proxy
    else:
        proxy_label = "без прокси"

    request = HTTPXRequest(**request_args)
    updater_request = HTTPXRequest(**request_args)
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .get_updates_request(updater_request)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
            YEAR:     [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_year_or_back)],
            MONTH:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_month_or_action)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    app.add_handler(conv)

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        port = int(os.environ.get("PORT", "8443"))
        webhook_url = f"{render_url}/webhook"
        logging.info("Бот запущен на Render, вебхук: %s", webhook_url)
        await app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=webhook_url,
            close_loop=False,
        )
    else:
        logging.info("Бот запущен через поллинг (%s)", proxy_label)
        await app.run_polling(close_loop=False)


if __name__ == "__main__":
    asyncio.run(main())
