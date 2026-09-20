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
OWNER_IDS = (7517164478, 8810454725)  # Telegram ID владельцев бизнес-аккаунтов

DB_PATH = Path("clients.db")
INACTIVE_AFTER_HOURS = 6
DELETE_AFTER_DAYS = 14
CHECK_INTERVAL_SECONDS = 30
STAGE_REMINDER_DAYS = (3, 6, 13)
PAYMENT_REMINDER_MIN_SECONDS = 3 * 60
PAYMENT_REMINDER_MAX_SECONDS = 6 * 60
PAYMENT_REPEAT_SECONDS = 5 * 60
# =====================================================

STAGES = [
    "Доставка",
    "СВ",
    "Залог",
    "Перерасчёт СВ",
    "Перерасчёт залога",
    "Лот по СВ",
    "Лот по залогу",
    "Комиссия 50%",
    "Комиссия 100%",
    "Депозит",
    "Депозит на залог",
    "Депозит на комиссию 50%",
    "Депозит на комиссию 100%",
    "Задаток",
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
    7: ["тк запросила комиссию 50% на неотправленные лоты по св и залогу у клиента перед отправкой"],
    8: ["тк запросила комиссию 100% на неотправленные лоты по итогу, так как не отправили дважды (1-лот по св, 2-лот по залогу), сумма к оплате"],
    9: ["тк запросили депозит на отдельную отправку по лоту у клиента"],
    10: ["депозит по лоту на залог"],
    11: ["депозит на комиссию"],
    12: ["депозит на вторую комиссию (100% которая) также"],
    13: ["задаток за перенос отправки у клиента"],
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
    """Клиентский триггер: любой PDF-документ во входящем business-сообщении."""
    document = message.document
    if document is None:
        attachment = message.effective_attachment
        if attachment is not None and hasattr(attachment, "mime_type") and hasattr(attachment, "file_name"):
            document = attachment
    if document is None:
        return None

    filename = (getattr(document, "file_name", None) or "").casefold().strip()
    mime_type = (getattr(document, "mime_type", None) or "").casefold().split(";", 1)[0].strip()
    if filename.endswith(".pdf") or mime_type == "application/pdf":
        return "PDF-файл от клиента"
    return None


def detect_owner_payment_trigger(message: Message) -> Optional[str]:
    """Триггер владельцев: только слово «Принято» в исходящем сообщении."""
    text = message.text or message.caption or ""
    normalized = normalize_text(text)

    if "принято" in normalized:
        return "Принято от владельца"
    return None


class ClientRepository:
    """Хранилище, изолированное по owner_id + user_id."""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("""
                CREATE TABLE IF NOT EXISTS clients_scoped (
                    owner_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
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
                    reminder_13_sent INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id, user_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS payment_reminders_scoped (
                    owner_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_message_id INTEGER,
                    baseline_stage INTEGER NOT NULL,
                    due_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    repeat_seconds INTEGER NOT NULL DEFAULT 300,
                    PRIMARY KEY(owner_id, user_id),
                    FOREIGN KEY(owner_id, user_id)
                        REFERENCES clients_scoped(owner_id, user_id) ON DELETE CASCADE
                )
            """)

    def adopt_legacy_client(self, owner_id: int, user_id: int) -> None:
        """Однократно подхватывает старую запись при первом событии именно этого business-аккаунта."""
        with self.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM clients_scoped WHERE owner_id=? AND user_id=?",
                (owner_id, user_id),
            ).fetchone()
            if exists:
                return
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clients'"
            ).fetchone()
            if not table:
                return
            old = db.execute("SELECT * FROM clients WHERE user_id=?", (user_id,)).fetchone()
            if old is None:
                return
            keys = set(old.keys())
            now_s = dt_to_str(utc_now())
            db.execute("""
                INSERT OR IGNORE INTO clients_scoped
                (owner_id,user_id,username,first_name,stage,status,last_active,hidden_at,
                 updated_at,stage_updated_at,reminder_3_sent,reminder_6_sent,reminder_13_sent)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                owner_id, user_id, old['username'], old['first_name'], int(old['stage']),
                old['status'], old['last_active'], old['hidden_at'], old['updated_at'],
                old['stage_updated_at'] if 'stage_updated_at' in keys else old['updated_at'],
                int(old['reminder_3_sent']) if 'reminder_3_sent' in keys else 0,
                int(old['reminder_6_sent']) if 'reminder_6_sent' in keys else 0,
                int(old['reminder_13_sent']) if 'reminder_13_sent' in keys else 0,
            ))

    def upsert_stage(self, owner_id: int, user_id: int, username: Optional[str],
                     first_name: str, detected_stage: int) -> tuple[int, bool]:
        now_s = dt_to_str(utc_now())
        with self.connect() as db:
            row = db.execute(
                "SELECT stage FROM clients_scoped WHERE owner_id=? AND user_id=?",
                (owner_id, user_id),
            ).fetchone()
            if row is None:
                new_stage, changed = detected_stage, True
                db.execute("""
                    INSERT INTO clients_scoped
                    (owner_id,user_id,username,first_name,stage,status,last_active,hidden_at,
                     updated_at,stage_updated_at,reminder_3_sent,reminder_6_sent,reminder_13_sent)
                    VALUES (?,?,?,?,?,'active',?,NULL,?,?,0,0,0)
                """, (owner_id,user_id,username,first_name,new_stage,now_s,now_s,now_s))
            else:
                old_stage = int(row["stage"])
                new_stage = max(old_stage, detected_stage)
                changed = new_stage > old_stage
                db.execute("""
                    UPDATE clients_scoped SET username=?,first_name=?,stage=?,status='active',
                        last_active=?,hidden_at=NULL,updated_at=?,stage_updated_at=?,
                        reminder_3_sent=0,reminder_6_sent=0,reminder_13_sent=0
                    WHERE owner_id=? AND user_id=?
                """, (username,first_name,new_stage,now_s,now_s,now_s,owner_id,user_id))
                db.execute(
                    "DELETE FROM payment_reminders_scoped WHERE owner_id=? AND user_id=?",
                    (owner_id,user_id),
                )
        return new_stage, changed

    def schedule_payment_reminder(self, owner_id: int, user_id: int,
                                  username: Optional[str], first_name: str,
                                  trigger_type: str, trigger_message_id: int,
                                  due_at: datetime, replace_existing: bool = True) -> bool:
        with self.connect() as db:
            client = db.execute(
                "SELECT * FROM clients_scoped WHERE owner_id=? AND user_id=?",
                (owner_id,user_id),
            ).fetchone()
            if client is None:
                logger.info("PAYMENT_TRIGGER_IGNORED | owner_id=%s | client_id=%s | reason=no_stage | trigger=%s",
                            owner_id,user_id,trigger_type)
                return False
            existing = db.execute(
                "SELECT 1 FROM payment_reminders_scoped WHERE owner_id=? AND user_id=?",
                (owner_id,user_id),
            ).fetchone()
            if existing and not replace_existing:
                return False
            if existing:
                db.execute("DELETE FROM payment_reminders_scoped WHERE owner_id=? AND user_id=?",
                           (owner_id,user_id))
            now_s=dt_to_str(utc_now())
            db.execute("""
                UPDATE clients_scoped SET username=?,first_name=?,status='active',last_active=?,
                    hidden_at=NULL,updated_at=? WHERE owner_id=? AND user_id=?
            """, (username,first_name,now_s,now_s,owner_id,user_id))
            db.execute("""
                INSERT INTO payment_reminders_scoped
                (owner_id,user_id,trigger_type,trigger_message_id,baseline_stage,due_at,created_at,repeat_seconds)
                VALUES (?,?,?,?,?,?,?,?)
            """, (owner_id,user_id,trigger_type,trigger_message_id,int(client["stage"]),
                  dt_to_str(due_at),now_s,PAYMENT_REPEAT_SECONDS))
            return True

    def reschedule_payment_reminder(self, owner_id: int, user_id: int, due_at: datetime) -> None:
        with self.connect() as db:
            db.execute("UPDATE payment_reminders_scoped SET due_at=? WHERE owner_id=? AND user_id=?",
                       (dt_to_str(due_at),owner_id,user_id))

    def due_payment_reminders(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("""
                SELECT p.*,c.username,c.first_name,c.stage
                FROM payment_reminders_scoped p
                JOIN clients_scoped c ON c.owner_id=p.owner_id AND c.user_id=p.user_id
                WHERE p.due_at<=? ORDER BY p.due_at
            """, (dt_to_str(now),)).fetchall()

    def delete_payment_reminder(self, owner_id: int, user_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM payment_reminders_scoped WHERE owner_id=? AND user_id=?",
                       (owner_id,user_id))

    def due_stage_reminders(self, now: datetime) -> list[tuple[sqlite3.Row,int]]:
        result=[]
        with self.connect() as db:
            for row in db.execute("SELECT * FROM clients_scoped").fetchall():
                stage_updated_at=str_to_dt(row["stage_updated_at"])
                if not stage_updated_at: continue
                elapsed=now-stage_updated_at
                for day in STAGE_REMINDER_DAYS:
                    if elapsed>=timedelta(days=day) and not int(row[f"reminder_{day}_sent"]):
                        result.append((row,day))
        return result

    def mark_stage_reminder_sent(self, owner_id: int, user_id: int, day: int) -> None:
        if day not in STAGE_REMINDER_DAYS: raise ValueError("Недопустимый срок напоминания")
        with self.connect() as db:
            db.execute(f"UPDATE clients_scoped SET reminder_{day}_sent=1 WHERE owner_id=? AND user_id=?",
                       (owner_id,user_id))

    def set_status(self, owner_id: int, user_id: int, status: str) -> bool:
        now_s=dt_to_str(utc_now()); hidden_at=now_s if status=='inactive' else None
        with self.connect() as db:
            cur=db.execute("""
                UPDATE clients_scoped SET status=?,hidden_at=?,
                    last_active=CASE WHEN ?='active' THEN ? ELSE last_active END,updated_at=?
                WHERE owner_id=? AND user_id=?
            """, (status,hidden_at,status,now_s,now_s,owner_id,user_id))
            return cur.rowcount>0

    def get(self, owner_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM clients_scoped WHERE owner_id=? AND user_id=?",
                              (owner_id,user_id)).fetchone()

    def list_by_status(self, owner_id: int, status: str) -> list[sqlite3.Row]:
        order='last_active DESC' if status=='active' else 'hidden_at DESC'
        with self.connect() as db:
            return db.execute(f"SELECT * FROM clients_scoped WHERE owner_id=? AND status=? ORDER BY {order}",
                              (owner_id,status)).fetchall()

    def auto_cleanup(self) -> tuple[int,int]:
        now=utc_now(); inactive_before=now-timedelta(hours=INACTIVE_AFTER_HOURS); delete_before=now-timedelta(days=DELETE_AFTER_DAYS)
        hidden_count=deleted_count=0
        with self.connect() as db:
            for row in db.execute("SELECT owner_id,user_id,last_active FROM clients_scoped WHERE status='active'").fetchall():
                last_active=str_to_dt(row['last_active'])
                if last_active and last_active<inactive_before:
                    db.execute("UPDATE clients_scoped SET status='inactive',hidden_at=?,updated_at=? WHERE owner_id=? AND user_id=?",
                               (dt_to_str(now),dt_to_str(now),row['owner_id'],row['user_id']))
                    hidden_count+=1
            for row in db.execute("SELECT owner_id,user_id,hidden_at FROM clients_scoped WHERE status='inactive'").fetchall():
                hidden_at=str_to_dt(row['hidden_at'])
                if hidden_at and hidden_at<delete_before:
                    db.execute("DELETE FROM clients_scoped WHERE owner_id=? AND user_id=?",(row['owner_id'],row['user_id']))
                    deleted_count+=1
        return hidden_count,deleted_count


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


def payment_reminder_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔇 Заглушить", callback_data=f"mute_payment:{user_id}")],
            [InlineKeyboardButton("Открыть клиента", callback_data=f"client:{user_id}")],
        ]
    )


_BUSINESS_OWNER_CACHE: dict[str, int] = {}

async def resolve_business_owner(application: Application, message: Message) -> Optional[int]:
    connection_id = message.business_connection_id
    if not connection_id:
        logger.warning("BUSINESS_OWNER_UNKNOWN | chat_id=%s | reason=no_connection_id", message.chat.id)
        return None
    if connection_id in _BUSINESS_OWNER_CACHE:
        return _BUSINESS_OWNER_CACHE[connection_id]
    try:
        connection = await application.bot.get_business_connection(connection_id)
        owner_id = int(connection.user.id)
    except Exception:
        logger.exception("Не удалось определить владельца Business Connection %s", connection_id)
        return None
    if owner_id not in OWNER_IDS:
        logger.warning("BUSINESS_OWNER_UNKNOWN | connection_id=%s | owner_id=%s | reason=not_allowed", connection_id, owner_id)
        return None
    _BUSINESS_OWNER_CACHE[connection_id] = owner_id
    return owner_id

async def send_to_owner(application: Application, owner_id: int, text: str, reply_markup) -> bool:
    try:
        await application.bot.send_message(chat_id=owner_id,text=text,reply_markup=reply_markup)
        return True
    except Exception:
        logger.exception("Не удалось отправить уведомление owner_id=%s", owner_id)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_user.id not in OWNER_IDS:
        if update.effective_message:
            await update.effective_message.reply_text("Доступ запрещён.")
        return
    await update.effective_message.reply_text(
        "Управление клиентами:", reply_markup=main_menu()
    )


async def show_list(query, owner_id: int, status: str) -> None:
    rows = repo.list_by_status(owner_id, status)
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


async def show_client(query, owner_id: int, user_id: int) -> None:
    row = repo.get(owner_id, user_id)
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
        f"Этап: {row['stage'] + 1}/{len(STAGES)} — {STAGES[row['stage']]}\n"
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

    if query.from_user.id not in OWNER_IDS:
        await query.answer("Доступ запрещён", show_alert=True)
        return

    data = query.data or ""
    if data == "menu":
        await query.edit_message_text("Управление клиентами:", reply_markup=main_menu())
    elif data.startswith("list:"):
        await show_list(query, query.from_user.id, data.split(":", 1)[1])
    elif data.startswith("client:"):
        await show_client(query, query.from_user.id, int(data.split(":", 1)[1]))
    elif data.startswith("hide:"):
        user_id = int(data.split(":", 1)[1])
        repo.set_status(query.from_user.id, user_id, "inactive")
        await show_client(query, query.from_user.id, user_id)
    elif data.startswith("restore:"):
        user_id = int(data.split(":", 1)[1])
        repo.set_status(query.from_user.id, user_id, "active")
        await show_client(query, query.from_user.id, user_id)
    elif data.startswith("mute_payment:"):
        user_id = int(data.split(":", 1)[1])
        repo.delete_payment_reminder(query.from_user.id, user_id)
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Открыть клиента", callback_data=f"client:{user_id}")]]
                )
            )
        except Exception:
            logger.exception("Не удалось обновить сообщение после заглушения client_id=%s", user_id)
        await query.answer(
            "Текущая серия напоминаний заглушена. Новый триггер запустит её снова.",
            show_alert=True,
        )


async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.business_message
    if message is None:
        return

    owner_id = await resolve_business_owner(context.application, message)
    if owner_id is None:
        return
    is_owner_message = bool(message.from_user and message.from_user.id == owner_id)
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
    repo.adopt_legacy_client(owner_id, client_id)

    # Триггеры распределены по отправителю и срабатывают только для
    # клиентов, уже добавленных на один из этапов:
    # клиент — только PDF; владелец — только «Принято».
    # Если клиент был скрыт, любой из этих триггеров возвращает его в активные.
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
            owner_id,
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

    new_stage, changed = repo.upsert_stage(owner_id, client_id, username, first_name, stage)
    display_name = f"@{username}" if username else first_name
    logger.info(
        "CLIENT_STAGE | client_id=%s | client=%s | stage=%s | stage_name=%s | changed=%s",
        client_id,
        display_name,
        new_stage,
        STAGES[new_stage],
        changed,
    )

async def reminder_loop(application: Application) -> None:
    while True:
        try:
            now = utc_now()

            # Напоминания через 3/6/13 дней без любой новой ключевой фразы этапа.
            for row, day in repo.due_stage_reminders(now):
                try:
                    delivered = await send_to_owner(
                        application,
                        int(row["owner_id"]),
                        (
                            f"⏰ {day} дн. без обновления этапа\n\n"
                            f"Клиент: {row_name(row)}\n"
                            f"Текущий этап: {row['stage'] + 1}/{len(STAGES)} — {STAGES[row['stage']]}\n"
                            f"Проверьте клиента и при необходимости продолжите работу."
                        ),
                        open_client_keyboard(int(row["user_id"])),
                    )
                    if delivered:
                        repo.mark_stage_reminder_sent(int(row["owner_id"]), int(row["user_id"]), day)
                except Exception:
                    logger.exception(
                        "Не удалось отправить напоминание %s дней для client_id=%s",
                        day,
                        row["user_id"],
                    )

            # Отложенное напоминание: PDF от клиента или «Принято» от владельца.
            for row in repo.due_payment_reminders(now):
                user_id = int(row["user_id"])
                baseline_stage = int(row["baseline_stage"])
                current_stage = int(row["stage"])

                # Повышение этапа — дополнительная защита. Любая ключевая фраза
                # также удаляет напоминание сразу в upsert_stage().
                if current_stage > baseline_stage:
                    repo.delete_payment_reminder(int(row["owner_id"]), user_id)
                    continue

                try:
                    delivered = await send_to_owner(
                        application,
                        int(row["owner_id"]),
                        (
                            f"💳 Пора выдать новый платёж\n\n"
                            f"Клиент: {row_name(row)}\n"
                            f"Триггер: {row['trigger_type']}\n"
                            f"Текущий этап: {current_stage + 1}/{len(STAGES)} — {STAGES[current_stage]}\n\n"
                            "После триггера этап не обновился. "
                            "Напоминание будет повторяться каждые 5 минут."
                        ),
                        payment_reminder_keyboard(user_id),
                    )
                    if delivered:
                        repo.reschedule_payment_reminder(
                            int(row["owner_id"]),
                            user_id,
                            utc_now() + timedelta(seconds=int(row["repeat_seconds"])),
                        )
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
    if not OWNER_IDS or any(owner_id <= 0 for owner_id in OWNER_IDS):
        raise RuntimeError("Укажите корректные OWNER_IDS в верхней части bot.py")

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