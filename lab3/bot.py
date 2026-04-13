"""
bot.py — основной модуль Telegram-бота «Tracker Production Improvement Service».

Лабораторная №2: интеграция с оборудованием.
  Новое в этой версии:
  • /equipment       — список оборудования из БД (CSV + пользовательские)
  • /search          — поиск оборудования по названию или отделу
  • /addequipment    — добавить новое оборудование в справочник
  • /add             — при создании задачи можно выбрать оборудование ИЛИ
                       прямо из диалога добавить новое

  Сохранён весь функционал Лаб. №1: приоритеты, напоминания, /tasks, /report.
"""

import csv
import io
import logging
import os
import sys
from datetime import datetime

# Гарантируем, что папка lab3/ есть в sys.path при любом способе запуска
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import database as db
import equipment as eq

# ── Переменные окружения ──────────────────────────────────────────────────────
# Загружаем .env, если он есть (для локального запуска)
load_dotenv()

# Сначала проверяем переменные окружения (Amvera), затем .env
token = os.environ.get("BOT_TOKEN")
BOT_TOKEN: str = token or ""
PROXY_URL: str | None = os.getenv("PROXY_URL") or None

if not BOT_TOKEN:
    raise ValueError("Токен бота не найден. Укажите BOT_TOKEN в настройках Amvera или в .env")

# ── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Состояния диалога /add (4 основных + 2 для ввода нового оборудования) ────
WAITING_TITLE        = 0
WAITING_EQUIPMENT    = 1
WAITING_NEW_EQ_NAME  = 4   # пользователь вводит название нового оборудования
WAITING_NEW_EQ_DEPT  = 5   # пользователь вводит отдел нового оборудования
WAITING_PRIORITY     = 2
WAITING_REMINDER     = 3

# ── Состояния отдельного диалога /addequipment ────────────────────────────────
AEQ_NAME   = 10
AEQ_DEPT   = 11
AEQ_STATUS = 12

# ── Прочие константы ─────────────────────────────────────────────────────────
PRIORITY_EMOJI        = {"Высокий": "🔴", "Средний": "🟡", "Низкий": "🟢"}
REPORT_FILE_THRESHOLD = 10


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ — клавиатуры
# ══════════════════════════════════════════════════════════════════════════════

def build_tasks_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("🗑 Удалить",   callback_data=f"delete:{task_id}"),
    ]])


def build_priority_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 Высокий", callback_data="priority:Высокий"),
        InlineKeyboardButton("🟡 Средний", callback_data="priority:Средний"),
        InlineKeyboardButton("🟢 Низкий",  callback_data="priority:Низкий"),
    ]])


def build_status_keyboard(prefix: str = "aeq_status") -> InlineKeyboardMarkup:
    """Клавиатура выбора статуса оборудования."""
    buttons = []
    for status in eq.STATUS_LIST:
        emoji = eq.STATUS_EMOJI.get(status, "⚪")
        buttons.append([InlineKeyboardButton(
            f"{emoji} {status}", callback_data=f"{prefix}:{status}"
        )])
    return InlineKeyboardMarkup(buttons)


def build_equipment_keyboard() -> InlineKeyboardMarkup:
    """
    Строит клавиатуру со списком оборудования из БД.
    Добавляет кнопки «➕ Новое оборудование» и «⏭ Пропустить».
    """
    buttons: list[list[InlineKeyboardButton]] = []
    items = db.get_all_equipment()
    for item in items:
        emoji = eq.STATUS_EMOJI.get(item["status"], "⚪")
        label = f"{emoji} {item['name']} (ID:{item['id']})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"equip:{item['id']}")])

    # Кнопка добавления нового оборудования прямо из диалога
    buttons.append([InlineKeyboardButton("➕ Новое оборудование", callback_data="equip:new")])
    # Кнопка пропуска — задача без привязки
    buttons.append([InlineKeyboardButton("⏭ Пропустить",          callback_data="equip:skip")])
    return InlineKeyboardMarkup(buttons)


def build_report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 7 дней",        callback_data="report:7"),
            InlineKeyboardButton("📅 30 дней",        callback_data="report:30"),
        ],
        [
            InlineKeyboardButton("📅 Текущий месяц", callback_data="report:month"),
            InlineKeyboardButton("📋 Всё время",      callback_data="report:0"),
        ],
    ])


# ══════════════════════════════════════════════════════════════════════════════
#  НАПОМИНАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

def schedule_reminder(app, task_id, user_id, title, remind_at) -> None:
    delay = max(1.0, (remind_at - datetime.now()).total_seconds())
    app.job_queue.run_once(
        callback=send_reminder,
        when=delay,
        data={"task_id": task_id, "user_id": user_id, "title": title},
        name=f"reminder_{task_id}",
    )
    logger.info("Напоминание #%d → %s (через %.0f сек.)", task_id, remind_at.strftime("%H:%M"), delay)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        task = db.get_task_by_id(data["task_id"])
        if task and not task["completed"]:
            await context.bot.send_message(
                chat_id=data["user_id"],
                text=f"⏰ *Напоминание!*\n\nЗадача: *{data['title']}*\nНе забудьте выполнить её!",
                parse_mode="Markdown",
                reply_markup=build_tasks_keyboard(data["task_id"]),
            )
    except Exception:
        logger.exception("Ошибка при отправке напоминания #%d.", data["task_id"])


# ══════════════════════════════════════════════════════════════════════════════
#  ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
# ══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует все необработанные исключения и уведомляет пользователя."""
    logger.error("Исключение при обработке обновления:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Произошла внутренняя ошибка. Попробуйте ещё раз или напишите /start."
        )


# ══════════════════════════════════════════════════════════════════════════════
#  БАЗОВЫЕ КОМАНДЫ
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        "Я — *Tracker Production Improvement Service Bot*.\n\n"
        "📌 *Команды:*\n"
        "• /add — добавить задачу\n"
        "• /tasks — список активных задач\n"
        "• /equipment — список оборудования\n"
        "• /addequipment — добавить новое оборудование\n"
        "• /search — поиск оборудования\n"
        "• /report — отчёт по выполненным задачам\n"
        "• /help — справка",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Справка*\n\n"
        "*Задачи:*\n"
        "• /add — добавить задачу (название → оборудование → приоритет → напоминание)\n"
        "• /tasks — активные задачи\n"
        "• /report — отчёт по выполненным\n\n"
        "*Оборудование:*\n"
        "• /equipment — полный список\n"
        "• /addequipment — добавить новый агрегат в справочник\n"
        "• /search `текст` — поиск по названию или отделу\n\n"
        "*В диалоге /add:*\n"
        "Выберите оборудование из списка, нажмите\n"
        "«➕ Новое оборудование» чтобы добавить прямо сейчас,\n"
        "или «⏭ Пропустить» если привязка не нужна.\n\n"
        "*Статусы:* 🟢 В работе · 🔴 Ремонт · 🟡 Ожидание · ⚫ Консервация\n"
        "*Приоритеты:* 🔴 Высокий · 🟡 Средний · 🟢 Низкий",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /tasks
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    tasks   = db.get_tasks(user_id, only_active=True)

    if not tasks:
        await update.message.reply_text("📭 Активных задач нет. Добавьте командой /add!")
        return

    await update.message.reply_text(f"📋 *Активные задачи* ({len(tasks)} шт.):", parse_mode="Markdown")

    for task in tasks:
        emoji    = PRIORITY_EMOJI.get(task["priority"], "⚪")
        reminder = f"\n⏰ {task['remind_at']}" if task["remind_at"] else ""
        eq_line  = ""
        if task["equipment_id"]:
            item = db.get_equipment_by_id(task["equipment_id"])
            if item:
                eq_line = f"\n🔧 {item['name']} {eq.STATUS_EMOJI.get(item['status'], '⚪')} {item['status']}"
            else:
                eq_line = f"\n🔧 ID:{task['equipment_id']}"

        await update.message.reply_text(
            f"{emoji} *{task['title']}*\n"
            f"Приоритет: {task['priority']}{eq_line}{reminder}\n"
            f"Добавлено: {task['created_at']}",
            parse_mode="Markdown",
            reply_markup=build_tasks_keyboard(task["id"]),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  /equipment — список оборудования из БД
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_equipment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = db.get_all_equipment()
    if not items:
        await update.message.reply_text(
            "📭 Справочник оборудования пуст.\n"
            "Добавьте первый агрегат командой /addequipment."
        )
        return

    lines = [f"🏭 *Список оборудования* ({len(items)} ед.):\n"]
    for item in items:
        lines.append(eq.format_equipment_item(item))

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  /search — поиск оборудования
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "🔍 Укажите запрос: `/search Трансформатор`",
            parse_mode="Markdown",
        )
        return

    results = db.search_equipment_db(query)
    if not results:
        await update.message.reply_text(f"🔍 По запросу *«{query}»* ничего не найдено.", parse_mode="Markdown")
        return

    lines = [f"🔍 *«{query}»* — {len(results)} рез.:\n"]
    for item in results:
        lines.append(eq.format_equipment_item(item))
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  /addequipment — отдельный диалог добавления оборудования в справочник
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_addequipment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 1/3 — запрашивает название нового оборудования."""
    await update.message.reply_text(
        "🔧 *Добавление нового оборудования*\n\n"
        "Введите название (например: `Насос НЦ-9`) или /cancel для отмены:",
        parse_mode="Markdown",
    )
    return AEQ_NAME


async def aeq_received_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2/3 — получает название, запрашивает отдел."""
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("⚠️ Слишком короткое название. Попробуйте ещё раз:")
        return AEQ_NAME

    context.user_data["aeq_name"] = name
    await update.message.reply_text(
        f"🔧 Название: *{name}*\n\n"
        "Введите отдел / цех (например: `Цех №5`)\n"
        "Или «-» чтобы оставить пустым:",
        parse_mode="Markdown",
    )
    return AEQ_DEPT


async def aeq_received_dept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3/3 — получает отдел, предлагает выбрать статус."""
    dept = update.message.text.strip()
    context.user_data["aeq_dept"] = "—" if dept == "-" else dept

    name = context.user_data["aeq_name"]
    await update.message.reply_text(
        f"🔧 *{name}*\n📍 {context.user_data['aeq_dept']}\n\n"
        "Выберите текущий статус:",
        parse_mode="Markdown",
        reply_markup=build_status_keyboard("aeq_status"),
    )
    return AEQ_STATUS


async def aeq_received_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Финал — сохраняет оборудование в БД."""
    query  = update.callback_query
    await query.answer()

    status = query.data.split(":")[1]
    name   = context.user_data["aeq_name"]
    dept   = context.user_data["aeq_dept"]

    eq_id  = db.add_equipment(name=name, department=dept, status=status)
    emoji  = eq.STATUS_EMOJI.get(status, "⚪")

    await query.edit_message_text(
        f"✅ *Оборудование добавлено в справочник!*\n\n"
        f"🔧 *{name}* (ID: {eq_id})\n"
        f"📍 {dept} | {emoji} {status}\n\n"
        "Теперь вы можете привязать его к задаче через /add.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def aeq_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Добавление оборудования отменено.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  /add — мастер добавления ЗАДАЧИ (с выбором/добавлением оборудования)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 1 — запрашивает название задачи."""
    await update.message.reply_text(
        "✏️ *Добавление задачи*\n\nВведите название или /cancel для отмены:",
        parse_mode="Markdown",
    )
    return WAITING_TITLE


async def received_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2 — сохраняет название, показывает список оборудования."""
    title = update.message.text.strip()
    if len(title) < 3:
        await update.message.reply_text("⚠️ Минимум 3 символа. Попробуйте ещё раз:")
        return WAITING_TITLE

    context.user_data["new_task_title"] = title
    await update.message.reply_text(
        f"📝 Задача: *{title}*\n\n"
        "🔧 Выберите оборудование из справочника,\n"
        "нажмите «➕ Новое» чтобы добавить прямо сейчас,\n"
        "или «⏭ Пропустить»:",
        parse_mode="Markdown",
        reply_markup=build_equipment_keyboard(),
    )
    return WAITING_EQUIPMENT


async def received_equipment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3 — обрабатывает выбор оборудования."""
    query    = update.callback_query
    await query.answer()
    eq_value = query.data.split(":")[1]

    if eq_value == "new":
        # Пользователь хочет добавить новое оборудование прямо в диалоге
        await query.edit_message_text(
            "🔧 *Добавление нового оборудования*\n\n"
            "Введите название агрегата (например: `Насос НЦ-9`):",
            parse_mode="Markdown",
        )
        return WAITING_NEW_EQ_NAME

    if eq_value == "skip":
        context.user_data["new_task_equipment_id"] = None
        eq_label = "без привязки"
    else:
        context.user_data["new_task_equipment_id"] = eq_value
        item     = db.get_equipment_by_id(eq_value)
        eq_label = item["name"] if item else f"ID:{eq_value}"

    return await _ask_priority(query, context, eq_label)


async def received_new_eq_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3а — получает название нового оборудования, спрашивает отдел."""
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("⚠️ Слишком короткое. Попробуйте ещё раз:")
        return WAITING_NEW_EQ_NAME

    context.user_data["new_eq_name"] = name
    await update.message.reply_text(
        f"📍 Введите отдел / цех для *{name}*\n"
        "Или «-» чтобы оставить пустым:",
        parse_mode="Markdown",
    )
    return WAITING_NEW_EQ_DEPT


async def received_new_eq_dept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3б — сохраняет новое оборудование (статус 'В работе') и продолжает."""
    dept = update.message.text.strip()
    dept = "—" if dept == "-" else dept

    name  = context.user_data.pop("new_eq_name")
    eq_id = db.add_equipment(name=name, department=dept, status="В работе")

    context.user_data["new_task_equipment_id"] = str(eq_id)

    await update.message.reply_text(
        f"✅ Оборудование *{name}* (ID:{eq_id}) добавлено в справочник!\n\n"
        "Выберите приоритет задачи:",
        parse_mode="Markdown",
        reply_markup=build_priority_keyboard(),
    )
    return WAITING_PRIORITY


async def _ask_priority(query, context, eq_label: str) -> int:
    """Вспомогательная функция — показывает выбор приоритета."""
    title = context.user_data["new_task_title"]
    await query.edit_message_text(
        f"📝 Задача: *{title}*\n"
        f"🔧 Оборудование: {eq_label}\n\n"
        "Выберите приоритет:",
        parse_mode="Markdown",
        reply_markup=build_priority_keyboard(),
    )
    return WAITING_PRIORITY


async def received_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 4 — сохраняет приоритет, запрашивает напоминание."""
    query    = update.callback_query
    await query.answer()

    priority = query.data.split(":")[1]
    context.user_data["new_task_priority"] = priority

    title  = context.user_data["new_task_title"]
    eq_id  = context.user_data.get("new_task_equipment_id")
    emoji  = PRIORITY_EMOJI.get(priority, "⚪")
    eq_line = ""
    if eq_id:
        item    = db.get_equipment_by_id(eq_id)
        eq_line = f"\n🔧 {item['name']}" if item else f"\n🔧 ID:{eq_id}"

    await query.edit_message_text(
        f"📝 *{title}*{eq_line}\n"
        f"Приоритет: {emoji} {priority}\n\n"
        "⏰ Дата и время напоминания `ДД.ММ.ГГГГ ЧЧ:ММ`\n"
        "Или «-» чтобы пропустить:",
        parse_mode="Markdown",
    )
    return WAITING_REMINDER


async def received_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 5 — сохраняет задачу в БД."""
    text     = update.message.text.strip().lower()
    user_id  = update.effective_user.id
    title    = context.user_data["new_task_title"]
    priority = context.user_data["new_task_priority"]
    eq_id    = context.user_data.get("new_task_equipment_id")

    remind_at_str = None
    remind_dt     = None

    if text not in ("-", "нет", "no", "skip"):
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                remind_dt     = datetime.strptime(update.message.text.strip(), fmt)
                remind_at_str = remind_dt.strftime("%Y-%m-%d %H:%M")
                break
            except ValueError:
                continue
        if not remind_at_str:
            await update.message.reply_text(
                "❌ Неверный формат. Используйте `ДД.ММ.ГГГГ ЧЧ:ММ` или введите «-».",
                parse_mode="Markdown",
            )
            return WAITING_REMINDER
        if remind_dt and remind_dt <= datetime.now():
            await update.message.reply_text(
                "❌ Дата уже прошла. Укажите будущую дату или введите «-»."
            )
            return WAITING_REMINDER

    task_id = db.add_task(
        user_id=user_id, title=title, priority=priority,
        remind_at=remind_at_str, equipment_id=eq_id,
    )

    if remind_dt:
        schedule_reminder(context.application, task_id, user_id, title, remind_dt)
        reminder_text = f"\n⏰ Напомню: {update.message.text.strip()}"
    else:
        reminder_text = ""

    eq_confirm = ""
    if eq_id:
        item = db.get_equipment_by_id(eq_id)
        eq_confirm = f"\n🔧 {item['name']}" if item else f"\n🔧 ID:{eq_id}"

    await update.message.reply_text(
        f"✅ *Задача добавлена!*\n\n"
        f"📝 {title}{eq_confirm}\n"
        f"Приоритет: {PRIORITY_EMOJI.get(priority, '⚪')} {priority}{reminder_text}\n\n"
        "Все задачи: /tasks",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Начните снова командой /add.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  INLINE-КНОПКИ ЗАДАЧ
# ══════════════════════════════════════════════════════════════════════════════

async def callback_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    task_id = int(query.data.split(":")[1])
    task    = db.get_task_by_id(task_id)
    if not task or task["user_id"] != update.effective_user.id:
        await query.answer("⚠️ Задача не найдена.", show_alert=True); return
    if task["completed"]:
        await query.answer("Уже выполнена.", show_alert=True); return
    db.complete_task(task_id)
    await query.edit_message_text(
        f"✅ *Выполнено:* {task['title']}\n"
        f"Приоритет: {PRIORITY_EMOJI.get(task['priority'], '⚪')} {task['priority']}",
        parse_mode="Markdown",
    )


async def callback_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    task_id = int(query.data.split(":")[1])
    task    = db.get_task_by_id(task_id)
    if not task or task["user_id"] != update.effective_user.id:
        await query.answer("⚠️ Задача не найдена.", show_alert=True); return
    db.delete_task(task_id)
    await query.edit_message_text(f"🗑 *Задача удалена:* {task['title']}", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  /report
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 *Отчёт по выполненным задачам*\n\nВыберите период:",
        parse_mode="Markdown",
        reply_markup=build_report_keyboard(),
    )


async def callback_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    period  = query.data.split(":")[1]

    if period == "month":
        today = datetime.now()
        days, label = today.day, f"текущий месяц ({today.strftime('%B %Y')})"
    elif period == "0":
        days, label = 0, "всё время"
    else:
        days, label = int(period), f"последние {period} дн."

    tasks = db.get_completed_tasks(user_id, days)
    if not tasks:
        await query.edit_message_text(f"📭 За *«{label}»* выполненных задач нет.", parse_mode="Markdown")
        return

    lines = [f"📊 *Отчёт: {label}* — {len(tasks)} задач\n"]
    for task in tasks:
        emoji = PRIORITY_EMOJI.get(task["priority"], "⚪")
        lines.append(f"📅 {task['created_at']}  |  ✅ {task['title']}  |  {emoji} {task['priority']}")

    if len(tasks) <= REPORT_FILE_THRESHOLD:
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return

    await query.edit_message_text(f"📊 Найдено *{len(tasks)}* задач. Формирую CSV…", parse_mode="Markdown")

    buf = io.StringIO()
    w   = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Задача", "Приоритет", "Оборудование", "Дата"])
    for task in tasks:
        eq_name = ""
        if task["equipment_id"]:
            item    = db.get_equipment_by_id(task["equipment_id"])
            eq_name = item["name"] if item else f"ID:{task['equipment_id']}"
        w.writerow([task["id"], task["title"], task["priority"], eq_name, task["created_at"]])

    csv_bytes      = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    csv_bytes.name = f"report_{period}.csv"
    await context.bot.send_document(
        chat_id=user_id,
        document=csv_bytes,
        filename=f"report_{label.replace(' ', '_')}.csv",
        caption=f"📎 Отчёт за *{label}*: {len(tasks)} задач.",
        parse_mode="Markdown",
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🤔 Не понял. Используйте /help.")


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    db.init_db()

    request_config = HTTPXRequest(
        connect_timeout=30.0, read_timeout=30.0,
        write_timeout=30.0,   pool_timeout=30.0,
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request_config)
        .get_updates_request(request_config)
        .build()
    )

    # ── Диалог /add ───────────────────────────────────────────────────────────
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            WAITING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_title)
            ],
            WAITING_EQUIPMENT: [
                CallbackQueryHandler(received_equipment, pattern=r"^equip:")
            ],
            WAITING_NEW_EQ_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_new_eq_name)
            ],
            WAITING_NEW_EQ_DEPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_new_eq_dept)
            ],
            WAITING_PRIORITY: [
                CallbackQueryHandler(received_priority, pattern=r"^priority:")
            ],
            WAITING_REMINDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_reminder)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )

    # ── Диалог /addequipment ──────────────────────────────────────────────────
    addequip_conv = ConversationHandler(
        entry_points=[CommandHandler("addequipment", cmd_addequipment)],
        states={
            AEQ_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, aeq_received_name)],
            AEQ_DEPT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, aeq_received_dept)],
            AEQ_STATUS: [CallbackQueryHandler(aeq_received_status, pattern=r"^aeq_status:")],
        },
        fallbacks=[CommandHandler("cancel", aeq_cancel)],
        per_message=False,
    )

    # ── Диагностика: логируем ВСЕ входящие обновления ────────────────────────
    async def _log_update(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(">>> UPDATE RECEIVED: %s", update)

    app.add_handler(TypeHandler(object, _log_update), group=-1)

    # ── Регистрация ───────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("tasks",     cmd_tasks))
    app.add_handler(CommandHandler("report",    cmd_report))
    app.add_handler(CommandHandler("equipment", cmd_equipment))
    app.add_handler(CommandHandler("search",    cmd_search))
    app.add_handler(add_conv)
    app.add_handler(addequip_conv)
    app.add_handler(CallbackQueryHandler(callback_done,   pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(callback_delete, pattern=r"^delete:"))
    app.add_handler(CallbackQueryHandler(callback_report, pattern=r"^report:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    app.add_error_handler(error_handler)

    # ── Восстановление напоминаний ────────────────────────────────────────────
    for task in db.get_pending_reminders():
        schedule_reminder(
            app, task["id"], task["user_id"], task["title"],
            datetime.strptime(task["remind_at"], "%Y-%m-%d %H:%M"),
        )

    # ── Меню команд ───────────────────────────────────────────────────────────
    async def set_commands(app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("start",        "Начало работы"),
            BotCommand("add",          "Добавить задачу"),
            BotCommand("tasks",        "Список активных задач"),
            BotCommand("equipment",    "Список оборудования"),
            BotCommand("addequipment", "Добавить оборудование"),
            BotCommand("search",       "Поиск оборудования"),
            BotCommand("report",       "Отчёт по выполненным задачам"),
            BotCommand("help",         "Справка"),
            BotCommand("cancel",       "Отменить диалог"),
        ])

    app.post_init = set_commands

    logger.info("Бот запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
