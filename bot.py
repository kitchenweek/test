# -*- coding: utf-8 -*-

import asyncio
import html
import json
import logging
import random
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import TelegramClient, errors
from telethon.network.connection import ConnectionTcpMTProxyAbridged


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = "8623083352:AAHPhZkAFymFxs272OO_YYECCeXQUXfH8is"
ADMIN_ID = 2010296191

API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"

MIN_DELAY_SECONDS = 180
MAX_DELAY_SECONDS = 420
MAX_FLOOD_WAIT_SECONDS = 900

MAX_TEXT_MESSAGES = 5
MAX_GROUP_TEMPLATES = 1000
MAX_TEMPLATE_SENDS = 100
SCAN_MESSAGE_LIMIT = 2000

# Скорость печати (секунд на символ)
CHAR_TYPING_SPEED = 0.13

TEMPLATE_REQUIRED_PHRASES = [
    "@WorldOfPoizon",
    "18.06",
    "Egor Sobolev",
]

DATA_DIR = Path("users_data")


# ============================================================
# ЛОГИРОВАНИЕ И ПАМЯТЬ ПРОЦЕССА
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("multi-account-broadcast")

router = Router()

user_steps: dict[int, str] = {}
flow_data: dict[int, dict[str, Any]] = {}

user_clients: dict[tuple[int, str], TelegramClient] = {}
qr_tasks: dict[int, asyncio.Task] = {}
broadcast_tasks: dict[int, asyncio.Task] = {}
stop_events: dict[int, asyncio.Event] = {}


# ============================================================
# ФУНКЦИЯ ЗАМЕНЫ БУКВ С ВЕРОЯТНОСТЬЮ 50%
# ============================================================

def replace_letters_random(text: str) -> str:
    """
    Заменяет кириллические буквы на латинские аналоги с вероятностью 50% для каждой буквы.
    """
    replacements = {
        'а': 'a', 'с': 'c', 'е': 'e', 'о': 'o', 'р': 'p', 'х': 'x', 'у': 'y',
        'А': 'A', 'С': 'C', 'Е': 'E', 'О': 'O', 'Р': 'P', 'Х': 'X', 'Т': 'T',
        'Н': 'H', 'В': 'B', 'К': 'K'
    }
    
    result = ''
    for char in text:
        if char in replacements and random.random() < 0.5:
            result += replacements[char]
        else:
            result += char
    
    return result


# ============================================================
# ФУНКЦИЯ ДЛЯ РАСЧЁТА ЗАДЕРЖКИ ПЕЧАТИ
# ============================================================

def calculate_typing_delay(text: str) -> float:
    """
    Рассчитывает задержку для имитации печати текста.
    """
    return len(text) * CHAR_TYPING_SPEED


# ============================================================
# СОСТОЯНИЕ
# ============================================================

def user_dir(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(user_id: int) -> Path:
    return user_dir(user_id) / "state.json"


def session_path(user_id: int, account_id: str) -> str:
    return str(user_dir(user_id) / f"telegram_session_{account_id}")


def default_state() -> dict[str, Any]:
    return {
        "accounts": {},
        "active_account_id": None,
        "next_account_number": 1,
        "messages": [],
        "common_recipients": [],
        "individual_recipients": {},
        "recipient_owners": {},
        "bound_groups": [],
        "group_templates": [],
        "template_usage": {},
        "last_template_key": None,
    }


def migrate_state(loaded: dict[str, Any]) -> dict[str, Any]:
    state = default_state()
    state.update(loaded)

    if "recipients" in loaded and not state["common_recipients"]:
        state["common_recipients"] = loaded.get("recipients", [])

    if not state["accounts"] and loaded.get("proxy"):
        account_id = "account_1"
        state["accounts"][account_id] = {
            "tag": "Аккаунт 1",
            "proxy": loaded.get("proxy"),
            "telegram_id": None,
            "username": None,
            "first_name": None,
        }
        state["active_account_id"] = account_id
        state["next_account_number"] = 2
        state["individual_recipients"].setdefault(account_id, [])

    for account_id in state["accounts"]:
        state["individual_recipients"].setdefault(account_id, [])

    return state


def load_state(user_id: int) -> dict[str, Any]:
    path = state_path(user_id)
    if not path.exists():
        state = default_state()
        save_state(user_id, state)
        return state

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Повреждён state.json пользователя %s", user_id)
        loaded = {}

    state = migrate_state(loaded)
    return state


def save_state(user_id: int, state: dict[str, Any]) -> None:
    state_path(user_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def event_user_label(event: Message | CallbackQuery) -> str:
    user = event.from_user
    if user is None:
        return "неизвестный пользователь"
    username = f"@{user.username}" if user.username else "без username"
    return f"{html.escape(user.full_name)} | {username} | <code>{user.id}</code>"


async def admin_log(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, f"<b>Лог</b>\n{text}")
    except Exception:
        log.exception("Не удалось отправить лог администратору")


def normalize_recipient(value: str | int) -> str:
    text = str(value).strip()
    text = re.sub(
        r"^https?://(?:www\.)?t\.me/",
        "@",
        text,
        flags=re.IGNORECASE,
    ).rstrip("/")

    if text.lstrip("-").isdigit():
        return str(int(text))

    if text and not text.startswith("@") and re.fullmatch(r"[A-Za-z0-9_]{5,}", text):
        text = "@" + text

    return text.casefold()


def parse_recipients(text: str) -> list[str | int]:
    result: list[str | int] = []
    seen: set[str] = set()

    for raw in re.split(r"[\s,;]+", text):
        value = raw.strip()
        if not value:
            continue

        value = re.sub(
            r"^https?://(?:www\.)?t\.me/",
            "@",
            value,
            flags=re.IGNORECASE,
        ).rstrip("/")

        parsed: str | int
        if value.lstrip("-").isdigit():
            parsed = int(value)
        else:
            if not value.startswith("@") and re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
                value = "@" + value
            parsed = value

        key = normalize_recipient(parsed)
        if key and key not in seen:
            seen.add(key)
            result.append(parsed)

    return result


def make_template_key(chat_id: int, message_id: int) -> str:
    return f"{int(chat_id)}:{int(message_id)}"


def normalize_template_text(value: str) -> str:
    invisible = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
    return "".join(ch for ch in value if ch not in invisible).casefold()


def contains_required_phrase(text: str) -> bool:
    normalized = normalize_template_text(text or "")
    return any(
        normalize_template_text(phrase) in normalized
        for phrase in TEMPLATE_REQUIRED_PHRASES
    )


def account_label(account_id: str, account: dict[str, Any]) -> str:
    tag = account.get("tag") or account_id
    tg_id = account.get("telegram_id")
    username = account.get("username")
    suffix = f" | {tg_id}" if tg_id else ""
    if username:
        suffix += f" | @{username}"
    return f"{tag}{suffix}"


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Аккаунты", callback_data="accounts")],
            [
                InlineKeyboardButton(text="📝 Сообщения", callback_data="messages"),
                InlineKeyboardButton(text="👥 Получатели", callback_data="recipients"),
            ],
            [
                InlineKeyboardButton(text="📚 Шаблоны группы", callback_data="group_templates"),
                InlineKeyboardButton(text="📊 Статус", callback_data="status"),
            ],
            [
                InlineKeyboardButton(text="▶️ Запустить", callback_data="start_broadcast"),
                InlineKeyboardButton(text="⛔ Остановить", callback_data="stop_broadcast"),
            ],
        ]
    )


def back_keyboard(callback_data: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]
        ]
    )


def accounts_keyboard(state: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for account_id, account in state["accounts"].items():
        tag = account.get("tag") or account_id
        tg_id = account.get("telegram_id") or "не подключён"
        rows.append([
            InlineKeyboardButton(
                text=f"👤 {tag} | {tg_id}",
                callback_data=f"select_account:{account_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Подключить / переподключить",
                    callback_data=f"account_proxy:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏷 Изменить тег",
                    callback_data=f"rename_account:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚪 Выйти",
                    callback_data=f"logout_account:{account_id}",
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"delete_account:{account_id}",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ К аккаунтам", callback_data="accounts")],
        ]
    )


def proxy_protocol_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="SOCKS5",
                    callback_data=f"proxy_protocol:{account_id}:socks5",
                ),
                InlineKeyboardButton(
                    text="SOCKS4",
                    callback_data=f"proxy_protocol:{account_id}:socks4",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="HTTP",
                    callback_data=f"proxy_protocol:{account_id}:http",
                ),
                InlineKeyboardButton(
                    text="MTProto",
                    callback_data=f"proxy_protocol:{account_id}:mtproto",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"select_account:{account_id}")],
        ]
    )


def login_method_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 По номеру",
                    callback_data=f"login_phone:{account_id}",
                ),
                InlineKeyboardButton(
                    text="📷 По QR-коду",
                    callback_data=f"login_qr:{account_id}",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"select_account:{account_id}")],
        ]
    )


def messages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить сообщения", callback_data="add_messages")],
            [InlineKeyboardButton(text="🗑 Удалить сообщение", callback_data="delete_message")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def adding_messages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить", callback_data="finish_messages")]
        ]
    )


def recipients_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить получателей", callback_data="recipient_mode")],
            [InlineKeyboardButton(text="🗑 Очистить списки", callback_data="clear_recipients_menu")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def recipient_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Индивидуальный", callback_data="recipient_mode:individual"),
                InlineKeyboardButton(text="🌐 Общий", callback_data="recipient_mode:common"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="recipients")],
        ]
    )


def account_choice_keyboard(
    state: dict[str, Any],
    action_prefix: str,
    back: str = "recipients",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for account_id, account in state["accounts"].items():
        rows.append([
            InlineKeyboardButton(
                text=account_label(account_id, account)[:60],
                callback_data=f"{action_prefix}:{account_id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def clear_recipients_keyboard(state: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🗑 Очистить общий список", callback_data="clear_common_recipients")],
        [InlineKeyboardButton(text="🧹 Очистить всё", callback_data="clear_all_recipients")],
    ]
    if state["accounts"]:
        rows.append([
            InlineKeyboardButton(
                text="👤 Очистить индивидуальный",
                callback_data="choose_clear_individual",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="recipients")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_templates_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Просканировать шаблоны", callback_data="scan_group_templates")],
            [InlineKeyboardButton(text="🗑 Очистить шаблоны", callback_data="clear_group_templates")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


# ============================================================
# ПРОКСИ И TELETHON
# ============================================================

def parse_proxy(value: str, protocol: str) -> dict[str, Any]:
    value = value.strip()

    if "://" in value:
        if value.lower().startswith("tg://proxy"):
            parsed = urlparse(value)
            query = parse_qs(parsed.query)
            host = (query.get("server") or [""])[0]
            port = int((query.get("port") or ["0"])[0])
            secret = (query.get("secret") or [""])[0]
            if not host or not port or not secret:
                raise ValueError("В MTProto-ссылке нужны server, port и secret")
            return {"type": "mtproto", "host": host, "port": port, "secret": secret}

        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        if scheme in {"socks5", "socks4", "http"}:
            if not parsed.hostname or not parsed.port:
                raise ValueError("Не найдены IP или порт")
            return {
                "type": scheme,
                "host": parsed.hostname,
                "port": parsed.port,
                "username": parsed.username or "",
                "password": parsed.password or "",
                "rdns": True,
            }

    parts = value.split(":")
    protocol = protocol.lower()

    if protocol in {"socks5", "socks4", "http"}:
        if len(parts) not in {2, 4}:
            raise ValueError("Формат: IP:ПОРТ:ЛОГИН:ПАРОЛЬ")
        host = parts[0].strip()
        port = int(parts[1].strip())
        username = parts[2].strip() if len(parts) == 4 else ""
        password = parts[3].strip() if len(parts) == 4 else ""
        return {
            "type": protocol,
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "rdns": True,
        }

    if protocol == "mtproto":
        if len(parts) != 3:
            raise ValueError("Формат MTProto: IP:ПОРТ:SECRET")
        return {
            "type": "mtproto",
            "host": parts[0].strip(),
            "port": int(parts[1].strip()),
            "secret": parts[2].strip(),
        }

    raise ValueError("Неизвестный протокол")


def client_options(account: dict[str, Any]) -> dict[str, Any]:
    proxy = account.get("proxy")
    if not proxy:
        raise ValueError("Для аккаунта не задан прокси")

    options: dict[str, Any] = {
        "device_model": "Desktop",
        "system_version": "Windows 11",
        "app_version": "1.0",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    }

    if proxy["type"] == "mtproto":
        options["connection"] = ConnectionTcpMTProxyAbridged
        options["proxy"] = (
            proxy["host"],
            int(proxy["port"]),
            proxy["secret"],
        )
    else:
        options["proxy"] = (
            proxy["type"],
            proxy["host"],
            int(proxy["port"]),
            bool(proxy.get("rdns", True)),
            proxy.get("username") or None,
            proxy.get("password") or None,
        )

    return options


async def rebuild_client(user_id: int, account_id: str) -> TelegramClient:
    key = (user_id, account_id)
    old = user_clients.pop(key, None)
    if old:
        try:
            await old.disconnect()
        except Exception:
            pass

    state = load_state(user_id)
    account = state["accounts"].get(account_id)
    if not account:
        raise ValueError("Аккаунт не найден")

    client = TelegramClient(
        session_path(user_id, account_id),
        API_ID,
        API_HASH,
        **client_options(account),
    )
    await client.connect()
    user_clients[key] = client
    return client


async def get_client(user_id: int, account_id: str) -> TelegramClient:
    key = (user_id, account_id)
    client = user_clients.get(key)
    if client is None:
        return await rebuild_client(user_id, account_id)
    if not client.is_connected():
        await client.connect()
    return client


async def update_account_identity(user_id: int, account_id: str, client: TelegramClient) -> None:
    me = await client.get_me()
    state = load_state(user_id)
    account = state["accounts"].get(account_id)
    if not account:
        return

    account["telegram_id"] = int(me.id)
    account["username"] = me.username
    account["first_name"] = me.first_name
    save_state(user_id, state)


async def authorized_accounts(user_id: int) -> list[tuple[str, dict[str, Any], TelegramClient]]:
    state = load_state(user_id)
    result = []

    for account_id, account in state["accounts"].items():
        try:
            client = await get_client(user_id, account_id)
            if await client.is_user_authorized():
                await update_account_identity(user_id, account_id, client)
                result.append((account_id, account, client))
        except Exception:
            log.exception("Аккаунт %s недоступен", account_id)

    return result


async def resolve_dialog_entity(client: TelegramClient, chat_id: int) -> Any:
    try:
        return await client.get_entity(chat_id)
    except Exception:
        async for dialog in client.iter_dialogs():
            if int(dialog.id) == int(chat_id):
                return dialog.entity
        raise ValueError(f"Группа {chat_id} не найдена в диалогах аккаунта")


# ============================================================
# ШАБЛОНЫ
# ============================================================

class TemplatePoolEmptyError(Exception):
    pass


def choose_random_template(state: dict[str, Any]) -> dict[str, int]:
    usage = state.setdefault("template_usage", {})
    last_key = state.get("last_template_key")

    available = []
    for template in state["group_templates"]:
        key = make_template_key(template["chat_id"], template["message_id"])
        if int(usage.get(key, 0)) >= MAX_TEMPLATE_SENDS:
            continue
        if key == last_key:
            continue
        available.append(template)

    if not available:
        raise TemplatePoolEmptyError

    return random.choice(available)


async def send_random_template(
    client: TelegramClient,
    recipient_entity: Any,
    state: dict[str, Any],
) -> None:
    template = choose_random_template(state)
    source = await resolve_dialog_entity(client, int(template["chat_id"]))

    sent = await client.forward_messages(
        recipient_entity,
        int(template["message_id"]),
        from_peer=source,
    )
    if not sent:
        raise RuntimeError("Шаблон не был переслан")

    key = make_template_key(template["chat_id"], template["message_id"])
    state["template_usage"][key] = int(state["template_usage"].get(key, 0)) + 1
    state["last_template_key"] = key


# ============================================================
# РАССЫЛКА (ПАРАЛЛЕЛЬНАЯ)
# ============================================================

def build_recipient_jobs(
    state: dict[str, Any],
    account_ids: list[str],
) -> list[tuple[str, str | int, str]]:
    """
    Возвращает (account_id, recipient, source).
    """
    if not account_ids:
        return []

    jobs: list[tuple[str, str | int, str]] = []
    seen: set[str] = set()
    owners = state.setdefault("recipient_owners", {})

    # Сначала индивидуальные списки.
    for account_id in account_ids:
        for recipient in state["individual_recipients"].get(account_id, []):
            key = normalize_recipient(recipient)
            if not key or key in seen:
                continue

            previous_owner = owners.get(key)
            if previous_owner and previous_owner != account_id:
                continue

            seen.add(key)
            jobs.append((account_id, recipient, "individual"))

    # Затем общий список.
    rr_index = 0
    for recipient in state["common_recipients"]:
        key = normalize_recipient(recipient)
        if not key or key in seen:
            continue

        previous_owner = owners.get(key)
        if previous_owner in account_ids:
            account_id = previous_owner
        else:
            account_id = account_ids[rr_index % len(account_ids)]
            rr_index += 1

        seen.add(key)
        jobs.append((account_id, recipient, "common"))

    return jobs


def remove_failed_recipient(
    state: dict[str, Any],
    account_id: str,
    recipient: str | int,
    source: str,
) -> None:
    if source == "common":
        target = state["common_recipients"]
    else:
        target = state["individual_recipients"].setdefault(account_id, [])

    while recipient in target:
        target.remove(recipient)


async def send_log_to_user(
    bot: Bot,
    chat_id: int,
    account_id: str,
    account_name: str,
    recipient: str | int,
    status: str,
    error_reason: str = "",
    total_remaining: int = 0,
) -> None:
    """Отправляет лог пользователю о попытке отписки."""
    
    status_emoji = "✅" if status == "success" else "❌"
    status_text = "УСПЕШНО" if status == "success" else "ОШИБКА"
    
    log_text = f"{status_emoji} <b>{status_text}</b>\n\n"
    log_text += f"Аккаунт: <b>{html.escape(account_name)}</b>\n"
    log_text += f"Получатель: <code>{html.escape(str(recipient))}</code>\n"
    
    if status == "success":
        log_text += f"\n✅ Отписка выполнена успешно!"
    else:
        log_text += f"\n❌ Причина: <code>{html.escape(error_reason)}</code>\n"
        log_text += f"⚠️ Получатель НЕ удалён из списка"
    
    log_text += f"\n\n📊 <b>Статистика:</b>\n"
    log_text += f"Осталось получателей: <b>{total_remaining}</b>"
    
    try:
        await bot.send_message(chat_id, log_text)
    except Exception as e:
        log.error(f"Не удалось отправить лог пользователю: {e}")


async def run_broadcast(bot: Bot, user_id: int, chat_id: int) -> None:
    stop_event = stop_events.setdefault(user_id, asyncio.Event())
    stop_event.clear()

    sent = 0
    failed = 0
    skipped = 0
    unsubscribed = 0

    try:
        state = load_state(user_id)
        available_accounts = await authorized_accounts(user_id)

        if not available_accounts:
            await bot.send_message(chat_id, "Нет ни одного подключённого аккаунта.")
            return

        account_map = {
            account_id: (account, client)
            for account_id, account, client in available_accounts
        }
        account_ids = list(account_map)

        jobs = build_recipient_jobs(state, account_ids)
        if not jobs:
            await bot.send_message(chat_id, "Списки получателей пусты.")
            return

        if not state["messages"] and not state["group_templates"]:
            await bot.send_message(chat_id, "Нет сообщений или шаблонов.")
            return

        if state["group_templates"] and len(state["group_templates"]) < 2:
            await bot.send_message(
                chat_id,
                "❌ Для случайной пересылки нужно минимум 2 шаблона.",
                reply_markup=group_templates_keyboard(),
            )
            return

        total_recipients = len(jobs)
        
        await bot.send_message(
            chat_id,
            "▶️ <b>Рассылка запущена (ПАРАЛЛЕЛЬНЫЙ РЕЖИМ)</b>\n\n"
            f"Аккаунтов: <b>{len(account_ids)}</b>\n"
            f"Уникальных получателей: <b>{total_recipients}</b>\n"
            f"Общих: <b>{len(state['common_recipients'])}</b>\n"
            f"Пауза между пользователями: <b>{MIN_DELAY_SECONDS}–{MAX_DELAY_SECONDS} сек.</b>\n"
            f"Скорость печати: <b>{CHAR_TYPING_SPEED} сек/символ</b>",
        )

        # Создаём очередь заданий для каждого аккаунта
        account_jobs = {account_id: [] for account_id in account_ids}
        for job in jobs:
            account_id, recipient, source = job
            account_jobs[account_id].append((recipient, source))

        # Функция для обработки заданий одного аккаунта
        async def process_account_jobs(account_id: str, jobs_list: list) -> tuple[int, int, int]:
            local_sent = 0
            local_failed = 0
            local_skipped = 0
            
            account, client = account_map[account_id]
            account_name = account_label(account_id, account)
            
            for recipient, source in jobs_list:
                # Проверяем стоп-событие
                if stop_event.is_set():
                    break

                recipient_key = normalize_recipient(recipient)
                previous_owner = state["recipient_owners"].get(recipient_key)

                if previous_owner and previous_owner != account_id:
                    local_skipped += 1
                    continue

                try:
                    entity = await client.get_entity(recipient)

                    # Отправляем все сообщения с задержкой перед каждым
                    for text in state["messages"]:
                        modified_text = replace_letters_random(text)
                        typing_delay = calculate_typing_delay(modified_text)
                        
                        await asyncio.sleep(typing_delay)
                        await client.send_message(
                            entity,
                            modified_text,
                            link_preview=False,
                        )

                    if state["group_templates"]:
                        await send_random_template(client, entity, state)

                    # Успешная отписка
                    local_sent += 1

                    # Удаляем получателя из списков
                    if recipient in state["common_recipients"]:
                        state["common_recipients"].remove(recipient)
                    
                    for acc_id in state["individual_recipients"]:
                        if recipient in state["individual_recipients"][acc_id]:
                            state["individual_recipients"][acc_id].remove(recipient)
                    
                    state["recipient_owners"].pop(recipient_key, None)
                    save_state(user_id, state)

                    # Подсчитываем оставшихся получателей
                    remaining = len(state["common_recipients"]) + sum(
                        len(state["individual_recipients"].get(acc_id, []))
                        for acc_id in state["accounts"]
                    )

                    await send_log_to_user(
                        bot, chat_id, account_id, account_name, recipient,
                        "success",
                        "",
                        remaining
                    )

                except TemplatePoolEmptyError:
                    error_msg = "Закончились доступные шаблоны"
                    remaining = len(state["common_recipients"]) + sum(
                        len(state["individual_recipients"].get(acc_id, []))
                        for acc_id in state["accounts"]
                    )
                    await send_log_to_user(
                        bot, chat_id, account_id, account_name, recipient,
                        "error", error_msg,
                        remaining
                    )
                    break

                except errors.FloodWaitError as exc:
                    wait_seconds = int(exc.seconds)
                    if wait_seconds > MAX_FLOOD_WAIT_SECONDS:
                        error_msg = f"FloodWait: {wait_seconds} секунд (превышен лимит)"
                        remaining = len(state["common_recipients"]) + sum(
                            len(state["individual_recipients"].get(acc_id, []))
                            for acc_id in state["accounts"]
                        )
                        await send_log_to_user(
                            bot, chat_id, account_id, account_name, recipient,
                            "error", error_msg,
                            remaining
                        )
                        break
                    await asyncio.sleep(wait_seconds + 2)
                    # Повторяем попытку
                    continue

                except (errors.UserPrivacyRestrictedError, errors.ChatWriteForbiddenError,
                        errors.UsernameInvalidError, errors.UsernameNotOccupiedError,
                        errors.UserDeactivatedError, ValueError) as exc:
                    error_msg = str(exc)
                    remaining = len(state["common_recipients"]) + sum(
                        len(state["individual_recipients"].get(acc_id, []))
                        for acc_id in state["accounts"]
                    )
                    await send_log_to_user(
                        bot, chat_id, account_id, account_name, recipient,
                        "error", error_msg,
                        remaining
                    )
                    local_failed += 1
                    remove_failed_recipient(state, account_id, recipient, source)
                    save_state(user_id, state)

                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {str(exc)}"
                    remaining = len(state["common_recipients"]) + sum(
                        len(state["individual_recipients"].get(acc_id, []))
                        for acc_id in state["accounts"]
                    )
                    await send_log_to_user(
                        bot, chat_id, account_id, account_name, recipient,
                        "error", error_msg,
                        remaining
                    )
                    local_failed += 1
                    log.exception("Ошибка отправки account=%s recipient=%r", account_id, recipient)

                # Задержка МЕЖДУ пользователями для этого аккаунта
                await asyncio.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
            
            return local_sent, local_failed, local_skipped

        # Запускаем обработку для всех аккаунтов параллельно
        tasks = []
        for account_id in account_ids:
            if account_jobs[account_id]:  # Если есть задания для аккаунта
                task = asyncio.create_task(process_account_jobs(account_id, account_jobs[account_id]))
                tasks.append(task)

        # Ждём завершения всех задач
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Собираем результаты
        for result in results:
            if isinstance(result, tuple):
                s, f, sk = result
                sent += s
                failed += f
                skipped += sk
                unsubscribed += s
            elif isinstance(result, Exception):
                log.error(f"Ошибка в задаче: {result}")

        # Итоговый отчёт
        remaining = len(state["common_recipients"]) + sum(
            len(state["individual_recipients"].get(acc_id, []))
            for acc_id in state["accounts"]
        )
        
        await bot.send_message(
            chat_id,
            "✅ <b>Рассылка завершена (ПАРАЛЛЕЛЬНЫЙ РЕЖИМ)</b>\n\n"
            f"Успешно отписано: <b>{unsubscribed}</b>\n"
            f"Ошибок: <b>{failed}</b>\n"
            f"Пропущено пересечений: <b>{skipped}</b>\n"
            f"Осталось получателей: <b>{remaining}</b>",
            reply_markup=main_keyboard(),
        )

    except Exception as exc:
        log.exception("Критическая ошибка рассылки")
        await bot.send_message(
            chat_id,
            f"❌ Критическая ошибка: <code>{html.escape(str(exc))}</code>",
            reply_markup=main_keyboard(),
        )
    finally:
        broadcast_tasks.pop(user_id, None)
        stop_event.clear()


# ============================================================
# QR-ВХОД
# ============================================================

async def perform_qr_login(
    bot: Bot,
    chat_id: int,
    user_id: int,
    account_id: str,
) -> None:
    try:
        client = await rebuild_client(user_id, account_id)
        qr_login = await client.qr_login()

        qr = qrcode.QRCode(border=3)
        qr.add_data(qr_login.url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")

        buffer = BytesIO()
        image.save(buffer, format="PNG")

        sent_message = await bot.send_photo(
            chat_id,
            BufferedInputFile(buffer.getvalue(), filename="telegram_login_qr.png"),
            caption=(
                "<b>Вход по QR-коду</b>\n\n"
                "Telegram → Настройки → Устройства → Подключить устройство."
            ),
        )

        try:
            await qr_login.wait(timeout=120)
        except errors.SessionPasswordNeededError:
            flow_data.setdefault(user_id, {})["account_id"] = account_id
            user_steps[user_id] = "await_qr_2fa"
            await bot.send_message(chat_id, "Введите пароль двухэтапной аутентификации.")
            return

        await update_account_identity(user_id, account_id, client)
        await bot.send_message(
            chat_id,
            "✅ Аккаунт подключён.",
            reply_markup=main_keyboard(),
        )

        try:
            await sent_message.delete()
        except Exception:
            pass

    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "QR-код истёк. Запустите вход снова.")
    except Exception as exc:
        await bot.send_message(
            chat_id,
            f"Ошибка QR-входа: <code>{html.escape(str(exc))}</code>",
        )
    finally:
        qr_tasks.pop(user_id, None)


# ============================================================
# КОМАНДЫ И ГРУППЫ
# ============================================================

@router.message(CommandStart())
async def start_handler(message: Message, bot: Bot) -> None:
    load_state(message.from_user.id)
    await message.answer(
        "<b>Панель рассылки</b>",
        reply_markup=main_keyboard(),
    )
    await admin_log(bot, f"Запустил бота:\n{event_user_label(message)}")


@router.message(Command("menu"))
async def menu_handler(message: Message) -> None:
    user_steps.pop(message.from_user.id, None)
    flow_data.pop(message.from_user.id, None)
    await message.answer("Главное меню:", reply_markup=main_keyboard())


@router.message(Command("bind"))
async def bind_handler(message: Message) -> None:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Команду /bind нужно отправить в группе.")
        return

    state = load_state(message.from_user.id)
    group_id = int(message.chat.id)

    if group_id not in state["bound_groups"]:
        state["bound_groups"].append(group_id)
        save_state(message.from_user.id, state)

    await message.answer(
        "✅ Группа привязана. Используйте кнопку "
        "«Просканировать шаблоны» в разделе шаблонов."
    )


def owners_for_group(group_id: int) -> list[int]:
    owners = []
    if not DATA_DIR.exists():
        return owners

    for directory in DATA_DIR.iterdir():
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        owner_id = int(directory.name)
        state = load_state(owner_id)
        if int(group_id) in [int(item) for item in state["bound_groups"]]:
            owners.append(owner_id)

    return owners


@router.message(F.chat.type == ChatType.GROUP)
@router.message(F.chat.type == ChatType.SUPERGROUP)
async def capture_template(message: Message, bot: Bot) -> None:
    if message.text and message.text.startswith("/bind"):
        return

    text = (message.text or message.caption or "").strip()
    if not contains_required_phrase(text):
        return

    for owner_id in owners_for_group(message.chat.id):
        state = load_state(owner_id)
        ref = {
            "chat_id": int(message.chat.id),
            "message_id": int(message.message_id),
        }

        if ref in state["group_templates"]:
            continue

        state["group_templates"].append(ref)
        state["group_templates"] = state["group_templates"][-MAX_GROUP_TEMPLATES:]

        key = make_template_key(ref["chat_id"], ref["message_id"])
        state["template_usage"].setdefault(key, 0)
        save_state(owner_id, state)

        try:
            await bot.send_message(
                owner_id,
                "✅ <b>Шаблон добавлен</b>\n\n"
                f"Всего шаблонов: <b>{len(state['group_templates'])}</b>",
            )
        except Exception:
            pass


# ============================================================
# ПРИВАТНЫЙ ВВОД
# ============================================================

@router.message(F.chat.type == ChatType.PRIVATE)
async def private_input(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    step = user_steps.get(user_id)
    text = (message.text or "").strip()
    data = flow_data.setdefault(user_id, {})

    if not step:
        await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())
        return

    if step == "await_account_tag":
        if not text:
            await message.answer("Тег не может быть пустым.")
            return

        state = load_state(user_id)
        account_id = f"account_{state['next_account_number']}"
        state["next_account_number"] += 1
        state["accounts"][account_id] = {
            "tag": text[:50],
            "proxy": None,
            "telegram_id": None,
            "username": None,
            "first_name": None,
        }
        state["individual_recipients"][account_id] = []
        state["active_account_id"] = account_id
        save_state(user_id, state)

        user_steps.pop(user_id, None)
        await message.answer(
            "Аккаунт создан. Выберите протокол прокси:",
            reply_markup=proxy_protocol_keyboard(account_id),
        )
        return

    if step == "await_rename_account":
        account_id = data.get("account_id")
        state = load_state(user_id)
        if account_id not in state["accounts"]:
            await message.answer("Аккаунт не найден.")
            return
        state["accounts"][account_id]["tag"] = text[:50]
        save_state(user_id, state)
        user_steps.pop(user_id, None)
        await message.answer("✅ Тег изменён.", reply_markup=accounts_keyboard(state))
        return

    if step == "await_proxy":
        account_id = data.get("account_id")
        protocol = data.get("protocol")
        try:
            proxy = parse_proxy(text, protocol)
            state = load_state(user_id)
            state["accounts"][account_id]["proxy"] = proxy
            save_state(user_id, state)

            client = await rebuild_client(user_id, account_id)
            user_steps.pop(user_id, None)

            if await client.is_user_authorized():
                await update_account_identity(user_id, account_id, client)
                await message.answer(
                    "✅ Прокси подключён. Аккаунт уже авторизован.",
                    reply_markup=main_keyboard(),
                )
            else:
                await message.answer(
                    "✅ Прокси подключён. Выберите способ входа:",
                    reply_markup=login_method_keyboard(account_id),
                )
        except Exception as exc:
            await message.answer(
                f"Прокси не подключён:\n<code>{html.escape(str(exc))}</code>"
            )
        return

    if step == "await_phone":
        account_id = data.get("account_id")
        try:
            client = await rebuild_client(user_id, account_id)
            await client.send_code_request(text)
            data["phone"] = text
            user_steps[user_id] = "await_code"
            await message.answer("Код отправлен. Введите его цифрами.")
        except Exception as exc:
            await message.answer(
                f"Не удалось отправить код:\n<code>{html.escape(str(exc))}</code>"
            )
        return

    if step == "await_code":
        account_id = data.get("account_id")
        phone = data.get("phone")
        try:
            client = await get_client(user_id, account_id)
            await client.sign_in(phone=phone, code=text.replace(" ", ""))
            await update_account_identity(user_id, account_id, client)
            user_steps.pop(user_id, None)
            await message.answer("✅ Аккаунт подключён.", reply_markup=main_keyboard())
        except errors.SessionPasswordNeededError:
            user_steps[user_id] = "await_2fa"
            await message.answer("Введите пароль двухэтапной аутентификации.")
        except Exception as exc:
            await message.answer(
                f"Ошибка входа:\n<code>{html.escape(str(exc))}</code>"
            )
        return

    if step in {"await_2fa", "await_qr_2fa"}:
        account_id = data.get("account_id")
        try:
            client = await get_client(user_id, account_id)
            await client.sign_in(password=text)
            await update_account_identity(user_id, account_id, client)
            user_steps.pop(user_id, None)
            await message.answer("✅ Аккаунт подключён.", reply_markup=main_keyboard())
        except Exception as exc:
            await message.answer(
                f"Ошибка 2FA:\n<code>{html.escape(str(exc))}</code>"
            )
        return

    if step == "await_messages":
        if not text:
            await message.answer(
                "Сообщение не может быть пустым.",
                reply_markup=adding_messages_keyboard(),
            )
            return

        state = load_state(user_id)
        if len(state["messages"]) >= MAX_TEXT_MESSAGES:
            user_steps.pop(user_id, None)
            await message.answer(
                f"Достигнут лимит {MAX_TEXT_MESSAGES}.",
                reply_markup=messages_keyboard(),
            )
            return

        state["messages"].append(text)
        save_state(user_id, state)

        await message.answer(
            f"✅ Добавлено: {len(state['messages'])}/{MAX_TEXT_MESSAGES}\n"
            "Отправьте следующее или нажмите «Завершить».",
            reply_markup=adding_messages_keyboard(),
        )
        return

    if step == "await_common_recipients":
        state = load_state(user_id)
        values = parse_recipients(text)
        existing = {normalize_recipient(item) for item in state["common_recipients"]}
        added = 0

        for item in values:
            key = normalize_recipient(item)
            if key not in existing:
                state["common_recipients"].append(item)
                existing.add(key)
                added += 1

        save_state(user_id, state)
        user_steps.pop(user_id, None)
        await message.answer(
            f"✅ В общий список добавлено: {added}\n"
            f"Всего: {len(state['common_recipients'])}",
            reply_markup=recipients_keyboard(),
        )
        return

    if step == "await_individual_recipients":
        account_id = data.get("account_id")
        state = load_state(user_id)
        target = state["individual_recipients"].setdefault(account_id, [])
        values = parse_recipients(text)
        existing = {normalize_recipient(item) for item in target}
        added = 0

        for item in values:
            key = normalize_recipient(item)
            if key not in existing:
                target.append(item)
                existing.add(key)
                added += 1

        save_state(user_id, state)
        user_steps.pop(user_id, None)
        account = state["accounts"][account_id]
        await message.answer(
            f"✅ Для аккаунта <b>{html.escape(account['tag'])}</b> добавлено: {added}\n"
            f"Всего: {len(target)}",
            reply_markup=recipients_keyboard(),
        )
        return


# ============================================================
# CALLBACK: МЕНЮ
# ============================================================

@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery) -> None:
    user_steps.pop(callback.from_user.id, None)
    flow_data.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        "<b>Панель рассылки</b>",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


# ============================================================
# CALLBACK: АККАУНТЫ
# ============================================================

@router.callback_query(F.data == "accounts")
async def accounts_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    await callback.message.edit_text(
        f"<b>Аккаунты: {len(state['accounts'])}</b>\n\n"
        "У каждого аккаунта собственный тег, прокси, сессия "
        "и индивидуальный список получателей.",
        reply_markup=accounts_keyboard(state),
    )
    await callback.answer()


@router.callback_query(F.data == "add_account")
async def add_account_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "await_account_tag"
    await callback.message.edit_text(
        "Введите тег нового аккаунта.\n\n"
        "Например: <code>Основной</code> или <code>Аккаунт 2</code>",
        reply_markup=back_keyboard("accounts"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("select_account:"))
async def select_account_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    account_id = callback.data.split(":", 1)[1]
    state = load_state(user_id)
    account = state["accounts"].get(account_id)

    if not account:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    state["active_account_id"] = account_id
    save_state(user_id, state)

    proxy = account.get("proxy")
    proxy_name = proxy["type"].upper() if proxy else "не задан"
    individual_count = len(state["individual_recipients"].get(account_id, []))

    await callback.message.edit_text(
        "<b>Аккаунт</b>\n\n"
        f"Тег: <b>{html.escape(account.get('tag') or account_id)}</b>\n"
        f"Telegram ID: <code>{account.get('telegram_id') or 'не подключён'}</code>\n"
        f"Username: "
        f"{'@' + account['username'] if account.get('username') else 'нет'}\n"
        f"Прокси: <b>{proxy_name}</b>\n"
        f"Индивидуальных получателей: <b>{individual_count}</b>",
        reply_markup=account_actions_keyboard(account_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rename_account:"))
async def rename_account_callback(callback: CallbackQuery) -> None:
    account_id = callback.data.split(":", 1)[1]
    flow_data[callback.from_user.id] = {"account_id": account_id}
    user_steps[callback.from_user.id] = "await_rename_account"
    await callback.message.edit_text(
        "Введите новый тег аккаунта:",
        reply_markup=back_keyboard(f"select_account:{account_id}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account_proxy:"))
async def account_proxy_callback(callback: CallbackQuery) -> None:
    account_id = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        "<b>Выберите протокол прокси:</b>",
        reply_markup=proxy_protocol_keyboard(account_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("proxy_protocol:"))
async def proxy_protocol_callback(callback: CallbackQuery) -> None:
    _, account_id, protocol = callback.data.split(":", 2)
    flow_data[callback.from_user.id] = {
        "account_id": account_id,
        "protocol": protocol,
    }
    user_steps[callback.from_user.id] = "await_proxy"

    if protocol == "mtproto":
        format_text = "IP:ПОРТ:SECRET"
        example = "196.19.123.231:443:SECRET"
    else:
        format_text = "IP:ПОРТ:ЛОГИН:ПАРОЛЬ"
        example = "196.19.123.231:8000:xm4Wj1:D2mF2K"

    await callback.message.edit_text(
        f"<b>Введите {protocol.upper()}-прокси</b>\n\n"
        f"Формат: <code>{format_text}</code>\n"
        f"Пример: <code>{html.escape(example)}</code>",
        reply_markup=back_keyboard(f"select_account:{account_id}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("login_phone:"))
async def login_phone_callback(callback: CallbackQuery) -> None:
    account_id = callback.data.split(":", 1)[1]
    flow_data[callback.from_user.id] = {"account_id": account_id}
    user_steps[callback.from_user.id] = "await_phone"

    await callback.message.edit_text(
        "Введите номер телефона:\n<code>+79991234567</code>",
        reply_markup=back_keyboard(f"select_account:{account_id}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("login_qr:"))
async def login_qr_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    account_id = callback.data.split(":", 1)[1]

    old = qr_tasks.get(user_id)
    if old and not old.done():
        await callback.answer("QR-вход уже запущен", show_alert=True)
        return

    flow_data[user_id] = {"account_id": account_id}
    qr_tasks[user_id] = asyncio.create_task(
        perform_qr_login(bot, callback.message.chat.id, user_id, account_id)
    )
    await callback.answer("Создаю QR-код")


@router.callback_query(F.data.startswith("logout_account:"))
async def logout_account_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    account_id = callback.data.split(":", 1)[1]

    try:
        client = await get_client(user_id, account_id)
        await client.log_out()
        user_clients.pop((user_id, account_id), None)

        state = load_state(user_id)
        account = state["accounts"].get(account_id)
        if account:
            account["telegram_id"] = None
            account["username"] = None
            account["first_name"] = None
            save_state(user_id, state)

        await callback.message.edit_text(
            "Аккаунт отключён.",
            reply_markup=accounts_keyboard(state),
        )
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data.startswith("delete_account:"))
async def delete_account_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    account_id = callback.data.split(":", 1)[1]
    state = load_state(user_id)

    client = user_clients.pop((user_id, account_id), None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

    state["accounts"].pop(account_id, None)
    state["individual_recipients"].pop(account_id, None)

    state["recipient_owners"] = {
        key: owner
        for key, owner in state["recipient_owners"].items()
        if owner != account_id
    }

    if state.get("active_account_id") == account_id:
        state["active_account_id"] = next(iter(state["accounts"]), None)

    save_state(user_id, state)

    await callback.message.edit_text(
        "Аккаунт удалён.",
        reply_markup=accounts_keyboard(state),
    )
    await callback.answer()


# ============================================================
# CALLBACK: СООБЩЕНИЯ
# ============================================================

@router.callback_query(F.data == "messages")
async def messages_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    preview = "\n\n".join(
        f"<b>{index}.</b> {html.escape(text[:300])}"
        for index, text in enumerate(state["messages"], 1)
    ) or "Сообщения ещё не добавлены."

    await callback.message.edit_text(
        f"<b>Сообщения: {len(state['messages'])}/{MAX_TEXT_MESSAGES}</b>\n\n"
        f"{preview}",
        reply_markup=messages_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "add_messages")
async def add_messages_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    if len(state["messages"]) >= MAX_TEXT_MESSAGES:
        await callback.answer("Достигнут лимит", show_alert=True)
        return

    user_steps[callback.from_user.id] = "await_messages"
    await callback.message.edit_text(
        "Введите сообщение. После сохранения можно сразу отправить следующее.\n"
        "Когда закончите — нажмите «Завершить».",
        reply_markup=adding_messages_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "finish_messages")
async def finish_messages_callback(callback: CallbackQuery) -> None:
    user_steps.pop(callback.from_user.id, None)
    await callback.answer("Добавление завершено")
    await messages_callback(callback)


@router.callback_query(F.data == "delete_message")
async def delete_message_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    if not state["messages"]:
        await callback.answer("Удалять нечего", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(
            text=f"Удалить №{index + 1}",
            callback_data=f"delete_message_index:{index}",
        )]
        for index in range(len(state["messages"]))
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="messages")])

    await callback.message.edit_text(
        "Выберите сообщение:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_message_index:"))
async def delete_message_index_callback(callback: CallbackQuery) -> None:
    index = int(callback.data.split(":", 1)[1])
    state = load_state(callback.from_user.id)

    if 0 <= index < len(state["messages"]):
        state["messages"].pop(index)
        save_state(callback.from_user.id, state)

    await callback.answer("Удалено")
    await messages_callback(callback)


# ============================================================
# CALLBACK: ПОЛУЧАТЕЛИ
# ============================================================

@router.callback_query(F.data == "recipients")
async def recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)

    lines = [
        "<b>Получатели</b>",
        "",
        f"Общий список: <b>{len(state['common_recipients'])}</b>",
        "",
        "<b>Индивидуальные списки:</b>",
    ]

    if state["accounts"]:
        for account_id, account in state["accounts"].items():
            count = len(state["individual_recipients"].get(account_id, []))
            lines.append(
                f"• {html.escape(account_label(account_id, account))}: <b>{count}</b>"
            )
    else:
        lines.append("Нет аккаунтов.")

    lines.extend([
        "",
        "<b>Общий</b> — список распределяется между всеми аккаунтами.",
        "<b>Индивидуальный</b> — список используется только выбранным аккаунтом.",
        "",
        "Один и тот же получатель не отправляется двум разным аккаунтам.",
    ])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=recipients_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "recipient_mode")
async def recipient_mode_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "<b>Выберите режим добавления</b>\n\n"
        "<b>Индивидуальный</b> — список только для одного аккаунта.\n"
        "<b>Общий</b> — список для всех аккаунтов сразу.",
        reply_markup=recipient_mode_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "recipient_mode:common")
async def common_mode_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "await_common_recipients"
    await callback.message.edit_text(
        "Отправьте получателей через пробел, запятую или новую строку.\n\n"
        "Пример:\n<code>@user1, @user2\n123456789</code>",
        reply_markup=back_keyboard("recipients"),
    )
    await callback.answer()


@router.callback_query(F.data == "recipient_mode:individual")
async def individual_mode_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    if not state["accounts"]:
        await callback.answer("Сначала добавьте аккаунт", show_alert=True)
        return

    await callback.message.edit_text(
        "Выберите аккаунт по тегу и Telegram ID:",
        reply_markup=account_choice_keyboard(
            state,
            "choose_individual_account",
            back="recipients",
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("choose_individual_account:"))
async def choose_individual_account_callback(callback: CallbackQuery) -> None:
    account_id = callback.data.split(":", 1)[1]
    flow_data[callback.from_user.id] = {"account_id": account_id}
    user_steps[callback.from_user.id] = "await_individual_recipients"

    await callback.message.edit_text(
        "Отправьте получателей через пробел, запятую или новую строку.",
        reply_markup=back_keyboard("recipients"),
    )
    await callback.answer()


@router.callback_query(F.data == "clear_recipients_menu")
async def clear_recipients_menu_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    await callback.message.edit_text(
        "Что очистить?",
        reply_markup=clear_recipients_keyboard(state),
    )
    await callback.answer()


@router.callback_query(F.data == "clear_common_recipients")
async def clear_common_recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    state["common_recipients"] = []
    save_state(callback.from_user.id, state)
    await callback.answer("Общий список очищен")
    await recipients_callback(callback)


@router.callback_query(F.data == "clear_all_recipients")
async def clear_all_recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    state["common_recipients"] = []
    state["individual_recipients"] = {
        account_id: [] for account_id in state["accounts"]
    }
    state["recipient_owners"] = {}
    save_state(callback.from_user.id, state)
    await callback.answer("Все списки очищены")
    await recipients_callback(callback)


@router.callback_query(F.data == "choose_clear_individual")
async def choose_clear_individual_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    await callback.message.edit_text(
        "Выберите аккаунт:",
        reply_markup=account_choice_keyboard(
            state,
            "clear_individual_account",
            back="recipients",
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("clear_individual_account:"))
async def clear_individual_account_callback(callback: CallbackQuery) -> None:
    account_id = callback.data.split(":", 1)[1]
    state = load_state(callback.from_user.id)
    state["individual_recipients"][account_id] = []
    save_state(callback.from_user.id, state)
    await callback.answer("Индивидуальный список очищен")
    await recipients_callback(callback)


# ============================================================
# CALLBACK: ШАБЛОНЫ
# ============================================================

async def group_names(bot: Bot, group_ids: list[int]) -> str:
    if not group_ids:
        return "Нет привязанных групп."

    result = []
    for group_id in group_ids:
        try:
            chat = await bot.get_chat(group_id)
            result.append(f"• {html.escape(chat.title or str(group_id))}")
        except Exception:
            result.append(f"• {group_id}")
    return "\n".join(result)


@router.callback_query(F.data == "group_templates")
async def group_templates_callback(callback: CallbackQuery, bot: Bot) -> None:
    state = load_state(callback.from_user.id)
    names = await group_names(bot, state["bound_groups"])

    total_sends = sum(int(value) for value in state["template_usage"].values())
    exhausted = sum(
        1
        for template in state["group_templates"]
        if int(state["template_usage"].get(
            make_template_key(template["chat_id"], template["message_id"]),
            0,
        )) >= MAX_TEMPLATE_SENDS
    )

    await callback.message.edit_text(
        "<b>Шаблоны из групп</b>\n\n"
        f"Привязанные группы:\n{names}\n\n"
        f"Сохранено шаблонов: <b>{len(state['group_templates'])}</b>\n"
        f"Всего отправок шаблонов: <b>{total_sends}</b>\n"
        f"Исчерпали лимит: <b>{exhausted}</b>\n\n"
        "В базу попадают только сообщения, содержащие хотя бы одну из этих фраз:\n"
        "• <code>@WorldOfPoizon</code>\n"
        "• <code>18.06</code>\n"
        "• <code>Egor Sobolev</code>\n\n"
        "Каждому получателю пересылается один случайный шаблон. "
        "Он не повторяется два раза подряд и может быть использован "
        f"не более {MAX_TEMPLATE_SENDS} раз.\n\n"
        "Одинаковые по содержанию сообщения разрешены. "
        "Повторно не добавляется только то же сообщение группы.",
        reply_markup=group_templates_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "scan_group_templates")
async def scan_group_templates_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)

    if not state["bound_groups"]:
        await callback.answer("Сначала привяжите группу через /bind", show_alert=True)
        return

    accounts = await authorized_accounts(user_id)
    if not accounts:
        await callback.answer("Сначала подключите аккаунт", show_alert=True)
        return

    await callback.answer("Сканирование запущено")
    status = await callback.message.answer("🔎 Сканирую сообщения…")

    added = 0
    scanned = 0
    errors_count = 0

    existing_refs = {
        make_template_key(item["chat_id"], item["message_id"])
        for item in state["group_templates"]
    }

    for group_id in state["bound_groups"]:
        source = None
        source_client = None

        for _, _, client in accounts:
            try:
                source = await resolve_dialog_entity(client, int(group_id))
                source_client = client
                break
            except Exception:
                continue

        if source is None or source_client is None:
            errors_count += 1
            continue

        try:
            async for tg_message in source_client.iter_messages(
                source,
                limit=SCAN_MESSAGE_LIMIT,
            ):
                scanned += 1
                if not contains_required_phrase(tg_message.message or ""):
                    continue

                ref_key = make_template_key(group_id, tg_message.id)
                if ref_key in existing_refs:
                    continue

                state["group_templates"].append({
                    "chat_id": int(group_id),
                    "message_id": int(tg_message.id),
                })
                state["template_usage"].setdefault(ref_key, 0)
                existing_refs.add(ref_key)
                added += 1

                if len(state["group_templates"]) >= MAX_GROUP_TEMPLATES:
                    break
        except Exception:
            errors_count += 1
            log.exception("Ошибка сканирования группы %s", group_id)

    state["group_templates"] = state["group_templates"][-MAX_GROUP_TEMPLATES:]
    valid_keys = {
        make_template_key(item["chat_id"], item["message_id"])
        for item in state["group_templates"]
    }
    state["template_usage"] = {
        key: int(value)
        for key, value in state["template_usage"].items()
        if key in valid_keys
    }
    save_state(user_id, state)

    try:
        await status.delete()
    except Exception:
        pass

    total = len(state["group_templates"])
    if total < 2:
        await bot.send_message(
            callback.message.chat.id,
            "❌ <b>Недостаточно шаблонов</b>\n\n"
            f"Найдено: <b>{total}</b>. Нужно минимум 2.\n"
            "Пополните привязанную группу подходящими сообщениями.",
            reply_markup=group_templates_keyboard(),
        )
        return

    await bot.send_message(
        callback.message.chat.id,
        "✅ <b>Сканирование завершено</b>\n\n"
        f"Проверено сообщений: <b>{scanned}</b>\n"
        f"Добавлено новых шаблонов: <b>{added}</b>\n"
        f"Всего шаблонов: <b>{total}</b>\n"
        f"Ошибок групп: <b>{errors_count}</b>",
        reply_markup=group_templates_keyboard(),
    )


@router.callback_query(F.data == "clear_group_templates")
async def clear_group_templates_callback(callback: CallbackQuery, bot: Bot) -> None:
    state = load_state(callback.from_user.id)
    state["group_templates"] = []
    state["template_usage"] = {}
    state["last_template_key"] = None
    save_state(callback.from_user.id, state)
    await callback.answer("Шаблоны очищены")
    await group_templates_callback(callback, bot)


# ============================================================
# CALLBACK: СТАТУС И РАССЫЛКА
# ============================================================

@router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)
    accounts = await authorized_accounts(user_id)
    running = (
        user_id in broadcast_tasks
        and not broadcast_tasks[user_id].done()
    )

    total_individual = sum(
        len(items) for items in state["individual_recipients"].values()
    )
    
    total_recipients = len(state["common_recipients"]) + total_individual

    await callback.message.edit_text(
        "<b>Статус</b>\n\n"
        f"Аккаунтов в базе: <b>{len(state['accounts'])}</b>\n"
        f"Авторизовано: <b>{len(accounts)}</b>\n"
        f"Основных сообщений: <b>{len(state['messages'])}</b>\n"
        f"Общих получателей: <b>{len(state['common_recipients'])}</b>\n"
        f"Индивидуальных получателей: <b>{total_individual}</b>\n"
        f"Всего получателей: <b>{total_recipients}</b>\n"
        f"Шаблонов: <b>{len(state['group_templates'])}</b>\n"
        f"Рассылка: <b>{'идёт (параллельно)' if running else 'остановлена'}</b>\n\n"
        f"Скорость печати: <b>{CHAR_TYPING_SPEED} сек/символ</b>",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "start_broadcast")
async def start_broadcast_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id

    current = broadcast_tasks.get(user_id)
    if current and not current.done():
        await callback.answer("Рассылка уже идёт", show_alert=True)
        return

    task = asyncio.create_task(
        run_broadcast(bot, user_id, callback.message.chat.id)
    )
    broadcast_tasks[user_id] = task
    await callback.answer("Рассылка запущена в параллельном режиме")


@router.callback_query(F.data == "stop_broadcast")
async def stop_broadcast_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    task = broadcast_tasks.get(user_id)

    if not task or task.done():
        await callback.answer("Активной рассылки нет")
        return

    stop_events.setdefault(user_id, asyncio.Event()).set()
    await callback.answer("Остановка запрошена")


# ============================================================
# ЗАПУСК
# ============================================================

async def main() -> None:
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("Проверьте BOT_TOKEN")
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Проверьте API_ID и API_HASH")
    if MIN_DELAY_SECONDS < 1 or MAX_DELAY_SECONDS < MIN_DELAY_SECONDS:
        raise RuntimeError("Некорректная задержка")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await admin_log(bot, "Бот запущен.")
        await dp.start_polling(bot)
    finally:
        for task in list(qr_tasks.values()) + list(broadcast_tasks.values()):
            if not task.done():
                task.cancel()

        for client in list(user_clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())