"""
Telegram-бот для расшифровки зарплат + агент-комплектатор · Коченевских бюро
Данные из Google Sheets через CSV. Python-telegram-bot v21+.
Версия 5 — добавлен AI-агент для отдела комплектации.
"""
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
import httpx

# ── Настройка ───────────────────────────────────────────────────
TELEGRAM_TOKEN = "8511200367:AAEPY0SVgMaXUGy6iMDSWR1COy171-GoaWM"

SHEET_URL = "https://docs.google.com/spreadsheets/d/1kTSGJUmb2AOTovaIDtzC6e-EltiZeeOGV6yJr8FWs-Q/export?format=csv&gid={gid}"

PROXY_URL = os.environ.get("BOT_PROXY", "")

# AI API (OpenRouter — для агента-комплектатора)
AI_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODEL = "deepseek/deepseek-chat"  # дешёвая модель, ~$0.14/1M токенов
AI_API_URL = "https://openrouter.ai/api/v1/chat/completions"

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


# ── Загрузка базы знаний комплектатора ──────────────────────────

def load_knowledge(folder: str = "knowledge") -> str:
    """Загружает все .md файлы из папки knowledge/ в одну строку."""
    if not os.path.exists(folder):
        logging.warning("Папка %s не найдена", folder)
        return ""
    parts = []
    for fname in sorted(os.listdir(folder)):
        if fname.endswith(".md"):
            with open(os.path.join(folder, fname), "r", encoding="utf-8") as f:
                parts.append(f.read())
    result = "\n\n---\n\n".join(parts)
    logging.info("База знаний загружена: %s файлов, %s символов", len(parts), len(result))
    return result

KNOWLEDGE_KOMPLEKTATOR = load_knowledge("knowledge/")


# ── Классификатор вопросов ──────────────────────────────────────

KOMPLEKTATOR_KEYWORDS = [
    "накладная", "упд", "эльба", "счёт поставщика", "счет поставщика",
    "документооборот", "приемка", "приёмка", "передача товара",
    "дилерск", "бонус", "поставщик", "чек", "касса",
    "ккт", "закупочн", "шаблон", "чек-лист", "чек лист",
    "красный флаг", "комплектаци", "отгрузка",
    "закрывающий документ", "эдо", "счет-фактура", "счёт-фактура",
    "сф ", " сф", "дизайнерский", "дилер", "хранение документов",
    "архив", "брак", "поврежден", "некомплект", "принимающее лицо",
    "доставка", "ррц", "закупочная цена", "закупочную цену",
    "агентский", "агентское", "подрядчик", "физлицо", "юрлицо",
    "контрагент", "реквизит", "офд", "сно", "усн", "тендер", "спецификация", 
    "артикул", "склад", "отгрузка со склада",
    "возврат", "обмен", "гарантия", "рекламация", "дефект",
]

SALARY_KEYWORDS = [
    "зарплата", "оклад", "премия", "сколько заработал",
    "выплата", "ведомость", "расшифровка", "зп",
    "к выплате", "мотиваци", "kpi",
]


def classify_question(text: str) -> str:
    """Возвращает 'komplektator' или 'salary'. По умолчанию — 'komplektator'."""
    text_lower = text.lower()

    # Явные команды зарплатного флоу
    if text_lower in ["/start", "start", "/cancel", "cancel"]:
        return "salary"


    for kw in SALARY_KEYWORDS:
        if kw in text_lower:
            # Проверяем, что это не ложное срабатывание
            # "чек" в контексте комплектации — не про зарплату
            return "salary"

    for kw in KOMPLEKTATOR_KEYWORDS:
        if kw in text_lower:
            return "komplektator"

    # Если ничего не подошло и это не пароль — комплектатор
    return "komplektator"


def is_password(text: str) -> bool:
    """Проверяет, похоже ли сообщение на пароль (короткое, цифры/буквы)."""
    t = text.strip()
    # Пароли обычно короткие
    if len(t) > 20:
        return False
    # Если похоже на вопрос — не пароль
    if "?" in t or " " in t and len(t.split()) > 3:
        return False
    return True


# ── AI-агент комплектатора ─────────────────────────────────────

SYSTEM_PROMPT_KOMPLEKTATOR = (
    "Ты — AI-ассистент отдела комплектации компании «Коченевских бюро».\n"
    "Твоя задача — помочь комплектаторам с документооборотом, правилами оформления сделок, "
    "работой в Эльбе, приемкой товара, чеками, поставщиками и шаблонами сообщений.\n\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Отвечай ТОЛЬКО на основе базы знаний ниже. НИКОГДА не додумывай.\n"
    "2. Если ответа нет в регламенте — честно скажи: «В регламенте этого нет. Уточни у руководителя отдела реализации или МД.»\n"
    "3. По КРАСНЫМ ФЛАГАМ (нестандартные ситуации) — НИКОГДА не давай самостоятельных решений. "
    "Напоминай: «Это красный флаг. Передай вопрос руководителю или МД.»\n"
    "4. Если сотрудник просит шаблон сообщения — дай точный текст из базы. Не перефразируй.\n"
    "5. Общайся как коллега: спокойно, по делу, без официоза.\n"
    "6. НИКОГДА не лезь в финансы, зарплаты и бухгалтерию. Это не твоя зона.\n\n"
    "=== БАЗА ЗНАНИЙ ===\n"
)


async def call_ai(system: str, question: str) -> str:
    """Отправляет запрос к AI API и возвращает ответ."""
    if not AI_API_KEY:
        return (
            "🤖 Агент-комплектатор пока не подключён к AI API.\n"
            "Добавь переменную OPENROUTER_API_KEY в настройки Render.\n\n"
            "А пока — посмотри ответ в регламенте вручную или спроси у руководителя."
        )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": 1500,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(AI_API_URL, json=body, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                answer = data["choices"][0]["message"]["content"]
                return answer.strip()
            else:
                logging.error("AI API error: %s %s", resp.status_code, resp.text[:200])
                return (
                    "⚠️ Не смог получить ответ от AI. Попробуй через минуту "
                    "или спроси у руководителя."
                )
    except Exception as e:
        logging.error("AI API exception: %s", e)
        return (
            "⚠️ AI-агент временно недоступен. Попробуй позже "
            "или посмотри ответ в регламенте."
        )


async def handle_komplektator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает вопрос к агенту-комплектатору."""
    question = update.message.text.strip()

    if not KNOWLEDGE_KOMPLEKTATOR:
        await update.message.reply_text(
            "📁 База знаний комплектатора не загружена. "
            "Проверь папку knowledge/ на сервере."
        )
        return

    # Показываем что бот думает
    await update.message.chat.send_action("typing")

    system = SYSTEM_PROMPT_KOMPLEKTATOR + KNOWLEDGE_KOMPLEKTATOR[:60000]
    answer = await call_ai(system, question)

    # Добавляем подсказку для навигации
    nav_hint = "\n\n💡 _Это был агент-комплектатор. Хочешь узнать зарплату? Напиши /start_"

    await update.message.reply_text(
        answer + nav_hint,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
    )


# ── Загрузка данных (существующий код) ──────────────────────────

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


# ── Маршрутизатор сообщений (новый!) ────────────────────────────

async def router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """
    Перехватывает ВСЕ текстовые сообщения ДО ConversationHandler.
    Если это вопрос комплектатора и пользователь не в диалоге зарплат —
    отправляет агенту-комплектатору.
    Если /start или пароль — отдаёт ConversationHandler.
    """
    text = update.message.text.strip() if update.message.text else ""

    # /start всегда запускает зарплатный флоу
    if text == "/start":
        return None  # пропускаем в ConversationHandler
        
    # Похоже на пароль и пользователь ещё не вошёл - отдаём ConversationHandler
    if is_password(text) and not context.user_data.get("entry"):
        return None

    # Если пользователь уже ввёл пароль и находится в диалоге — не трогаем
    if context.user_data.get("entry"):
        return None  # пропускаем в ConversationHandler

    # Классифицируем
    agent = classify_question(text)

    if agent == "salary":
        # Возможно пароль — отдаём в ConversationHandler
        if is_password(text):
            return None  # пропускаем
        else:
            # Вопрос про зарплату без пароля — предлагаем /start
            await update.message.reply_text(
                "Для просмотра зарплаты нужен пароль 🔑\n"
                "Напиши /start и введи пароль.",
                reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
            )
            return ConversationHandler.END

    # Комплектатор
    # Но сначала — если похоже на пароль, не отправляем агенту
    if is_password(text):
        await update.message.reply_text(
            "Похоже на пароль 🔑\n"
            "Напиши /start и введи пароль для просмотра зарплаты.\n\n"
            "Если у тебя вопрос по документообороту — напиши его словами, и я помогу.",
            reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
        )
        return ConversationHandler.END

    await handle_komplektator(update, context)
    return ConversationHandler.END


# ── Обработчики диалога зарплат ─────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info("/start: новый диалог")
    context.user_data.clear()  # очищаем предыдущий диалог

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Я помогу узнать расшифровку твоей зарплаты.\n"
        "Введи свой пароль 🔑",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PASSWORD


async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pwd = update.message.text.strip()

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
    context.user_data.clear()
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

def main():
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

    # 1. Маршрутизатор: перехватывает ВСЕ сообщения ДО диалога зарплат
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, router),
        group=0,  # высший приоритет
    )

    # 2. Диалог зарплат (ConversationHandler)
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
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=webhook_url,
        )
    else:
        logging.info("Бот запущен через поллинг (%s)", proxy_label)
        app.run_polling()


if __name__ == "__main__":
    main()
