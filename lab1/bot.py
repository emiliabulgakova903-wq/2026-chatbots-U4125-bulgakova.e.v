"""
bot.py — основной модуль Telegram-бота «Tracker Production Improvement Service».

Бот позволяет пользователю:
  • добавлять задачи с приоритетом через Inline-кнопки (/add);
  • просматривать список активных задач с управлением (/tasks);
  • устанавливать напоминания по дате и времени;
  • сохранять все данные в SQLite, чтобы задачи не пропадали после перезапуска.

Требования: python-telegram-bot >= 20, python-dotenv, APScheduler.
"""

import csv
import io
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import database as db

# ── Загрузка переменных окружения из файла .env ───────────────────────────────
load_dotenv()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# URL прокси-сервера (необязательно). Примеры:
#   socks5://user:pass@host:port
#   http://user:pass@host:port
# Если переменная не задана — прокси не используется.
PROXY_URL: str | None = os.getenv("PROXY_URL") or None

if not BOT_TOKEN:
    raise ValueError(
        "Токен бота не найден. Укажите BOT_TOKEN в файле .env."
    )

# ── Настройка логирования ─────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Состояния диалога для ConversationHandler ─────────────────────────────────
# Каждое состояние — это этап мастера добавления задачи
WAITING_TITLE    = 0   # Ожидаем ввод названия задачи
WAITING_PRIORITY = 1   # Ожидаем выбор приоритета через Inline-кнопки
WAITING_REMINDER = 2   # Ожидаем ввод даты напоминания (или пропуск)

# ── Emoji-метки приоритетов для красивого отображения ─────────────────────────
PRIORITY_EMOJI = {
    "Высокий": "🔴",
    "Средний": "🟡",
    "Низкий":  "🟢",
}


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def build_tasks_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """
    Создаёт Inline-клавиатуру для отдельной задачи:
    кнопки «Выполнено» и «Удалить».
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Выполнено", callback_data=f"done:{task_id}"
            ),
            InlineKeyboardButton(
                "🗑 Удалить", callback_data=f"delete:{task_id}"
            ),
        ]
    ])


def build_priority_keyboard() -> InlineKeyboardMarkup:
    """Создаёт Inline-клавиатуру выбора приоритета при добавлении задачи."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Высокий", callback_data="priority:Высокий"),
            InlineKeyboardButton("🟡 Средний", callback_data="priority:Средний"),
            InlineKeyboardButton("🟢 Низкий",  callback_data="priority:Низкий"),
        ]
    ])


def schedule_reminder(
    app: Application,
    task_id: int,
    user_id: int,
    title: str,
    remind_at: datetime,
) -> None:
    """
    Регистрирует одноразовое задание в job_queue для отправки напоминания
    пользователю в указанное время.

    :param app:       Экземпляр приложения (содержит job_queue).
    :param task_id:   ID задачи (передаётся в данных задания).
    :param user_id:   Telegram ID пользователя.
    :param title:     Текст задачи для уведомления.
    :param remind_at: Момент времени для отправки напоминания (локальное время).
    """
    # Вычисляем задержку в секундах от текущего момента.
    # Передаём `when` как float (секунды), а не как datetime-объект,
    # чтобы избежать конфликта часовых поясов: APScheduler внутри PTB
    # работает в UTC, а datetime.strptime() возвращает «наивный» datetime
    # без зоны — планировщик неверно интерпретировал бы его как UTC,
    # и задание срабатывало бы не вовремя (или вообще не срабатывало).
    delay = max(1.0, (remind_at - datetime.now()).total_seconds())

    app.job_queue.run_once(
        callback=send_reminder,
        when=delay,
        data={"task_id": task_id, "user_id": user_id, "title": title},
        name=f"reminder_{task_id}",
    )
    logger.info(
        "Напоминание для задачи #%d запланировано на %s (через %.0f сек.).",
        task_id, remind_at.strftime("%Y-%m-%d %H:%M"), delay,
    )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Callback-функция, вызываемая планировщиком: отправляет напоминание
    пользователю о конкретной задаче.
    """
    data    = context.job.data
    user_id = data["user_id"]
    task_id = data["task_id"]
    title   = data["title"]

    try:
        # Проверяем, что задача ещё не выполнена перед отправкой
        task = db.get_task_by_id(task_id)
        if task and not task["completed"]:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⏰ *Напоминание!*\n\n"
                    f"Задача: *{title}*\n"
                    f"Не забудьте выполнить её вовремя!"
                ),
                parse_mode="Markdown",
                reply_markup=build_tasks_keyboard(task_id),
            )
            logger.info(
                "Напоминание отправлено пользователю %d, задача #%d.", user_id, task_id
            )
        else:
            logger.info(
                "Напоминание для задачи #%d пропущено: задача уже выполнена или удалена.",
                task_id,
            )
    except Exception:
        logger.exception(
            "Ошибка при отправке напоминания пользователю %d, задача #%d.",
            user_id, task_id,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ КОМАНД
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start — приветствие и краткая инструкция."""
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        "Я — *Tracker Production Improvement Service Bot*.\n"
        "Помогаю отслеживать задачи по улучшению производства.\n\n"
        "📌 *Доступные команды:*\n"
        "• /add — добавить новую задачу\n"
        "• /tasks — показать список активных задач\n"
        "• /help — справка по боту\n\n"
        "Начните с команды /add, чтобы добавить первую задачу!",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help — подробная справка."""
    await update.message.reply_text(
        "📖 *Справка по боту*\n\n"
        "*Команды:*\n"
        "• /start — начало работы\n"
        "• /add — мастер добавления задачи (название → приоритет → напоминание)\n"
        "• /tasks — список всех активных задач\n"
        "• /help — эта справка\n\n"
        "*Приоритеты:*\n"
        "🔴 Высокий — срочные задачи\n"
        "🟡 Средний — плановые задачи\n"
        "🟢 Низкий — задачи по возможности\n\n"
        "*Напоминания:*\n"
        "При добавлении задачи можно указать дату и время напоминания "
        "в формате `ДД.ММ.ГГГГ ЧЧ:ММ`, например: `15.04.2026 09:00`.\n"
        "Введите «Нет» или «-», чтобы пропустить напоминание.\n\n"
        "*Управление задачами:*\n"
        "В списке /tasks у каждой задачи есть кнопки:\n"
        "✅ *Выполнено* — пометить задачу выполненной\n"
        "🗑 *Удалить* — удалить задачу из списка",
        parse_mode="Markdown",
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /tasks — выводит список всех активных задач
    пользователя, отсортированных по приоритету.
    """
    user_id = update.effective_user.id
    tasks   = db.get_tasks(user_id, only_active=True)

    if not tasks:
        await update.message.reply_text(
            "📭 У вас пока нет активных задач.\n"
            "Добавьте первую задачу командой /add!"
        )
        return

    await update.message.reply_text(
        f"📋 *Активные задачи* ({len(tasks)} шт.):",
        parse_mode="Markdown",
    )

    # Отправляем каждую задачу отдельным сообщением с кнопками управления
    for task in tasks:
        emoji    = PRIORITY_EMOJI.get(task["priority"], "⚪")
        reminder = (
            f"\n⏰ Напоминание: {task['remind_at']}"
            if task["remind_at"]
            else ""
        )
        text = (
            f"{emoji} *{task['title']}*\n"
            f"Приоритет: {task['priority']}{reminder}\n"
            f"Добавлено: {task['created_at']}"
        )
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=build_tasks_keyboard(task["id"]),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  МАСТЕР ДОБАВЛЕНИЯ ЗАДАЧИ (ConversationHandler)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 1/3 — запускает диалог добавления задачи:
    просит пользователя ввести название задачи.
    """
    await update.message.reply_text(
        "✏️ *Добавление новой задачи*\n\n"
        "Введите название задачи или /cancel для отмены:",
        parse_mode="Markdown",
    )
    return WAITING_TITLE


async def received_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 2/3 — получает название задачи, сохраняет его во временных данных
    пользователя и предлагает выбрать приоритет через Inline-кнопки.
    """
    title = update.message.text.strip()

    if len(title) < 3:
        await update.message.reply_text(
            "⚠️ Название слишком короткое (минимум 3 символа). Попробуйте ещё раз:"
        )
        return WAITING_TITLE

    # Сохраняем название во временном хранилище пользователя
    context.user_data["new_task_title"] = title

    await update.message.reply_text(
        f"📝 Задача: *{title}*\n\n"
        "Выберите приоритет:",
        parse_mode="Markdown",
        reply_markup=build_priority_keyboard(),
    )
    return WAITING_PRIORITY


async def received_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 3/3 — получает выбранный приоритет из callback-данных,
    сохраняет его и просит ввести дату напоминания.
    """
    query    = update.callback_query
    await query.answer()

    # Извлекаем выбранный приоритет из данных кнопки (формат "priority:Высокий")
    priority = query.data.split(":")[1]
    context.user_data["new_task_priority"] = priority

    emoji = PRIORITY_EMOJI.get(priority, "⚪")
    await query.edit_message_text(
        f"📝 Задача: *{context.user_data['new_task_title']}*\n"
        f"Приоритет: {emoji} {priority}\n\n"
        "⏰ Укажите дату и время напоминания в формате:\n"
        "`ДД.ММ.ГГГГ ЧЧ:ММ`\n\n"
        "Или введите «-» / «нет», чтобы пропустить напоминание:",
        parse_mode="Markdown",
    )
    return WAITING_REMINDER


async def received_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Финальный шаг — получает дату напоминания (или отказ от него),
    сохраняет задачу в базу данных и регистрирует напоминание в планировщике.
    """
    text    = update.message.text.strip().lower()
    user_id = update.effective_user.id
    title   = context.user_data["new_task_title"]
    priority = context.user_data["new_task_priority"]

    remind_at_str: str | None = None
    remind_dt: datetime | None = None

    # Пользователь отказался от напоминания
    if text in ("-", "нет", "no", "skip"):
        remind_at_str = None
    else:
        # Пробуем разобрать введённую дату в нескольких форматах
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                remind_dt     = datetime.strptime(update.message.text.strip(), fmt)
                remind_at_str = remind_dt.strftime("%Y-%m-%d %H:%M")
                break
            except ValueError:
                continue

        # Если ни один формат не подошёл — сообщаем об ошибке и повторяем шаг
        if remind_at_str is None:
            await update.message.reply_text(
                "❌ *Неверный формат даты.*\n\n"
                "Пожалуйста, используйте формат `ДД.ММ.ГГГГ ЧЧ:ММ`\n"
                "Например: `15.04.2026 09:00`\n\n"
                "Или введите «-», чтобы пропустить напоминание.",
                parse_mode="Markdown",
            )
            return WAITING_REMINDER

        # Проверяем, что дата не в прошлом
        if remind_dt and remind_dt <= datetime.now():
            await update.message.reply_text(
                "❌ *Дата напоминания уже прошла.*\n\n"
                "Укажите будущую дату или введите «-», чтобы пропустить.",
                parse_mode="Markdown",
            )
            return WAITING_REMINDER

    # Сохраняем задачу в базе данных
    task_id = db.add_task(
        user_id=user_id,
        title=title,
        priority=priority,
        remind_at=remind_at_str,
    )

    # Если напоминание задано — регистрируем задание в планировщике
    if remind_dt:
        schedule_reminder(
            app=context.application,
            task_id=task_id,
            user_id=user_id,
            title=title,
            remind_at=remind_dt,
        )
        reminder_text = f"\n⏰ Напомню: {update.message.text.strip()}"
    else:
        reminder_text = ""

    emoji = PRIORITY_EMOJI.get(priority, "⚪")
    await update.message.reply_text(
        f"✅ *Задача добавлена!*\n\n"
        f"📝 {title}\n"
        f"Приоритет: {emoji} {priority}{reminder_text}\n\n"
        f"Посмотреть все задачи: /tasks",
        parse_mode="Markdown",
    )

    # Очищаем временные данные пользователя
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Отменяет текущий диалог добавления задачи по команде /cancel."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Добавление задачи отменено.\n"
        "Вы можете начать снова командой /add."
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ INLINE-КНОПОК (управление задачами)
# ══════════════════════════════════════════════════════════════════════════════

async def callback_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие кнопки «Выполнено» — помечает задачу выполненной
    и обновляет сообщение в чате.
    """
    query   = update.callback_query
    await query.answer()

    task_id = int(query.data.split(":")[1])
    task    = db.get_task_by_id(task_id)

    # Проверяем, что задача принадлежит нажавшему пользователю
    if not task or task["user_id"] != update.effective_user.id:
        await query.answer("⚠️ Задача не найдена.", show_alert=True)
        return

    if task["completed"]:
        await query.answer("Задача уже отмечена выполненной.", show_alert=True)
        return

    db.complete_task(task_id)

    # Редактируем сообщение: убираем кнопки и ставим отметку о выполнении
    await query.edit_message_text(
        f"✅ *Выполнено:* {task['title']}\n"
        f"Приоритет: {PRIORITY_EMOJI.get(task['priority'], '⚪')} {task['priority']}",
        parse_mode="Markdown",
    )


async def callback_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Обрабатывает нажатие кнопки «Удалить» — удаляет задачу из базы данных
    и убирает сообщение из чата.
    """
    query   = update.callback_query
    await query.answer()

    task_id = int(query.data.split(":")[1])
    task    = db.get_task_by_id(task_id)

    if not task or task["user_id"] != update.effective_user.id:
        await query.answer("⚠️ Задача не найдена.", show_alert=True)
        return

    title = task["title"]
    db.delete_task(task_id)

    await query.edit_message_text(
        f"🗑 *Задача удалена:* {title}",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ОТЧЁТ ПО ВЫПОЛНЕННЫМ ЗАДАЧАМ (/report)
# ══════════════════════════════════════════════════════════════════════════════

# Порог: если выполненных задач больше этого числа — отправляем CSV-файл
REPORT_FILE_THRESHOLD = 10


def build_report_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора периода для отчёта."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 7 дней",    callback_data="report:7"),
            InlineKeyboardButton("📅 30 дней",   callback_data="report:30"),
        ],
        [
            InlineKeyboardButton("📅 Текущий месяц", callback_data="report:month"),
            InlineKeyboardButton("📋 Всё время",     callback_data="report:0"),
        ],
    ])


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /report — предлагает выбрать период отчёта."""
    await update.message.reply_text(
        "📊 *Отчёт по выполненным задачам*\n\n"
        "Выберите период, за который нужно сформировать отчёт:",
        parse_mode="Markdown",
        reply_markup=build_report_keyboard(),
    )


async def callback_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает выбор периода отчёта.
    При количестве задач <= REPORT_FILE_THRESHOLD отправляет текстовое сообщение,
    иначе формирует CSV-файл и отправляет его документом.
    """
    query   = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    period  = query.data.split(":")[1]   # "7", "30", "month" или "0"

    # Определяем количество дней для SQL-запроса
    if period == "month":
        # Количество дней с первого числа текущего месяца
        today = datetime.now()
        days  = today.day          # сколько дней прошло с начала месяца
        label = f"текущий месяц ({today.strftime('%B %Y')})"
    elif period == "0":
        days  = 0
        label = "всё время"
    else:
        days  = int(period)
        label = f"последние {days} дн."

    tasks = db.get_completed_tasks(user_id, days)

    if not tasks:
        await query.edit_message_text(
            f"📭 За период *«{label}»* выполненных задач не найдено.",
            parse_mode="Markdown",
        )
        return

    # ── Формируем текстовый отчёт ─────────────────────────────────────────────
    lines = [f"📊 *Отчёт: {label}* — {len(tasks)} задач\n"]
    for task in tasks:
        emoji = PRIORITY_EMOJI.get(task["priority"], "⚪")
        lines.append(
            f"📅 {task['created_at']}  |  ✅ {task['title']}  |  {emoji} {task['priority']}"
        )

    report_text = "\n".join(lines)

    # ── Короткий отчёт — отправляем текстом ──────────────────────────────────
    if len(tasks) <= REPORT_FILE_THRESHOLD:
        await query.edit_message_text(
            report_text,
            parse_mode="Markdown",
        )
        return

    # ── Длинный отчёт — генерируем CSV и отправляем файлом ───────────────────
    await query.edit_message_text(
        f"📊 Найдено *{len(tasks)}* выполненных задач за {label}.\n"
        "Формирую CSV-файл…",
        parse_mode="Markdown",
    )

    # Собираем CSV в памяти (без записи на диск)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["ID", "Задача", "Приоритет", "Дата создания"])
    for task in tasks:
        writer.writerow([
            task["id"],
            task["title"],
            task["priority"],
            task["created_at"],
        ])

    # Конвертируем StringIO → BytesIO для отправки через Telegram
    csv_bytes = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))  # utf-8-sig для Excel
    csv_bytes.name = f"report_{period}days.csv"

    filename = f"report_{label.replace(' ', '_')}.csv"
    await context.bot.send_document(
        chat_id=user_id,
        document=csv_bytes,
        filename=filename,
        caption=(
            f"📎 Отчёт за *{label}*: {len(tasks)} выполненных задач."
        ),
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАБОТЧИК НЕИЗВЕСТНЫХ СООБЩЕНИЙ
# ══════════════════════════════════════════════════════════════════════════════

async def unknown_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Реагирует на произвольные сообщения вне активного диалога."""
    await update.message.reply_text(
        "🤔 Я не понял команду.\n"
        "Используйте /help, чтобы увидеть список доступных команд."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК БОТА
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Точка входа: инициализирует БД, регистрирует обработчики
    и запускает бота в режиме polling.
    """
    # Инициализируем базу данных (создаём таблицы, если их нет)
    db.init_db()
    print("--- ЗАПУСК С НОВЫМИ ТАЙМАУТАМИ 30 СЕКУНД ---")

    # ── Настройка HTTP-клиента: таймауты и опциональный прокси ───────────────
    # connect_timeout — время на установку TCP-соединения (сек)
    # read_timeout    — время ожидания ответа от сервера (сек)
    # write_timeout   — время на отправку запроса (сек)
    # pool_timeout    — время ожидания свободного соединения из пула (сек)
    # proxy           — необязательный прокси из .env (None = без прокси)
    #                   Форматы: socks5://host:port  или  http://host:port
    request_config = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    if PROXY_URL:
        logger.info("Используется прокси: %s", PROXY_URL)
    else:
        logger.info("Прокси не задан, прямое подключение к Telegram API.")

    # Строим приложение с поддержкой job_queue (встроенный планировщик).
    # .request()             — клиент для всех Bot API-запросов (отправка сообщений и т.д.)
    # .get_updates_request() — отдельный клиент для long-polling (getUpdates),
    #                          без него таймаут при получении апдейтов остаётся дефолтным
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request_config)
        .get_updates_request(request_config)
        .build()
    )

    # ── Диалог добавления задачи ──────────────────────────────────────────────
    add_conversation = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            WAITING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_title)
            ],
            WAITING_PRIORITY: [
                CallbackQueryHandler(received_priority, pattern=r"^priority:")
            ],
            WAITING_REMINDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_reminder)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        # Позволяем диалогу работать в группах и личных чатах
        per_message=False,
    )

    # ── Регистрация обработчиков ──────────────────────────────────────────────
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("tasks",  cmd_tasks))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(add_conversation)

    # Обработчики Inline-кнопок управления задачами
    app.add_handler(CallbackQueryHandler(callback_done,   pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(callback_delete, pattern=r"^delete:"))

    # Обработчик Inline-кнопок выбора периода отчёта
    app.add_handler(CallbackQueryHandler(callback_report, pattern=r"^report:"))

    # Отвечаем на все прочие текстовые сообщения
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message)
    )

    # ── Восстановление напоминаний после перезапуска ──────────────────────────
    pending = db.get_pending_reminders()
    for task in pending:
        remind_dt = datetime.strptime(task["remind_at"], "%Y-%m-%d %H:%M")
        schedule_reminder(
            app=app,
            task_id=task["id"],
            user_id=task["user_id"],
            title=task["title"],
            remind_at=remind_dt,
        )
    if pending:
        logger.info("Восстановлено %d напоминаний из базы данных.", len(pending))

    # ── Регистрация команд в меню Telegram ────────────────────────────────────
    async def set_commands(app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("start",  "Начало работы"),
            BotCommand("add",    "Добавить задачу"),
            BotCommand("tasks",  "Список активных задач"),
            BotCommand("report", "Отчёт по выполненным задачам"),
            BotCommand("help",   "Справка"),
            BotCommand("cancel", "Отменить текущий диалог"),
        ])

    app.post_init = set_commands

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
