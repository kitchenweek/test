# -*- coding: utf-8 -*-

import asyncio
import html
import json
import logging
import random
import time
import re
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

BOT_TOKEN = "ВСТАВЬ_НОВЫЙ_BOT_TOKEN"
ADMIN_ID = 2010296191

API_ID = 32200104
API_HASH = "ВСТАВЬ_API_HASH"

MIN_DELAY_SECONDS = 180
MAX_DELAY_SECONDS = 420

MAX_FLOOD_WAIT_SECONDS = 900
MAX_TEXT_TEMPLATES = 5
MAX_GROUP_TEMPLATES = 100

DATA_DIR = Path("users_data")

# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("safe-broadcast")

router = Router()

user_steps: dict[int, str] = {}
pending_phones: dict[int, str] = {}
pending_proxy: dict[int, dict[str, Any]] = {}
qr_tasks: dict[int, asyncio.Task] = {}

user_clients: dict[int, TelegramClient] = {}
broadcast_tasks: dict[int, asyncio.Task] = {}
stop_events: dict[int, asyncio.Event] = {}


# ============================================================
# ОБНАРУЖЕНИЕ СПАМ-БЛОКА
# ============================================================

SPAM_BLOCK_PHRASES = [
    "только взаимным контактам",
    "вы можете отправлять сообщения только взаимным контактам",
    "к сожалению, в данный момент",
    "ограничение на отправку сообщений",
    "spam block",
    "limited from posting",
    "restricted from posting",
]


def is_spam_block_message(text: str) -> bool:
    if not text:
        return False

    text_lower = text.lower()

    for phrase in SPAM_BLOCK_PHRASES:
        if phrase.lower() in text_lower:
            return True

    has_restriction = any(word in text_lower for word in [
        "ограничени", "заблокирован", "временн",
        "spam", "restricted", "limited", "block"
    ])

    has_sending = any(word in text_lower for word in [
        "отправк", "сообщен", "posting", "sending"
    ])

    return has_restriction and has_sending


async def handle_spam_block(user_id: int, bot: Bot, message: Message) -> None:
    """Уведомляет о спам-блоке и останавливает активную рассылку."""
    log.warning("Обнаружен возможный спам-блок у пользователя %s", user_id)

    stop_events.setdefault(user_id, asyncio.Event()).set()

    try:
        await bot.send_message(
            user_id,
            "⚠️ <b>Обнаружено ограничение Telegram</b>\n\n"
            "Активная рассылка остановлена.\n"
            "Проверьте состояние аккаунта вручную через @SpamBot.",
            reply_markup=main_keyboard(),
        )
    except Exception:
        log.exception("Не удалось уведомить пользователя о спам-блоке")

    await admin_log(
        bot,
        "⚠️ <b>Обнаружено ограничение Telegram</b>\n\n"
        f"Пользователь: {event_user_label(message)}\n"
        "Рассылка остановлена. Автоматические действия с @SpamBot не выполнялись.",
    )


# ============================================================
# ТРАНСЛИТЕРАЦИЯ
# ============================================================

def apply_transliteration(text: str) -> str:
    translit_map = {
        'а': 'a', 'А': 'A', 'с': 'c', 'С': 'C', 'е': 'e', 'Е': 'E',
        'о': 'o', 'О': 'O', 'р': 'p', 'Р': 'P', 'х': 'x', 'Х': 'X',
    }

    result = []
    for char in text:
        if char in translit_map and random.random() < 0.5:
            result.append(translit_map[char])
        else:
            result.append(char)

    return ''.join(result)


async def typing_speed_simulate(client, entity, text: str) -> None:
    delay = 0.09 * len(text)
    await asyncio.sleep(delay)


# ============================================================
# ОСНОВНЫЕ ФУНКЦИИ
# ============================================================

def user_dir(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(user_id: int) -> Path:
    return user_dir(user_id) / "state.json"


def session_path(user_id: int) -> str:
    return str(user_dir(user_id) / "telegram_user_session")


def default_state() -> dict[str, Any]:
    return {
        "proxy": None,
        "messages": [],
        "recipients": [],
        "bound_groups": [],
        "group_templates": [],
    }


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

    state = default_state()
    state.update(loaded)
    return state


def save_state(user_id: int, state: dict[str, Any]) -> None:
    state_path(user_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Подключить аккаунт", callback_data="account")],
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


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]]
    )


def login_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 По номеру", callback_data="login_phone"),
                InlineKeyboardButton(text="📷 По QR-коду", callback_data="login_qr"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def messages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить сообщение", callback_data="add_message")],
            [InlineKeyboardButton(text="🗑 Удалить сообщение", callback_data="delete_message")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def adding_messages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить", callback_data="finish_adding_messages")]
        ]
    )


def recipients_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить получателей", callback_data="add_recipients")],
            [InlineKeyboardButton(text="🗑 Очистить список", callback_data="clear_recipients")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def group_templates_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Очистить шаблоны", callback_data="clear_group_templates")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def parse_proxy(value: str) -> dict[str, Any]:
    value = value.strip()

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
            raise ValueError("Не найдены адрес или порт прокси")
        return {
            "type": scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "username": parsed.username or "",
            "password": parsed.password or "",
            "rdns": True,
        }

    if scheme == "mtproto":
        if not parsed.hostname or not parsed.port:
            raise ValueError("Не найдены адрес или порт MTProto-прокси")
        secret = parsed.username or parsed.path.lstrip("/")
        if not secret:
            raise ValueError(
                "Используйте mtproto://SECRET@HOST:PORT или "
                "tg://proxy?server=...&port=...&secret=..."
            )
        return {"type": "mtproto", "host": parsed.hostname, "port": parsed.port, "secret": secret}

    raise ValueError("Поддерживаются SOCKS5, SOCKS4, HTTP и MTProto")


def client_options(user_id: int) -> dict[str, Any]:
    state = load_state(user_id)
    proxy = state.get("proxy")
    if not proxy:
        raise ValueError("Сначала укажите прокси")

    common: dict[str, Any] = {
        "device_model": "Desktop",
        "system_version": "Windows 11",
        "app_version": "1.0",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    }

    if proxy["type"] == "mtproto":
        common["connection"] = ConnectionTcpMTProxyAbridged
        common["proxy"] = (proxy["host"], int(proxy["port"]), proxy["secret"])
    else:
        common["proxy"] = (
            proxy["type"], proxy["host"], int(proxy["port"]),
            bool(proxy.get("rdns", True)),
            proxy.get("username") or None,
            proxy.get("password") or None,
        )

    return common


async def rebuild_client(user_id: int) -> TelegramClient:
    old = user_clients.pop(user_id, None)
    if old:
        try:
            await old.disconnect()
        except Exception:
            pass

    client = TelegramClient(session_path(user_id), API_ID, API_HASH, **client_options(user_id))
    await client.connect()
    user_clients[user_id] = client
    return client


async def get_client(user_id: int) -> TelegramClient:
    client = user_clients.get(user_id)
    if client is None:
        return await rebuild_client(user_id)
    if not client.is_connected():
        await client.connect()
    return client


async def account_summary(user_id: int) -> tuple[str, bool]:
    state = load_state(user_id)
    if not state.get("proxy"):
        return "Прокси ещё не настроен.", False

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            return "Прокси сохранён, аккаунт не авторизован.", False

        me = await client.get_me()
        username = f"@{me.username}" if me.username else "без username"
        return (
            f"✅ Подключён: {html.escape(me.first_name or '')}\n"
            f"Username: {username}\n"
            f"ID: <code>{me.id}</code>",
            True,
        )
    except Exception as exc:
        return f"Ошибка подключения: <code>{html.escape(str(exc))}</code>", False


async def full_status(user_id: int) -> str:
    state = load_state(user_id)
    account, _ = await account_summary(user_id)
    task = broadcast_tasks.get(user_id)
    running = task is not None and not task.done()

    proxy = state.get("proxy")
    proxy_name = proxy["type"].upper() if proxy else "не задан"

    return (
        f"<b>Статус</b>\n\n{account}\n\n"
        f"Прокси: <b>{proxy_name}</b>\n"
        f"Основных сообщений: <b>{len(state['messages'])}/{MAX_TEXT_TEMPLATES}</b>\n"
        f"Получателей: <b>{len(state['recipients'])}</b>\n"
        f"Привязанных групп: <b>{len(state['bound_groups'])}</b>\n"
        f"Шаблонов группы: <b>{len(state['group_templates'])}</b>\n"
        f"Рассылка: <b>{'идёт' if running else 'остановлена'}</b>"
    )


async def send_group_templates(client: TelegramClient, recipient_entity: Any, templates: list[dict[str, int]]) -> int:
    sent = 0
    for template in templates:
        source_chat = int(template["chat_id"])
        message_id = int(template["message_id"])
        try:
            source_entity = await client.get_entity(source_chat)
            await client.forward_messages(recipient_entity, message_id, from_peer=source_entity)
            sent += 1
        except Exception:
            log.exception("Не удалось переслать шаблон %s/%s", source_chat, message_id)
    return sent


async def run_broadcast(bot: Bot, user_id: int, chat_id: int, label: str) -> None:
    state = load_state(user_id)
    stop_event = stop_events.setdefault(user_id, asyncio.Event())
    stop_event.clear()

    sent_recipients = 0
    failed = 0

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await bot.send_message(chat_id, "Сначала подключите Telegram-аккаунт.")
            return

        if not state["recipients"]:
            await bot.send_message(chat_id, "Список получателей пуст.")
            return

        if not state["messages"] and not state["group_templates"]:
            await bot.send_message(chat_id, "Нет ни одного сообщения или шаблона.")
            return

        await bot.send_message(
            chat_id,
            f"Рассылка запущена.\n"
            f"Получателей: {len(state['recipients'])}\n"
            f"Основных сообщений: {len(state['messages'])}\n"
            f"Шаблонов группы: {len(state['group_templates'])}\n"
            f"Пауза: {MIN_DELAY_SECONDS}–{MAX_DELAY_SECONDS} секунд.",
        )

        for index, recipient in enumerate(state["recipients"], 1):
            if stop_event.is_set():
                await bot.send_message(chat_id, "Рассылка остановлена.")
                break

            try:
                entity = await client.get_entity(recipient)

                for original_text in state["messages"]:
                    if stop_event.is_set():
                        break
                    prepared_text = apply_transliteration(original_text)
                    await typing_speed_simulate(client, entity, prepared_text)
                    await client.send_message(entity, prepared_text, link_preview=False)
                    await asyncio.sleep(1)

                if stop_event.is_set():
                    break

                await send_group_templates(client, entity, state["group_templates"])
                sent_recipients += 1

            except errors.FloodWaitError as exc:
                wait_seconds = int(exc.seconds)
                if wait_seconds > MAX_FLOOD_WAIT_SECONDS:
                    await bot.send_message(
                        chat_id,
                        f"Telegram запросил паузу {wait_seconds} секунд. Рассылка остановлена.",
                    )
                    break
                await asyncio.sleep(wait_seconds + 2)

            except (
                errors.UserPrivacyRestrictedError,
                errors.ChatWriteForbiddenError,
                errors.UsernameInvalidError,
                errors.UsernameNotOccupiedError,
                ValueError,
            ):
                failed += 1

            except Exception as exc:
                failed += 1
                log.exception("Ошибка рассылки пользователя %s", user_id)
                await admin_log(
                    bot,
                    f"Ошибка рассылки:\n{label}\n"
                    f"<code>{html.escape(type(exc).__name__ + ': ' + str(exc))}</code>",
                )

            if index % 10 == 0:
                await bot.send_message(
                    chat_id,
                    f"Прогресс: {index}/{len(state['recipients'])}\n"
                    f"Успешных получателей: {sent_recipients}\n"
                    f"Ошибок: {failed}",
                )

            if index < len(state["recipients"]):
                await asyncio.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

        await bot.send_message(
            chat_id,
            f"Готово.\nУспешных получателей: {sent_recipients}\nОшибок: {failed}",
            reply_markup=main_keyboard(),
        )

    except Exception as exc:
        log.exception("Критическая ошибка")
        await bot.send_message(
            chat_id,
            f"Ошибка: <code>{html.escape(str(exc))}</code>",
            reply_markup=main_keyboard(),
        )
    finally:
        broadcast_tasks.pop(user_id, None)
        stop_event.clear()


async def perform_qr_login(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        client = await rebuild_client(user_id)
        qr_login = await client.qr_login()

        qr = qrcode.QRCode(border=3)
        qr.add_data(qr_login.url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")

        buffer = BytesIO()
        image.save(buffer, format="PNG")

        sent = await bot.send_photo(
            chat_id,
            BufferedInputFile(buffer.getvalue(), filename="telegram_login_qr.png"),
            caption=(
                "<b>Вход по QR-коду</b>\n\n"
                "Откройте Telegram: Настройки → Устройства → Подключить устройство."
            ),
        )

        try:
            await qr_login.wait(timeout=120)
        except errors.SessionPasswordNeededError:
            user_steps[user_id] = "await_qr_2fa"
            await bot.send_message(chat_id, "Введите пароль двухэтапной аутентификации.")
            return

        me = await client.get_me()
        await bot.send_message(
            chat_id,
            f"✅ Аккаунт подключён: {html.escape(me.first_name or '')}",
            reply_markup=main_keyboard(),
        )

        try:
            await sent.delete()
        except Exception:
            pass

    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "Время QR-кода истекло.", reply_markup=main_keyboard())
    except Exception as exc:
        await bot.send_message(
            chat_id,
            f"Ошибка QR-входа: <code>{html.escape(str(exc))}</code>",
            reply_markup=main_keyboard(),
        )
    finally:
        qr_tasks.pop(user_id, None)


def owners_for_group(group_id: int) -> list[int]:
    owners: list[int] = []
    if not DATA_DIR.exists():
        return owners

    for directory in DATA_DIR.iterdir():
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        owner_id = int(directory.name)
        state = load_state(owner_id)
        if group_id in state["bound_groups"]:
            owners.append(owner_id)
    return owners


# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

@router.message(CommandStart())
async def start_handler(message: Message, bot: Bot) -> None:
    load_state(message.from_user.id)
    await message.answer(
        "<b>Панель рассылки</b>\n\nПеред входом в аккаунт бот обязательно запросит прокси.",
        reply_markup=main_keyboard(),
    )
    await admin_log(bot, f"Запустил бота:\n{event_user_label(message)}")


@router.message(Command("menu"))
async def menu_command(message: Message) -> None:
    user_steps.pop(message.from_user.id, None)
    await message.answer("Главное меню:", reply_markup=main_keyboard())


@router.message(Command("bind"))
async def bind_group_handler(message: Message, bot: Bot) -> None:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Команду /bind нужно отправить в группе.")
        return

    user_id = message.from_user.id
    state = load_state(user_id)

    if message.chat.id not in state["bound_groups"]:
        state["bound_groups"].append(message.chat.id)
        save_state(user_id, state)

    await message.answer(
        "✅ Группа привязана. Новые сообщения этой группы будут сохранены как шаблоны."
    )


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def capture_group_template(message: Message) -> None:
    if message.text and message.text.startswith("/bind"):
        return

    for owner_id in owners_for_group(message.chat.id):
        state = load_state(owner_id)
        ref = {"chat_id": message.chat.id, "message_id": message.message_id}

        if ref not in state["group_templates"]:
            state["group_templates"].append(ref)
            state["group_templates"] = state["group_templates"][-MAX_GROUP_TEMPLATES:]
            save_state(owner_id, state)


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_input_handler(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    if message.text and is_spam_block_message(message.text):
        await handle_spam_block(user_id, bot, message)
        return

    step = user_steps.get(user_id)
    text = (message.text or "").strip()

    if not step:
        await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())
        return

    if step == "await_proxy":
        try:
            proxy = parse_proxy(text)
            state = load_state(user_id)
            state["proxy"] = proxy
            save_state(user_id, state)
            pending_proxy[user_id] = proxy

            client = await rebuild_client(user_id)
            if await client.is_user_authorized():
                user_steps.pop(user_id, None)
                await message.answer("Прокси подключён. Аккаунт уже авторизован.", reply_markup=main_keyboard())
            else:
                user_steps.pop(user_id, None)
                await message.answer("✅ Прокси подключён. Выберите способ входа:", reply_markup=login_method_keyboard())
        except Exception as exc:
            await message.answer(f"Прокси не подключён:\n<code>{html.escape(str(exc))}</code>")
        return

    if step == "await_phone":
        try:
            client = await rebuild_client(user_id)
            await client.send_code_request(text)
            pending_phones[user_id] = text
            user_steps[user_id] = "await_code"
            await message.answer("Код отправлен. Введите его цифрами.")
        except Exception as exc:
            await message.answer(f"Не удалось отправить код:\n<code>{html.escape(str(exc))}</code>")
        return

    if step == "await_code":
        phone = pending_phones.get(user_id)
        if not phone:
            user_steps.pop(user_id, None)
            await message.answer("Начните вход заново.", reply_markup=main_keyboard())
            return
        try:
            client = await get_client(user_id)
            await client.sign_in(phone=phone, code=text.replace(" ", ""))
            pending_phones.pop(user_id, None)
            user_steps.pop(user_id, None)
            await message.answer("✅ Аккаунт подключён.", reply_markup=main_keyboard())
        except errors.SessionPasswordNeededError:
            user_steps[user_id] = "await_2fa"
            await message.answer("Введите пароль двухэтапной аутентификации.")
        except Exception as exc:
            await message.answer(f"Ошибка входа:\n<code>{html.escape(str(exc))}</code>")
        return

    if step in {"await_2fa", "await_qr_2fa"}:
        try:
            client = await get_client(user_id)
            await client.sign_in(password=text)
            user_steps.pop(user_id, None)
            pending_phones.pop(user_id, None)
            await message.answer("✅ Аккаунт подключён.", reply_markup=main_keyboard())
        except Exception as exc:
            await message.answer(f"Ошибка 2FA:\n<code>{html.escape(str(exc))}</code>")
        return

    if step == "await_messages_loop":
        if not text:
            await message.answer("Сообщение не может быть пустым.", reply_markup=adding_messages_keyboard())
            return

        state = load_state(user_id)
        if len(state["messages"]) >= MAX_TEXT_TEMPLATES:
            user_steps.pop(user_id, None)
            await message.answer(
                f"Достигнут лимит: {MAX_TEXT_TEMPLATES} сообщений.",
                reply_markup=messages_keyboard(),
            )
            return

        state["messages"].append(text)
        save_state(user_id, state)
        current_count = len(state["messages"])

        await message.answer(
            f"✅ Добавлено сообщение №{current_count}.\n\n"
            "Отправьте следующее сообщение или нажмите «Завершить».",
            reply_markup=adding_messages_keyboard(),
        )

        if current_count >= MAX_TEXT_TEMPLATES:
            user_steps.pop(user_id, None)
            await message.answer(
                f"Добавлено максимальное количество сообщений: {MAX_TEXT_TEMPLATES}.",
                reply_markup=messages_keyboard(),
            )
        return

    if step == "await_recipients":
        state = load_state(user_id)
        added = 0
        for line in text.splitlines():
            value = line.strip()
            if not value:
                continue
            parsed: str | int = int(value) if value.lstrip("-").isdigit() else value
            if parsed not in state["recipients"]:
                state["recipients"].append(parsed)
                added += 1

        save_state(user_id, state)
        user_steps.pop(user_id, None)
        await message.answer(
            f"Добавлено: {added}\nВсего: {len(state['recipients'])}",
            reply_markup=main_keyboard(),
        )


# ============================================================
# CALLBACK ХЕНДЛЕРЫ
# ============================================================

@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery) -> None:
    user_steps.pop(callback.from_user.id, None)
    await callback.message.edit_text("<b>Панель рассылки</b>", reply_markup=main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "account")
async def account_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    summary, authorized = await account_summary(user_id)

    if authorized:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚪 Выйти из аккаунта", callback_data="logout")],
                [InlineKeyboardButton(text="🔄 Переподключить", callback_data="begin_proxy")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
            ]
        )
        await callback.message.edit_text(f"<b>Аккаунт</b>\n\n{summary}", reply_markup=keyboard)
    else:
        await callback.message.edit_text(
            f"<b>Подключение аккаунта</b>\n\n{summary}\n\nСначала необходимо указать прокси.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Продолжить", callback_data="begin_proxy")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data == "begin_proxy")
async def begin_proxy_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "await_proxy"
    await callback.message.edit_text(
        "<b>Введите прокси</b>\n\n"
        "<code>socks5://login:password@host:port</code>\n"
        "<code>socks4://host:port</code>\n"
        "<code>http://login:password@host:port</code>\n"
        "<code>mtproto://SECRET@host:port</code>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "login_phone")
async def login_phone_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "await_phone"
    await callback.message.edit_text(
        "Введите номер телефона в международном формате:\n<code>+79991234567</code>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "login_qr")
async def login_qr_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    old_task = qr_tasks.get(user_id)
    if old_task and not old_task.done():
        await callback.answer("QR-код уже создан", show_alert=True)
        return

    qr_tasks[user_id] = asyncio.create_task(
        perform_qr_login(bot, callback.message.chat.id, user_id)
    )
    await callback.answer("Создаю QR-код")


@router.callback_query(F.data == "logout")
async def logout_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    try:
        client = await get_client(user_id)
        await client.log_out()
        user_clients.pop(user_id, None)
        await callback.message.edit_text("Аккаунт отключён.", reply_markup=main_keyboard())
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "messages")
async def messages_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    if state["messages"]:
        preview = "\n\n".join(
            f"<b>{index}.</b> {html.escape(text[:300])}"
            for index, text in enumerate(state["messages"], 1)
        )
    else:
        preview = "Сообщения ещё не добавлены."

    await callback.message.edit_text(
        f"<b>Основные сообщения: {len(state['messages'])}/{MAX_TEXT_TEMPLATES}</b>\n\n{preview}",
        reply_markup=messages_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "add_message")
async def add_message_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    if len(state["messages"]) >= MAX_TEXT_TEMPLATES:
        await callback.answer(f"Можно добавить максимум {MAX_TEXT_TEMPLATES} сообщений", show_alert=True)
        return

    user_steps[callback.from_user.id] = "await_messages_loop"
    await callback.message.edit_text(
        "<b>Добавление сообщений</b>\n\n"
        "Введите сообщение. После сохранения можно сразу отправить следующее.\n"
        "Когда закончите — нажмите «Завершить».",
        reply_markup=adding_messages_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "finish_adding_messages")
async def finish_adding_messages_callback(callback: CallbackQuery) -> None:
    user_steps.pop(callback.from_user.id, None)
    await callback.answer("Добавление завершено")
    await messages_callback(callback)


@router.callback_query(F.data == "delete_message")
async def delete_message_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    if not state["messages"]:
        await callback.answer("Удалять нечего", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=f"Удалить №{i}", callback_data=f"delmsg:{i - 1}")]
        for i in range(1, len(state["messages"]) + 1)
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="messages")])
    await callback.message.edit_text(
        "Выберите сообщение:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delmsg:"))
async def delete_selected_message(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    try:
        index = int(callback.data.split(":", 1)[1])
        state["messages"].pop(index)
        save_state(callback.from_user.id, state)
        await callback.answer("Удалено")
        await messages_callback(callback)
    except (ValueError, IndexError):
        await callback.answer("Сообщение уже отсутствует", show_alert=True)


@router.callback_query(F.data == "recipients")
async def recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    preview = "\n".join(
        f"{i}. <code>{html.escape(str(item))}</code>"
        for i, item in enumerate(state["recipients"][:30], 1)
    ) or "Список пуст."

    await callback.message.edit_text(
        f"<b>Получатели: {len(state['recipients'])}</b>\n\n{preview}",
        reply_markup=recipients_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "add_recipients")
async def add_recipients_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "await_recipients"
    await callback.message.edit_text(
        "Отправьте @username, ссылки или ID — по одному в строке.",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clear_recipients")
async def clear_recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    state["recipients"] = []
    save_state(callback.from_user.id, state)
    await callback.answer("Список очищен")
    await recipients_callback(callback)


@router.callback_query(F.data == "group_templates")
async def group_templates_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    groups = "\n".join(f"• <code>{group_id}</code>" for group_id in state["bound_groups"]) or "Нет привязанных групп."

    await callback.message.edit_text(
        f"<b>Шаблоны из групп</b>\n\n"
        f"Привязанные группы:\n{groups}\n\n"
        f"Сохранено сообщений: <b>{len(state['group_templates'])}</b>",
        reply_markup=group_templates_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clear_group_templates")
async def clear_group_templates_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    state["group_templates"] = []
    save_state(callback.from_user.id, state)
    await callback.answer("Шаблоны очищены")
    await group_templates_callback(callback)


@router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        await full_status(callback.from_user.id),
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
        run_broadcast(bot, user_id, callback.message.chat.id, event_user_label(callback))
    )
    broadcast_tasks[user_id] = task
    await callback.answer("Рассылка запущена")


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
    if ADMIN_ID <= 0 or API_ID <= 0 or not API_HASH:
        raise RuntimeError("Проверьте ADMIN_ID, API_ID и API_HASH")
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