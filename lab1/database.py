"""
database.py — модуль для работы с базой данных SQLite.
Содержит все операции создания, чтения, обновления и удаления задач (CRUD).
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Путь к файлу базы данных
DB_PATH = "tasks.db"


def get_connection() -> sqlite3.Connection:
    """Создаёт и возвращает соединение с базой данных."""
    conn = sqlite3.connect(DB_PATH)
    # Возвращать строки как словари для удобства работы
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Инициализирует базу данных: создаёт таблицу задач, если она ещё не существует.
    Вызывается один раз при запуске бота.
    """
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                title       TEXT    NOT NULL,
                priority    TEXT    NOT NULL DEFAULT 'Средний',
                remind_at   TEXT,
                completed   INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.commit()
    logger.info("База данных инициализирована.")


def add_task(
    user_id: int,
    title: str,
    priority: str,
    remind_at: Optional[str] = None,
) -> int:
    """
    Добавляет новую задачу в базу данных.

    :param user_id:   Telegram ID пользователя-владельца задачи.
    :param title:     Текст задачи.
    :param priority:  Приоритет: 'Низкий', 'Средний' или 'Высокий'.
    :param remind_at: Дата/время напоминания в формате 'YYYY-MM-DD HH:MM' (или None).
    :return:          ID только что созданной записи.
    """
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks (user_id, title, priority, remind_at, completed, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (user_id, title, priority, remind_at, created_at),
        )
        conn.commit()
        task_id = cursor.lastrowid
    logger.info("Задача #%d добавлена для пользователя %d.", task_id, user_id)
    return task_id


def get_tasks(user_id: int, only_active: bool = True) -> list[sqlite3.Row]:
    """
    Возвращает список задач для указанного пользователя.

    :param user_id:     Telegram ID пользователя.
    :param only_active: Если True — только незавершённые задачи.
    :return:            Список строк-словарей из таблицы tasks.
    """
    query = "SELECT * FROM tasks WHERE user_id = ?"
    params: list = [user_id]

    if only_active:
        query += " AND completed = 0"

    # Сортировка: сначала Высокий приоритет, затем Средний, затем Низкий
    query += """
        ORDER BY
            CASE priority
                WHEN 'Высокий' THEN 1
                WHEN 'Средний' THEN 2
                WHEN 'Низкий'  THEN 3
                ELSE 4
            END,
            created_at ASC
    """

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return rows


def get_task_by_id(task_id: int) -> Optional[sqlite3.Row]:
    """Возвращает одну задачу по её ID или None, если задача не найдена."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return row


def complete_task(task_id: int) -> bool:
    """
    Помечает задачу как выполненную.

    :param task_id: ID задачи.
    :return:        True — если запись обновлена, False — если задача не найдена.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET completed = 1 WHERE id = ?", (task_id,)
        )
        conn.commit()
    updated = cursor.rowcount > 0
    if updated:
        logger.info("Задача #%d помечена как выполненная.", task_id)
    return updated


def delete_task(task_id: int) -> bool:
    """
    Удаляет задачу из базы данных.

    :param task_id: ID задачи.
    :return:        True — если запись удалена, False — если задача не найдена.
    """
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
    deleted = cursor.rowcount > 0
    if deleted:
        logger.info("Задача #%d удалена.", task_id)
    return deleted


def get_completed_tasks(user_id: int, days: int) -> list[sqlite3.Row]:
    """
    Возвращает выполненные задачи пользователя за указанный период.

    :param user_id: Telegram ID пользователя.
    :param days:    Глубина выборки в днях. 0 — возвращает все выполненные задачи.
    :return:        Список задач, отсортированных по дате создания (новые первыми).
    """
    if days > 0:
        # Вычисляем нижнюю границу периода
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        rows = get_connection().execute(
            """
            SELECT * FROM tasks
            WHERE user_id   = ?
              AND completed = 1
              AND created_at >= ?
            ORDER BY created_at DESC
            """,
            (user_id, since),
        ).fetchall()
    else:
        # days == 0 означает «за всё время»
        rows = get_connection().execute(
            """
            SELECT * FROM tasks
            WHERE user_id   = ?
              AND completed = 1
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()

    logger.info(
        "Запрос отчёта: пользователь %d, период %d дн., найдено %d задач.",
        user_id, days, len(rows),
    )
    return rows


def get_pending_reminders() -> list[sqlite3.Row]:
    """
    Возвращает все незавершённые задачи, у которых задана дата напоминания.
    Используется планировщиком для регистрации напоминаний при перезапуске бота.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE completed = 0
              AND remind_at IS NOT NULL
              AND remind_at > ?
            """,
            (datetime.now().strftime("%Y-%m-%d %H:%M"),),
        ).fetchall()
    return rows
