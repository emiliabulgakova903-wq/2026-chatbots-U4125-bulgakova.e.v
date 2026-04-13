"""
database.py — модуль для работы с базой данных SQLite.

Лабораторная №2: добавлена таблица equipment для хранения оборудования.
При первом запуске данные автоматически импортируются из equipment.csv.
Пользователи могут добавлять своё оборудование — оно сохраняется в БД.
"""

import csv
import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Путь к базе данных:
# если папка /data существует (сервер Amvera) — используем её,
# иначе — текущая директория (локальный запуск).
DB_PATH = "/data/tasks.db" if os.path.exists("/data") else "tasks.db"


def get_connection() -> sqlite3.Connection:
    """Создаёт и возвращает соединение с базой данных."""
    conn = sqlite3.connect(DB_PATH)
    # Возвращать строки как словари для удобства работы
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Инициализирует базу данных: создаёт все таблицы и выполняет миграции.
    При первом запуске импортирует оборудование из equipment.csv.
    Вызывается один раз при запуске бота.
    """
    with get_connection() as conn:
        # ── Таблица задач ─────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                title        TEXT    NOT NULL,
                priority     TEXT    NOT NULL DEFAULT 'Средний',
                remind_at    TEXT,
                completed    INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL,
                equipment_id TEXT
            )
        """)
        # Миграция для БД из Лаб. №1: добавляем equipment_id, если нет
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN equipment_id TEXT")
            logger.info("Миграция БД: добавлена колонка equipment_id.")
        except Exception:
            pass

        # ── Таблица оборудования (Лаб. №2) ───────────────────────────────────
        # source = 'csv' для данных из файла, 'user' для добавленных пользователем
        conn.execute("""
            CREATE TABLE IF NOT EXISTS equipment (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                department       TEXT    NOT NULL DEFAULT '—',
                status           TEXT    NOT NULL DEFAULT 'В работе',
                last_maintenance TEXT,
                source           TEXT    NOT NULL DEFAULT 'user'
            )
        """)
        conn.commit()

    # Импортируем CSV в БД при первом запуске (если таблица пуста)
    _import_csv_if_empty()
    logger.info("База данных инициализирована.")


def _import_csv_if_empty(csv_path: str = "equipment.csv") -> None:
    """
    Импортирует оборудование из CSV в таблицу equipment, если она пуста.
    Выполняется автоматически при init_db() — только при первом запуске.
    """
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        if count > 0:
            return  # Данные уже есть — пропускаем

    if not os.path.exists(csv_path):
        logger.warning("CSV-файл '%s' не найден, импорт пропущен.", csv_path)
        return

    imported = 0
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        with get_connection() as conn:
            for row in reader:
                if not any(row.values()):
                    continue
                conn.execute(
                    """
                    INSERT INTO equipment (name, department, status, last_maintenance, source)
                    VALUES (?, ?, ?, ?, 'csv')
                    """,
                    (
                        row.get("name", "").strip(),
                        row.get("department", "—").strip(),
                        row.get("status", "В работе").strip(),
                        row.get("last_maintenance", "").strip() or None,
                    ),
                )
                imported += 1
            conn.commit()
    logger.info("Импортировано %d единиц оборудования из %s.", imported, csv_path)


# ══════════════════════════════════════════════════════════════════════════════
#  CRUD ДЛЯ ТАБЛИЦЫ ОБОРУДОВАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

def add_equipment(
    name: str,
    department: str = "—",
    status: str = "В работе",
    last_maintenance: Optional[str] = None,
) -> int:
    """
    Добавляет новую единицу оборудования в базу данных.

    :param name:             Название оборудования.
    :param department:       Отдел / цех.
    :param status:           Статус: 'В работе', 'Ремонт', 'Ожидание', 'Консервация'.
    :param last_maintenance: Дата последнего ТО (строка, необязательно).
    :return:                 ID новой записи.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO equipment (name, department, status, last_maintenance, source)
            VALUES (?, ?, ?, ?, 'user')
            """,
            (name.strip(), department.strip(), status.strip(), last_maintenance),
        )
        conn.commit()
        eq_id = cursor.lastrowid
    logger.info("Оборудование #%d «%s» добавлено пользователем.", eq_id, name)
    return eq_id


def get_all_equipment() -> list[sqlite3.Row]:
    """
    Возвращает весь список оборудования из БД.
    Сначала CSV-записи (source='csv'), затем пользовательские (source='user'),
    внутри каждой группы — по алфавиту.
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM equipment
            ORDER BY
                CASE source WHEN 'csv' THEN 0 ELSE 1 END,
                name COLLATE NOCASE ASC
            """
        ).fetchall()


def get_equipment_by_id(eq_id: int | str) -> Optional[sqlite3.Row]:
    """Возвращает одну запись об оборудовании по ID или None."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM equipment WHERE id = ?", (str(eq_id),)
        ).fetchone()


def search_equipment_db(query: str) -> list[sqlite3.Row]:
    """
    Ищет оборудование в БД по вхождению строки в название или отдел.

    :param query: Поисковый запрос (регистронезависимо).
    :return:      Список совпадающих записей.
    """
    q = f"%{query.strip()}%"
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM equipment
            WHERE name       LIKE ? COLLATE NOCASE
               OR department LIKE ? COLLATE NOCASE
            ORDER BY name COLLATE NOCASE ASC
            """,
            (q, q),
        ).fetchall()


def add_task(
    user_id: int,
    title: str,
    priority: str,
    remind_at: Optional[str] = None,
    equipment_id: Optional[str] = None,
) -> int:
    """
    Добавляет новую задачу в базу данных.

    :param user_id:      Telegram ID пользователя-владельца задачи.
    :param title:        Текст задачи.
    :param priority:     Приоритет: 'Низкий', 'Средний' или 'Высокий'.
    :param remind_at:    Дата/время напоминания в формате 'YYYY-MM-DD HH:MM' (или None).
    :param equipment_id: ID оборудования из equipment.csv (или None).
    :return:             ID только что созданной записи.
    """
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks
                (user_id, title, priority, remind_at, completed, created_at, equipment_id)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (user_id, title, priority, remind_at, created_at, equipment_id),
        )
        conn.commit()
        task_id = cursor.lastrowid
    logger.info(
        "Задача #%d добавлена для пользователя %d (оборудование: %s).",
        task_id, user_id, equipment_id or "не привязано",
    )
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
