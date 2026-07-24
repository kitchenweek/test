import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
BOT_TOKEN = "ВСТАВЬТЕ_ТОКЕН_БОТА"
OWNER_ID = 123456789  # Telegram ID владельца бизнес-аккаунта

DB_PATH = Path("clients.db")
INACTIVE_AFTER_HOURS = 6
DELETE_AFTER_DAYS = 14
CHECK_INTERVAL_SECONDS = 3600
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


class ClientRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

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
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert_stage(
        self,
        user_id: int,
        username: Optional[str],
        first_name: str,
        detected_stage: int,
    ) -> tuple[int, bool]:
        now = utc_now()
        now_s = dt_to_str(now)
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
                    (user_id, username, first_name, stage, status,
                     last_active, hidden_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, NULL, ?)
                    """,
                    (user_id, username, first_name, new_stage, now_s, now_s),
                )
            else:
                old_stage = int(row["stage"])
                new_stage = max(old_stage, detected_stage)
                changed = new_stage != old_stage
                db.execute(
                    """
                    UPDATE clients
                    SET username = ?, first_name = ?, stage = ?, status = 'active',
                        last_active = ?, hidden_at = NULL, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (username, first_name, new_stage, now_s, now_s, user_id),
                )
        return new_stage, changed

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
                    db.execute("DELETE FROM clients WHERE user_id = ?", (row["user_id"],))
                    deleted_count += 1

        return hidden_count, deleted_count


repo = ClientRepository(DB_PATH)


def row_name(row: sqlite3.Row) -> str:
    return f"@{row['username']}" if row["username"] else row["first_name"]


def chat_name(message) -> str:
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

    # Логируем ВСЕ полученные бизнес-сообщения: входящие и исходящие,
    # текстовые и нетекстовые. Telegram пришлёт их только из доступных боту чатов.
    direction = (
        "OUTGOING"
        if message.from_user and message.from_user.id == OWNER_ID
        else "INCOMING"
    )
    content = message.text or message.caption or f"<{message.effective_attachment.__class__.__name__ if message.effective_attachment else 'service message'}>"
    logger.info(
        "BUSINESS_MESSAGE | %s | chat_id=%s | chat=%s | from_id=%s | message_id=%s | text=%r",
        direction,
        message.chat.id,
        chat_name(message),
        message.from_user.id if message.from_user else None,
        message.message_id,
        content,
    )

    # Этапы отслеживаются только по исходящим сообщениям владельца
    # в личном чате с клиентом.
    if (
        message.chat.type != ChatType.PRIVATE
        or message.from_user is None
        or message.from_user.id != OWNER_ID
    ):
        return

    text = message.text or message.caption
    if not text:
        return

    stage = detect_stage(text)
    if stage is None:
        return

    # Реплай и отметка НЕ нужны: в business_message текущий message.chat
    # уже является личным чатом клиента.
    client_id = message.chat.id
    username = message.chat.username
    first_name = message.chat.first_name or message.chat.full_name or str(client_id)
    new_stage, changed = repo.upsert_stage(client_id, username, first_name, stage)

    status_word = "обновлён" if changed else "подтверждён"
    display_name = f"@{username}" if username else first_name
    logger.info(
        "CLIENT_STAGE | client_id=%s | client=%s | stage=%s | stage_name=%s",
        client_id,
        display_name,
        new_stage,
        STAGES[new_stage],
    )

    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"✅ Клиент {display_name}\n"
                f"Этап {status_word}: {new_stage + 1}/7 — {STAGES[new_stage]}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Открыть клиента", callback_data=f"client:{client_id}")]]
            ),
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление владельцу")


async def cleanup_loop() -> None:
    while True:
        try:
            hidden_count, deleted_count = repo.auto_cleanup()
            if hidden_count:
                logger.info("Автоматически скрыто клиентов: %s", hidden_count)
            if deleted_count:
                logger.info("Автоматически удалено клиентов: %s", deleted_count)
        except Exception:
            logger.exception("Ошибка фоновой очистки")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(application: Application) -> None:
    application.create_task(cleanup_loop(), name="client-cleanup")
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