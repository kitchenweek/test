import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, UpdateType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===================== НАСТРОЙКА =====================
BOT_TOKEN = "8622609872:AAH2rMoJZ-D7xggf1tS217ZxVjOv-RHr8Ks"
OWNER_ID = 7517164478  # Telegram ID владельца бизнес-аккаунта

DB_PATH = Path("clients.db")
INACTIVE_AFTER_HOURS = 6
DELETE_AFTER_DAYS = 14
CHECK_INTERVAL_SECONDS = 30
STAGE_REMINDER_DAYS = (3, 6, 13)
PAYMENT_REMINDER_MIN_SECONDS = 3 * 60
PAYMENT_REMINDER_MAX_SECONDS = 6 * 60
# =====================================================

STAGES = [
    "Доставка",
    "СВ",
    "Залог",
    "Перерасчёт СВ",
    "Перерасчёт залога",
    "Лот по СВ",
    "Лот по залогу",
]

STAGE_PHRASES = {
    0: ["добрый день, ваш заказ прибыл к нам на склад в мск"],
    1: [
        "сумма полностью возвратная т.е при уведомлении сдэка/почты о получении товара клиентом сумма будет возвращена в полном объеме на номер карты (имя получателя и банк должен быть тот же, с которого была отправлена сумма)",
        "на разных складах товары, сумма за св та же",
    ],
    2: [
        "18-19мск, также не получили реквизиты на возврат св (имя отправителя как в чеке и тот же банк)",
        "18-19мск, возврат по реквизитам",
    ],
    3: ["перерасчет по св (отмена категории заказов до 100тыс₽), св на все заказы теперь"],
    4: ["перерасчет по залогу (отмена категории заказов до 100тыс₽), залог на все заказы теперь"],
    5: ["клиент оплатил залоги и св на одну отправку, лот по которой уже закрыт, сейчас тк ждет"],
    6: ["сумма к оплате на новый лот по залогу"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("business_bot")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def str_to_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def normalize_text(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def detect_stage(text: str) -> Optional[int]:
    normalized = normalize_text(text)
    found = [
        stage
        for stage, phrases in STAGE_PHRASES.items()
        if any(normalize_text(phrase) in normalized for phrase in phrases)
    ]
    return max(found) if found else None


def detect_client_payment_trigger(message: Message) -> Optional[str]:
    """Клиентский триггер: только PDF-файл во входящем сообщении."""
    document = message.document
    if not document:
        return None

    filename = (document.file_name or "").casefold()
    mime_type = (document.mime_type or "").casefold()
    if filename.endswith(".pdf") or mime_type == "application/pdf":
        return "PDF-файл от клиента"
    return None


def detect_owner_payment_trigger(message: Message) -> Optional[str]:
    """Триггеры владельца: «Принято» или 👌 в исходящем сообщении."""
    text = message.text or message.caption or ""
    normalized = normalize_text(text)

    if "👌" in text:
        return "👌 от владельца"
    if "принято" in normalized:
        return "Принято от владельца"
    return None


class ClientRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    stage INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'inactive')),
                    last_active TEXT NOT NULL,
                    hidden_at TEXT,
                    updated_at TEXT NOT NULL,
                    stage_updated_at TEXT,
                    reminder_3_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_6_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_13_sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            # Миграция базы, созданной предыдущей версией бота.
            columns = self._columns(db, "clients")
            additions = {
                "stage_updated_at": "TEXT",
                "reminder_3_sent": "INTEGER NOT NULL DEFAULT 0",
                "reminder_6_sent": "INTEGER NOT NULL DEFAULT 0",
                "reminder_13_sent": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE clients ADD COLUMN {name} {definition}")

            db.execute(
                """
                UPDATE clients
                SET stage_updated_at = COALESCE(stage_updated_at, updated_at, last_active)
                WHERE stage_updated_at IS NULL
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_reminders (
                    user_id INTEGER PRIMARY KEY,
                    trigger_type TEXT NOT NULL,
                    trigger_message_id INTEGER,
                    baseline_stage INTEGER,
                    due_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES clients(user_id) ON DELETE CASCADE
                )
                """
            )

    def ensure_client(
        self,
        user_id: int,
        username: Optional[str],
        first_name: str,
    ) -> sqlite3.Row:
        now_s = dt_to_str(utc_now())
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM clients WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    """
                    INSERT INTO clients
                    (user_id, username, first_name, stage, status, last_active,
                     hidden_at, updated_at, stage_updated_at)
                    VALUES (?, ?, ?, 0, 'active', ?, NULL, ?, ?)
                    """,
                    (user_id, username, first_name, now_s, now_s, now_s),
                )
            else:
                db.execute(
                    """
                    UPDATE clients
                    SET username = ?, first_name = ?, last_active = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (username, first_name, now_s, now_s, user_id),
                )
            return db.execute(
                "SELECT * FROM clients WHERE user_id = ?", (user_id,)
            ).fetchone()

    def upsert_stage(
        self,
        user_id: int,
        username: Optional[str],
        first_name: str,
        detected_stage: int,
    ) -> tuple[int, bool]:
        now_s = dt_to_str(utc_now())
        with self.connect() as db:
            row = db.execute(
                "SELECT stage FROM clients WHERE user_id = ?", (user_id,)
            ).fetchone()

            if row is None:
                new_stage = detected_stage
                changed = True
                db.execute(
                    """
                    INSERT INTO clients
                    (user_id, username, first_name, stage, status, last_active,
                     hidden_at, updated_at, stage_updated_at,
                     reminder_3_sent, reminder_6_sent, reminder_13_sent)
                    VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, 0, 0, 0)
                    """,
                    (user_id, username, first_name, new_stage, now_s, now_s, now_s),
                )
            else:
                old_stage = int(row["stage"])
                new_stage = max(old_stage, detected_stage)
                changed = new_stage > old_stage

                # Любая найденная ключевая фраза считается обновлением этапа,
                # даже если этот же этап уже был установлен ранее.
                db.execute(
                    """
                    UPDATE clients
                    SET username = ?, first_name = ?, stage = ?, status = 'active',
                        last_active = ?, hidden_at = NULL, updated_at = ?,
                        stage_updated_at = ?, reminder_3_sent = 0,
                        reminder_6_sent = 0, reminder_13_sent = 0
                    WHERE user_id = ?
                    """,
                    (username, first_name, new_stage, now_s, now_s, now_s, user_id),
                )
                # Ключевая фраза означает, что новый платёж уже выдан.
                # Поэтому отменяем ожидающее платёжное напоминание и при
                # повышении этапа, и при повторной фразе того же этапа.
                db.execute(
                    "DELETE FROM payment_reminders WHERE user_id = ?", (user_id,)
                )

        return new_stage, changed

    def schedule_payment_reminder(
        self,
        user_id: int,
        username: Optional[str],
        first_name: str,
        trigger_type: str,
        trigger_message_id: int,
        due_at: datetime,
    ) -> bool:
        client = self.ensure_client(user_id, username, first_name)
        with self.connect() as db:
            existing = db.execute(
                "SELECT 1 FROM payment_reminders WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing:
                return False
            db.execute(
                """
                INSERT INTO payment_reminders
                (user_id, trigger_type, trigger_message_id, baseline_stage, due_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    trigger_type,
                    trigger_message_id,
                    int(client["stage"]),
                    dt_to_str(due_at),
                    dt_to_str(utc_now()),
                ),
            )
            return True

    def due_payment_reminders(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """
                SELECT p.*, c.username, c.first_name, c.stage
                FROM payment_reminders p
                JOIN clients c ON c.user_id = p.user_id
                WHERE p.due_at <= ?
                ORDER BY p.due_at
                """,
                (dt_to_str(now),),
            ).fetchall()

    def delete_payment_reminder(self, user_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM payment_reminders WHERE user_id = ?", (user_id,))

    def due_stage_reminders(self, now: datetime) -> list[tuple[sqlite3.Row, int]]:
        result: list[tuple[sqlite3.Row, int]] = []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM clients").fetchall()
            for row in rows:
                stage_updated_at = str_to_dt(row["stage_updated_at"])
                if not stage_updated_at:
                    continue
                elapsed = now - stage_updated_at
                for day in STAGE_REMINDER_DAYS:
                    column = f"reminder_{day}_sent"
                    if elapsed >= timedelta(days=day) and not int(row[column]):
                        result.append((row, day))
        return result

    def mark_stage_reminder_sent(self, user_id: int, day: int) -> None:
        if day not in STAGE_REMINDER_DAYS:
            raise ValueError("Недопустимый срок напоминания")
        with self.connect() as db:
            db.execute(
                f"UPDATE clients SET reminder_{day}_sent = 1 WHERE user_id = ?",
                (user_id,),
            )

    def set_status(self, user_id: int, status: str) -> bool:
        now_s = dt_to_str(utc_now())
        hidden_at = now_s if status == "inactive" else None
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE clients
                SET status = ?, hidden_at = ?,
                    last_active = CASE WHEN ? = 'active' THEN ? ELSE last_active END,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (status, hidden_at, status, now_s, now_s, user_id),
            )
            return cursor.rowcount > 0

    def get(self, user_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM clients WHERE user_id = ?", (user_id,)
            ).fetchone()

    def list_by_status(self, status: str) -> list[sqlite3.Row]:
        order = "last_active DESC" if status == "active" else "hidden_at DESC"
        with self.connect() as db:
            return db.execute(
                f"SELECT * FROM clients WHERE status = ? ORDER BY {order}",
                (status,),
            ).fetchall()

    def auto_cleanup(self) -> tuple[int, int]:
        now = utc_now()
        inactive_before = now - timedelta(hours=INACTIVE_AFTER_HOURS)
        delete_before = now - timedelta(days=DELETE_AFTER_DAYS)
        hidden_count = 0
        deleted_count = 0

        with self.connect() as db:
            rows = db.execute(
                "SELECT user_id, last_active FROM clients WHERE status = 'active'"
            ).fetchall()
            for row in rows:
                last_active = str_to_dt(row["last_active"])
                if last_active and last_active < inactive_before:
                    db.execute(
                        """
                        UPDATE clients
                        SET status = 'inactive', hidden_at = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (dt_to_str(now), dt_to_str(now), row["user_id"]),
                    )
                    hidden_count += 1

            rows = db.execute(
                "SELECT user_id, hidden_at FROM clients WHERE status = 'inactive'"
            ).fetchall()
            for row in rows:
                hidden_at = str_to_dt(row["hidden_at"])
                if hidden_at and hidden_at < delete_before:
                    db.execute("DELETE FROM payment_reminders WHERE user_id = ?", (row["user_id"],))
                    db.execute("DELETE FROM clients WHERE user_id = ?", (row["user_id"],))
                    deleted_count += 1

        return hidden_count, deleted_count


repo = ClientRepository(DB_PATH)


def row_name(row: sqlite3.Row) -> str:
    return f"@{row['username']}" if row["username"] else row["first_name"]


def message_client_data(message: Message) -> tuple[int, Optional[str], str]:
    client_id = message.chat.id
    username = message.chat.username
    first_name = message.chat.first_name or message.chat.full_name or str(client_id)
    return client_id, username, first_name


def chat_name(message: Message) -> str:
    if message.chat.username:
        return f"@{message.chat.username}"
    return message.chat.full_name or message.chat.title or str(message.chat.id)


def format_dt(value: Optional[str]) -> str:
    dt = str_to_dt(value)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M") if dt else "—"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Актуальные клиенты", callback_data="list:active")],
            [InlineKeyboardButton("📂 Неактуальные клиенты", callback_data="list:inactive")],
        ]
    )


def client_keyboard(user_id: int, status: str) -> InlineKeyboardMarkup:
    if status == "active":
        action = InlineKeyboardButton("⬇️ В неактуальные", callback_data=f"hide:{user_id}")
        back_data = "list:active"
    else:
        action = InlineKeyboardButton("⬆️ Вернуть", callback_data=f"restore:{user_id}")
        back_data = "list:inactive"

    return InlineKeyboardMarkup(
        [
            [action],
            [InlineKeyboardButton("⬅️ К списку", callback_data=back_data)],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
        ]
    )


def open_client_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Открыть клиента", callback_data=f"client:{user_id}")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_user.id != OWNER_ID:
        if update.effective_message:
            await update.effective_message.reply_text("Доступ запрещён.")
        return
    await update.effective_message.reply_text(
        "Управление клиентами:", reply_markup=main_menu()
    )


async def show_list(query, status: str) -> None:
    rows = repo.list_by_status(status)
    title = "📋 Актуальные клиенты" if status == "active" else "📂 Неактуальные клиенты"

    if not rows:
        await query.edit_message_text(f"{title}\n\nСписок пуст.", reply_markup=main_menu())
        return

    buttons = []
    for row in rows[:80]:
        marker = "🟢" if status == "active" else "⚪️"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{marker} {row_name(row)} — {STAGES[row['stage']]}",
                    callback_data=f"client:{row['user_id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu")])

    suffix = "\nПоказаны первые 80." if len(rows) > 80 else ""
    await query.edit_message_text(
        f"{title}\n\nВсего: {len(rows)}{suffix}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_client(query, user_id: int) -> None:
    row = repo.get(user_id)
    if row is None:
        await query.edit_message_text("Клиент не найден.", reply_markup=main_menu())
        return

    timing = (
        f"Последняя активность: {format_dt(row['last_active'])}"
        if row["status"] == "active"
        else f"Скрыт: {format_dt(row['hidden_at'])}"
    )
    text = (
        f"👤 {row_name(row)}\n"
        f"ID чата: {row['user_id']}\n"
        f"Этап: {row['stage'] + 1}/7 — {STAGES[row['stage']]}\n"
        f"Этап обновлён: {format_dt(row['stage_updated_at'])}\n"
        f"Статус: {'активен' if row['status'] == 'active' else 'неактивен'}\n"
        f"{timing}"
    )
    await query.edit_message_text(
        text, reply_markup=client_keyboard(user_id, row["status"])
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if query.from_user.id != OWNER_ID:
        await query.answer("Доступ запрещён", show_alert=True)
        return

    data = query.data or ""
    if data == "menu":
        await query.edit_message_text("Управление клиентами:", reply_markup=main_menu())
    elif data.startswith("list:"):
        await show_list(query, data.split(":", 1)[1])
    elif data.startswith("client:"):
        await show_client(query, int(data.split(":", 1)[1]))
    elif data.startswith("hide:"):
        user_id = int(data.split(":", 1)[1])
        repo.set_status(user_id, "inactive")
        await show_client(query, user_id)
    elif data.startswith("restore:"):
        user_id = int(data.split(":", 1)[1])
        repo.set_status(user_id, "active")
        await show_client(query, user_id)


async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.business_message
    if message is None:
        return

    is_owner_message = bool(message.from_user and message.from_user.id == OWNER_ID)
    direction = "OUTGOING" if is_owner_message else "INCOMING"
    attachment_name = (
        message.effective_attachment.__class__.__name__
        if message.effective_attachment
        else "service message"
    )
    content = message.text or message.caption or f"<{attachment_name}>"
    logger.info(
        "BUSINESS_MESSAGE | %s | chat_id=%s | chat=%s | from_id=%s | message_id=%s | text=%r",
        direction,
        message.chat.id,
        chat_name(message),
        message.from_user.id if message.from_user else None,
        message.message_id,
        content,
    )

    if message.chat.type != ChatType.PRIVATE:
        return

    client_id, username, first_name = message_client_data(message)

    # Триггеры распределены по отправителю:
    # клиент — только PDF; владелец — только «Принято» или 👌.
    trigger = (
        detect_owner_payment_trigger(message)
        if is_owner_message
        else detect_client_payment_trigger(message)
    )
    if trigger:
        delay = random.randint(
            PAYMENT_REMINDER_MIN_SECONDS, PAYMENT_REMINDER_MAX_SECONDS
        )
        due_at = utc_now() + timedelta(seconds=delay)
        created = repo.schedule_payment_reminder(
            client_id,
            username,
            first_name,
            trigger,
            message.message_id,
            due_at,
        )
        if created:
            logger.info(
                "PAYMENT_REMINDER_SCHEDULED | client_id=%s | trigger=%s | due_at=%s",
                client_id,
                trigger,
                dt_to_str(due_at),
            )

    # Этапы отслеживаются только по исходящим сообщениям владельца.
    if not is_owner_message:
        return

    text = message.text or message.caption
    if not text:
        return

    stage = detect_stage(text)
    if stage is None:
        return

    new_stage, changed = repo.upsert_stage(client_id, username, first_name, stage)
    display_name = f"@{username}" if username else first_name
    logger.info(
        "CLIENT_STAGE | client_id=%s | client=%s | stage=%s | stage_name=%s | changed=%s",
        client_id,
        display_name,
        new_stage,
        STAGES[new_stage],
        changed,
    )

    status_word = "обновлён" if changed else "уже был установлен"
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"✅ Клиент {display_name}\n"
                f"Этап {status_word}: {new_stage + 1}/7 — {STAGES[new_stage]}"
            ),
            reply_markup=open_client_keyboard(client_id),
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление владельцу")


async def reminder_loop(application: Application) -> None:
    while True:
        try:
            now = utc_now()

            # Напоминания через 3/6/13 дней без любой новой ключевой фразы этапа.
            for row, day in repo.due_stage_reminders(now):
                try:
                    await application.bot.send_message(
                        chat_id=OWNER_ID,
                        text=(
                            f"⏰ {day} дн. без обновления этапа\n\n"
                            f"Клиент: {row_name(row)}\n"
                            f"Текущий этап: {row['stage'] + 1}/7 — {STAGES[row['stage']]}\n"
                            f"Проверьте клиента и при необходимости продолжите работу."
                        ),
                        reply_markup=open_client_keyboard(int(row["user_id"])),
                    )
                    repo.mark_stage_reminder_sent(int(row["user_id"]), day)
                except Exception:
                    logger.exception(
                        "Не удалось отправить напоминание %s дней для client_id=%s",
                        day,
                        row["user_id"],
                    )

            # Отложенное напоминание: PDF от клиента или «Принято»/👌 от владельца.
            for row in repo.due_payment_reminders(now):
                user_id = int(row["user_id"])
                baseline_stage = int(row["baseline_stage"])
                current_stage = int(row["stage"])

                # Повышение этапа — дополнительная защита. Любая ключевая фраза
                # также удаляет напоминание сразу в upsert_stage().
                if current_stage > baseline_stage:
                    repo.delete_payment_reminder(user_id)
                    continue

                try:
                    await application.bot.send_message(
                        chat_id=OWNER_ID,
                        text=(
                            f"💳 Пора выдать новый платёж\n\n"
                            f"Клиент: {row_name(row)}\n"
                            f"Триггер: {row['trigger_type']}\n"
                            f"Текущий этап: {current_stage + 1}/7 — {STAGES[current_stage]}\n\n"
                            f"После триггера этап не обновился. Выдайте клиенту новый платёж."
                        ),
                        reply_markup=open_client_keyboard(user_id),
                    )
                    repo.delete_payment_reminder(user_id)
                except Exception:
                    logger.exception(
                        "Не удалось отправить платёжное напоминание client_id=%s",
                        user_id,
                    )

            hidden_count, deleted_count = repo.auto_cleanup()
            if hidden_count:
                logger.info("Автоматически скрыто клиентов: %s", hidden_count)
            if deleted_count:
                logger.info("Автоматически удалено клиентов: %s", deleted_count)

        except Exception:
            logger.exception("Ошибка фоновой проверки")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(application: Application) -> None:
    application.create_task(reminder_loop(application), name="reminder-loop")
    logger.info("Бот запущен. База данных: %s", DB_PATH.resolve())


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке update=%r", update, exc_info=context.error)


def main() -> None:
    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_БОТА" or not BOT_TOKEN.strip():
        raise RuntimeError("Вставьте BOT_TOKEN в верхней части bot.py")
    if OWNER_ID <= 0 or OWNER_ID == 123456789:
        raise RuntimeError("Вставьте OWNER_ID в верхней части bot.py")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message)
    )
    application.add_error_handler(error_handler)

    application.run_polling(
        allowed_updates=[
            UpdateType.MESSAGE,
            UpdateType.BUSINESS_MESSAGE,
            UpdateType.CALLBACK_QUERY,
            UpdateType.BUSINESS_CONNECTION,
        ],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()