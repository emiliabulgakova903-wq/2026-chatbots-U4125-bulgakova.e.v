"""
equipment.py — вспомогательный модуль для работы с оборудованием.

Лабораторная №2: хранение перенесено в SQLite (см. database.py).
Этот модуль содержит только константы и функции форматирования,
не зависящие от источника данных (CSV или БД).
"""

# Emoji-метки для статусов оборудования
STATUS_EMOJI: dict[str, str] = {
    "В работе":    "🟢",
    "Ремонт":      "🔴",
    "Ожидание":    "🟡",
    "Консервация": "⚫",
}

# Допустимые статусы — используются при добавлении нового оборудования
STATUS_LIST: list[str] = ["В работе", "Ожидание", "Ремонт", "Консервация"]


def format_equipment_item(item, show_maintenance: bool = True) -> str:
    """
    Форматирует одну запись об оборудовании в читаемую строку для Telegram.
    Принимает как словарь, так и sqlite3.Row (оба поддерживают item["field"]).

    :param item:             Запись об оборудовании.
    :param show_maintenance: Показывать ли дату последнего ТО.
    :return:                 Отформатированная строка с Markdown-разметкой.
    """
    status = item["status"] if item["status"] else "—"
    emoji  = STATUS_EMOJI.get(status, "⚪")
    dept   = item["department"] if item["department"] else "—"
    source_badge = " _(польз.)_" if item["source"] == "user" else ""

    text = (
        f"🔧 *{item['name']}*{source_badge} (ID: {item['id']})\n"
        f"   📍 {dept} | {emoji} {status}"
    )
    maintenance = item["last_maintenance"]
    if show_maintenance and maintenance:
        text += f"\n   🗓 Последнее ТО: {maintenance}"
    return text
