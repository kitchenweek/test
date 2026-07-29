import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, errors


# ============================================================
# НАСТРОЙКИ — ЗАПОЛНИТЕ ПЕРЕД ЗАПУСКОМ
# ============================================================

BOT_TOKEN = "8623083352:AAHPhZkAFymFxs272OO_YYECCeXQUXfH8is"

# Администратор видит логи действий пользователей.
ADMIN_ID = 2010296191

# Telegram API приложения. Получить на my.telegram.org
API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"

MIN_DELAY_SECONDS = 8
MAX_DELAY_SECONDS = 15
MAX_FLOOD_WAIT_SECONDS = 300
DEFAULT_DRY_RUN = True

DATA_DIR = Path("users_data")

# ============================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("telegram-console")

router = Router()

# Временные шаги интерфейса хранятся только в памяти.
user_steps: dict[int, str] = {}
pending_phones: dict[int, str] = {}

# Отдельный Telethon-клиент и задача рассылки для каждого пользователя.
user_clients: dict[int, TelegramClient] = {}
broadcast_tasks: dict[int, asyncio.Task] = {}
stop_events: dict[int, asyncio.Event] = {}


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
        "proxy": {
            "enabled": False,
            "type": "socks5",
            "host": "",
            "port": 1080,
            "username": "",
            "password": "",
            "rdns": True,
        },
        "recipients": [],
        "message": "",
        "dry_run": DEFAULT_DRY_RUN,
    }


def load_state(user_id: int) -> dict[str, Any]:
    path = state_path(user_id)
    if not path.exists():
        state = default_state()
        save_state(user_id, state)
        return state

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не удалось прочитать состояние пользователя %s", user_id)
        data = {}

    defaults = default_state()
    defaults.update(data)
    defaults["proxy"] = {**default_state()["proxy"], **data.get("proxy", {})}
    return defaults


def save_state(user_id: int, state: dict[str, Any]) -> None:
    state_path(user_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def admin_log(bot: Bot, text: str) -> None:
    if ADMIN_ID <= 0:
        return

    try:
        await bot.send_message(
            ADMIN_ID,
            f"<b>Лог</b>\n{text}",
        )
    except Exception:
        log.exception("Не удалось отправить лог администратору")


def user_label(event: Message | CallbackQuery) -> str:
    user = event.from_user
    if not user:
        return "неизвестный пользователь"

    username = f"@{user.username}" if user.username else "без username"
    name = (user.full_name or "").strip()
    return f"{name} | {username} | <code>{user.id}</code>"


def proxy_for_telethon(user_id: int):
    state = load_state(user_id)
    proxy = state["proxy"]

    if not proxy.get("enabled"):
        return None

    proxy_type = str(proxy.get("type", "socks5")).lower()
    if proxy_type not in {"socks5", "socks4", "http"}:
        raise ValueError("Тип прокси должен быть socks5, socks4 или http")

    host = str(proxy.get("host", "")).strip()
    port = int(proxy.get("port", 0))

    if not host or not port:
        raise ValueError("Не указан адрес или порт прокси")

    return (
        proxy_type,
        host,
        port,
        bool(proxy.get("rdns", True)),
        proxy.get("username") or None,
        proxy.get("password") or None,
    )


async def rebuild_user_client(user_id: int) -> TelegramClient:
    old = user_clients.get(user_id)
    if old is not None:
        try:
            await old.disconnect()
        except Exception:
            pass

    client = TelegramClient(
        session_path(user_id),
        API_ID,
        API_HASH,
        proxy=proxy_for_telethon(user_id),
        device_model="Desktop",
        system_version="Windows 11",
        app_version="1.0",
        lang_code="ru",
        system_lang_code="ru-RU",
    )
    await client.connect()
    user_clients[user_id] = client
    return client


async def get_user_client(user_id: int) -> TelegramClient:
    client = user_clients.get(user_id)

    if client is None:
        return await rebuild_user_client(user_id)

    if not client.is_connected():
        await client.connect()

    return client


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Аккаунт", callback_data="account"),
                InlineKeyboardButton(text="🌐 Прокси", callback_data="proxy"),
            ],
            [
                InlineKeyboardButton(text="👥 Получатели", callback_data="recipients"),
                InlineKeyboardButton(text="📝 Сообщение", callback_data="message"),
            ],
            [
                InlineKeyboardButton(text="🧪 Тестовый режим", callback_data="toggle_dry"),
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
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
        ]
    )


def account_keyboard(authorized: bool) -> InlineKeyboardMarkup:
    rows = []

    if authorized:
        rows.append([InlineKeyboardButton(text="🚪 Выйти", callback_data="logout")])
    else:
        rows.append([InlineKeyboardButton(text="🔐 Войти", callback_data="login")])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def proxy_keyboard(user_id: int) -> InlineKeyboardMarkup:
    enabled = bool(load_state(user_id)["proxy"]["enabled"])

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Включён" if enabled else "⚪ Выключен",
                    callback_data="toggle_proxy",
                )
            ],
            [InlineKeyboardButton(text="⚙️ Настроить прокси", callback_data="set_proxy")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def recipients_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data="add_recipient"),
                InlineKeyboardButton(text="🗑 Очистить", callback_data="clear_recipients"),
            ],
            [InlineKeyboardButton(text="📋 Показать", callback_data="show_recipients")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


async def account_status_text(user_id: int) -> tuple[str, bool]:
    try:
        client = await get_user_client(user_id)
        authorized = await client.is_user_authorized()

        if not authorized:
            return "Аккаунт не авторизован.", False

        me = await client.get_me()
        username = f"@{me.username}" if me.username else "без username"
        phone = getattr(me, "phone", None) or "скрыт"

        return (
            f"✅ Аккаунт авторизован\n"
            f"ID: <code>{me.id}</code>\n"
            f"Username: {username}\n"
            f"Телефон: <code>{phone}</code>",
            True,
        )
    except Exception as exc:
        return f"Ошибка подключения: <code>{type(exc).__name__}: {exc}</code>", False


async def status_text(user_id: int) -> str:
    state = load_state(user_id)
    account_text, _ = await account_status_text(user_id)
    proxy = state["proxy"]

    proxy_text = (
        f"{proxy['type']}://{proxy['host']}:{proxy['port']}"
        if proxy["enabled"]
        else "выключен"
    )

    task = broadcast_tasks.get(user_id)
    running = task is not None and not task.done()

    return (
        f"<b>Статус</b>\n\n"
        f"{account_text}\n\n"
        f"Прокси: <code>{proxy_text}</code>\n"
        f"Получателей: <b>{len(state['recipients'])}</b>\n"
        f"Сообщение: {'задано' if state['message'].strip() else 'не задано'}\n"
        f"Тестовый режим: {'включён' if state['dry_run'] else 'выключен'}\n"
        f"Рассылка: {'выполняется' if running else 'остановлена'}"
    )


@router.message(CommandStart())
async def start_handler(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    load_state(user_id)

    await message.answer(
        "<b>Консоль рассылки</b>\n\n"
        "У каждого пользователя отдельные настройки, Telegram-сессия, "
        "прокси, получатели и текст.",
        reply_markup=main_keyboard(),
    )

    await admin_log(bot, f"Запуск бота пользователем:\n{user_label(message)}")


@router.message(Command("menu"))
async def menu_command(message: Message) -> None:
    user_steps.pop(message.from_user.id, None)
    await message.answer("Главное меню:", reply_markup=main_keyboard())


@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery) -> None:
    user_steps.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        "<b>Консоль рассылки</b>",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "account")
async def account_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    text, authorized = await account_status_text(user_id)

    await callback.message.edit_text(
        f"<b>Telegram-аккаунт</b>\n\n{text}",
        reply_markup=account_keyboard(authorized),
    )
    await callback.answer()


@router.callback_query(F.data == "login")
async def login_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    user_steps[user_id] = "login_phone"

    await callback.message.edit_text(
        "Отправьте номер телефона в международном формате.\n"
        "Пример: <code>+79991234567</code>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()

    await admin_log(bot, f"Начал вход в аккаунт:\n{user_label(callback)}")


@router.callback_query(F.data == "logout")
async def logout_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id

    try:
        client = await get_user_client(user_id)
        await client.log_out()

        await callback.message.edit_text(
            "Аккаунт отключён. Локальная сессия завершена.",
            reply_markup=account_keyboard(False),
        )
        await admin_log(bot, f"Вышел из аккаунта:\n{user_label(callback)}")

    except Exception as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)
        await admin_log(
            bot,
            f"Ошибка выхода пользователя:\n{user_label(callback)}\n"
            f"<code>{type(exc).__name__}: {exc}</code>",
        )
        return

    await callback.answer()


@router.callback_query(F.data == "proxy")
async def proxy_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)
    proxy = state["proxy"]

    password_text = "есть" if proxy.get("password") else "нет"

    text = (
        f"<b>Прокси</b>\n\n"
        f"Состояние: {'включён' if proxy['enabled'] else 'выключен'}\n"
        f"Тип: <code>{proxy['type']}</code>\n"
        f"Адрес: <code>{proxy['host'] or 'не задан'}</code>\n"
        f"Порт: <code>{proxy['port']}</code>\n"
        f"Логин: <code>{proxy['username'] or 'нет'}</code>\n"
        f"Пароль: <code>{password_text}</code>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=proxy_keyboard(user_id),
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_proxy")
async def toggle_proxy_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)

    state["proxy"]["enabled"] = not state["proxy"]["enabled"]
    save_state(user_id, state)

    try:
        await rebuild_user_client(user_id)
    except Exception as exc:
        state["proxy"]["enabled"] = False
        save_state(user_id, state)

        await callback.answer(f"Прокси не подключён: {exc}", show_alert=True)
        await admin_log(
            bot,
            f"Ошибка подключения прокси:\n{user_label(callback)}\n"
            f"<code>{type(exc).__name__}: {exc}</code>",
        )
        return

    status = "включил" if state["proxy"]["enabled"] else "выключил"
    await admin_log(bot, f"Пользователь {status} прокси:\n{user_label(callback)}")
    await proxy_callback(callback)


@router.callback_query(F.data == "set_proxy")
async def set_proxy_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "set_proxy"

    await callback.message.edit_text(
        "<b>Отправьте прокси одной строкой</b>\n\n"
        "Форматы:\n"
        "<code>socks5://host:port</code>\n"
        "<code>socks5://login:password@host:port</code>\n"
        "<code>http://login:password@host:port</code>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "recipients")
async def recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)

    await callback.message.edit_text(
        f"<b>Получатели</b>\n\nСейчас добавлено: <b>{len(state['recipients'])}</b>",
        reply_markup=recipients_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "add_recipient")
async def add_recipient_callback(callback: CallbackQuery) -> None:
    user_steps[callback.from_user.id] = "add_recipients"

    await callback.message.edit_text(
        "Отправьте получателей по одному в строке.\n\n"
        "Допустимо:\n"
        "<code>@username</code>\n"
        "<code>123456789</code>\n\n"
        "Добавляйте только пользователей и чаты, согласившиеся получать сообщения.",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clear_recipients")
async def clear_recipients_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)
    old_count = len(state["recipients"])

    state["recipients"] = []
    save_state(user_id, state)

    await callback.answer("Список очищен")
    await admin_log(
        bot,
        f"Очистил список получателей ({old_count}):\n{user_label(callback)}",
    )
    await recipients_callback(callback)


@router.callback_query(F.data == "show_recipients")
async def show_recipients_callback(callback: CallbackQuery) -> None:
    state = load_state(callback.from_user.id)
    recipients = state["recipients"]

    if not recipients:
        text = "Список пуст."
    else:
        shown = recipients[:100]
        text = "\n".join(
            f"{i}. <code>{r}</code>"
            for i, r in enumerate(shown, 1)
        )

        if len(recipients) > 100:
            text += f"\n\nПоказаны первые 100 из {len(recipients)}."

    await callback.message.edit_text(
        f"<b>Получатели</b>\n\n{text}",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "message")
async def message_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)
    user_steps[user_id] = "set_message"

    current = state["message"].strip()
    preview = current[:800] if current else "не задано"

    await callback.message.edit_text(
        f"<b>Текущее сообщение</b>\n\n{preview}\n\n"
        "Отправьте новый текст одним сообщением.",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_dry")
async def toggle_dry_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    state = load_state(user_id)

    state["dry_run"] = not state["dry_run"]
    save_state(user_id, state)

    await callback.answer(
        f"Тестовый режим {'включён' if state['dry_run'] else 'выключен'}"
    )
    await callback.message.edit_text(
        await status_text(user_id),
        reply_markup=main_keyboard(),
    )

    await admin_log(
        bot,
        f"{'Включил' if state['dry_run'] else 'Выключил'} тестовый режим:\n"
        f"{user_label(callback)}",
    )


@router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        await status_text(callback.from_user.id),
        reply_markup=main_keyboard(),
    )
    await callback.answer()


async def run_broadcast(bot: Bot, user_id: int, chat_id: int, label: str) -> None:
    state = load_state(user_id)
    stop_event = stop_events.setdefault(user_id, asyncio.Event())
    stop_event.clear()

    sent = 0
    failed = 0
    checked = 0

    try:
        client = await get_user_client(user_id)

        if not await client.is_user_authorized():
            await bot.send_message(chat_id, "Сначала войдите в Telegram-аккаунт.")
            return

        recipients = list(state["recipients"])
        text = state["message"].strip()
        dry_run = bool(state["dry_run"])

        if not recipients:
            await bot.send_message(chat_id, "Список получателей пуст.")
            return

        if not text:
            await bot.send_message(chat_id, "Текст сообщения не задан.")
            return

        await bot.send_message(
            chat_id,
            f"Рассылка запущена.\n"
            f"Получателей: {len(recipients)}\n"
            f"Режим: {'тестовый' if dry_run else 'отправка'}",
        )

        await admin_log(
            bot,
            f"Запуск рассылки:\n{label}\n"
            f"Получателей: <b>{len(recipients)}</b>\n"
            f"Режим: {'тестовый' if dry_run else 'отправка'}",
        )

        for index, recipient in enumerate(recipients, start=1):
            if stop_event.is_set():
                await bot.send_message(chat_id, "Рассылка остановлена.")
                await admin_log(bot, f"Рассылка остановлена пользователем:\n{label}")
                break

            try:
                entity = await client.get_entity(recipient)

                if dry_run:
                    checked += 1
                else:
                    await client.send_message(entity, text, link_preview=False)
                    sent += 1

            except errors.FloodWaitError as exc:
                wait_seconds = int(exc.seconds)

                await admin_log(
                    bot,
                    f"FloodWait у пользователя:\n{label}\n"
                    f"Пауза: <b>{wait_seconds}</b> сек.",
                )

                if wait_seconds > MAX_FLOOD_WAIT_SECONDS:
                    await bot.send_message(
                        chat_id,
                        f"Рассылка остановлена: Telegram запросил паузу "
                        f"{wait_seconds} секунд.",
                    )
                    break

                await bot.send_message(
                    chat_id,
                    f"Telegram запросил паузу {wait_seconds} секунд.",
                )
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
                log.exception("Ошибка отправки пользователем %s", user_id)
                await admin_log(
                    bot,
                    f"Ошибка отправки:\n{label}\n"
                    f"<code>{type(exc).__name__}: {exc}</code>",
                )

            if index % 20 == 0:
                await bot.send_message(
                    chat_id,
                    f"Прогресс: {index}/{len(recipients)}\n"
                    f"Отправлено: {sent}\n"
                    f"Проверено: {checked}\n"
                    f"Ошибок: {failed}",
                )

            if index < len(recipients):
                await asyncio.sleep(
                    random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                )

        await bot.send_message(
            chat_id,
            f"Готово.\n"
            f"Отправлено: {sent}\n"
            f"Проверено: {checked}\n"
            f"Ошибок: {failed}",
        )

        await admin_log(
            bot,
            f"Рассылка завершена:\n{label}\n"
            f"Отправлено: <b>{sent}</b>\n"
            f"Проверено: <b>{checked}</b>\n"
            f"Ошибок: <b>{failed}</b>",
        )

    except asyncio.CancelledError:
        await bot.send_message(chat_id, "Рассылка принудительно остановлена.")
        await admin_log(bot, f"Рассылка отменена:\n{label}")
        raise

    except Exception as exc:
        log.exception("Критическая ошибка рассылки пользователя %s", user_id)

        await bot.send_message(
            chat_id,
            f"Ошибка: <code>{type(exc).__name__}: {exc}</code>",
        )
        await admin_log(
            bot,
            f"Критическая ошибка рассылки:\n{label}\n"
            f"<code>{type(exc).__name__}: {exc}</code>",
        )

    finally:
        broadcast_tasks.pop(user_id, None)
        stop_event.clear()


@router.callback_query(F.data == "start_broadcast")
async def start_broadcast_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    task = broadcast_tasks.get(user_id)

    if task is not None and not task.done():
        await callback.answer("Рассылка уже запущена", show_alert=True)
        return

    task = asyncio.create_task(
        run_broadcast(
            bot,
            user_id,
            callback.message.chat.id,
            user_label(callback),
        )
    )
    broadcast_tasks[user_id] = task
    await callback.answer("Запущено")


@router.callback_query(F.data == "stop_broadcast")
async def stop_broadcast_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    task = broadcast_tasks.get(user_id)

    if task is None or task.done():
        await callback.answer("Активной рассылки нет")
        return

    stop_events.setdefault(user_id, asyncio.Event()).set()
    await callback.answer("Остановка запрошена")


def parse_proxy_url(value: str) -> dict[str, Any]:
    parsed = urlparse(value.strip())
    proxy_type = parsed.scheme.lower()

    if proxy_type not in {"socks5", "socks4", "http"}:
        raise ValueError("Поддерживаются socks5, socks4 и http")

    if not parsed.hostname or not parsed.port:
        raise ValueError("Не найден host или port")

    return {
        "enabled": True,
        "type": proxy_type,
        "host": parsed.hostname,
        "port": parsed.port,
        "username": parsed.username or "",
        "password": parsed.password or "",
        "rdns": True,
    }


@router.message()
async def text_input_handler(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    step = user_steps.get(user_id)
    text = (message.text or "").strip()

    if not text:
        await message.answer("Нужен текст.")
        return

    if step == "login_phone":
        try:
            client = await rebuild_user_client(user_id)
            await client.send_code_request(text)

            pending_phones[user_id] = text
            user_steps[user_id] = "login_code"

            try:
                await message.delete()
            except Exception:
                pass

            await message.answer(
                "Код отправлен в Telegram.\n"
                "Введите код цифрами."
            )

        except Exception as exc:
            await message.answer(
                f"Ошибка отправки кода: <code>{type(exc).__name__}: {exc}</code>"
            )
            await admin_log(
                bot,
                f"Ошибка отправки кода:\n{user_label(message)}\n"
                f"<code>{type(exc).__name__}: {exc}</code>",
            )
        return

    if step == "login_code":
        phone = pending_phones.get(user_id)

        if not phone:
            user_steps.pop(user_id, None)
            await message.answer("Номер телефона потерян. Начните вход заново.")
            return

        try:
            client = await get_user_client(user_id)

            try:
                await message.delete()
            except Exception:
                pass

            await client.sign_in(
                phone=phone,
                code=text.replace(" ", ""),
            )

            user_steps.pop(user_id, None)
            pending_phones.pop(user_id, None)

            account_text, authorized = await account_status_text(user_id)
            await message.answer(
                account_text,
                reply_markup=account_keyboard(authorized),
            )
            await admin_log(
                bot,
                f"Успешный вход в аккаунт:\n{user_label(message)}",
            )

        except errors.SessionPasswordNeededError:
            user_steps[user_id] = "login_2fa"
            await message.answer("Введите пароль двухэтапной аутентификации.")

        except Exception as exc:
            await message.answer(
                f"Ошибка входа: <code>{type(exc).__name__}: {exc}</code>"
            )
            await admin_log(
                bot,
                f"Ошибка входа:\n{user_label(message)}\n"
                f"<code>{type(exc).__name__}: {exc}</code>",
            )
        return

    if step == "login_2fa":
        try:
            client = await get_user_client(user_id)

            try:
                await message.delete()
            except Exception:
                pass

            await client.sign_in(password=text)

            user_steps.pop(user_id, None)
            pending_phones.pop(user_id, None)

            account_text, authorized = await account_status_text(user_id)
            await message.answer(
                account_text,
                reply_markup=account_keyboard(authorized),
            )
            await admin_log(
                bot,
                f"Успешный вход с 2FA:\n{user_label(message)}",
            )

        except Exception as exc:
            await message.answer(
                f"Ошибка 2FA: <code>{type(exc).__name__}: {exc}</code>"
            )
            await admin_log(
                bot,
                f"Ошибка 2FA:\n{user_label(message)}\n"
                f"<code>{type(exc).__name__}: {exc}</code>",
            )
        return

    if step == "set_proxy":
        try:
            state = load_state(user_id)
            state["proxy"] = parse_proxy_url(text)
            save_state(user_id, state)

            try:
                await message.delete()
            except Exception:
                pass

            await rebuild_user_client(user_id)
            user_steps.pop(user_id, None)

            await message.answer(
                "Прокси сохранён и подключён.",
                reply_markup=main_keyboard(),
            )

            proxy = state["proxy"]
            await admin_log(
                bot,
                f"Пользователь настроил прокси:\n{user_label(message)}\n"
                f"<code>{proxy['type']}://{proxy['host']}:{proxy['port']}</code>",
            )

        except Exception as exc:
            await message.answer(
                f"Ошибка прокси: <code>{type(exc).__name__}: {exc}</code>"
            )
            await admin_log(
                bot,
                f"Ошибка настройки прокси:\n{user_label(message)}\n"
                f"<code>{type(exc).__name__}: {exc}</code>",
            )
        return

    if step == "add_recipients":
        state = load_state(user_id)
        values: list[str | int] = []

        for line in text.splitlines():
            value = line.strip()
            if not value:
                continue

            if value.lstrip("-").isdigit():
                values.append(int(value))
            else:
                values.append(value)

        added = 0

        for value in values:
            if value not in state["recipients"]:
                state["recipients"].append(value)
                added += 1

        save_state(user_id, state)
        user_steps.pop(user_id, None)

        await message.answer(
            f"Добавлено: {added}\nВсего: {len(state['recipients'])}",
            reply_markup=main_keyboard(),
        )

        await admin_log(
            bot,
            f"Добавил получателей:\n{user_label(message)}\n"
            f"Новых: <b>{added}</b>\n"
            f"Всего: <b>{len(state['recipients'])}</b>",
        )
        return

    if step == "set_message":
        state = load_state(user_id)
        state["message"] = text
        save_state(user_id, state)
        user_steps.pop(user_id, None)

        await message.answer(
            "Текст сообщения сохранён.",
            reply_markup=main_keyboard(),
        )

        await admin_log(
            bot,
            f"Изменил текст рассылки:\n{user_label(message)}\n"
            f"Длина: <b>{len(text)}</b> символов",
        )
        return

    await message.answer(
        "Используйте кнопки меню.",
        reply_markup=main_keyboard(),
    )


async def main() -> None:
    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_БОТА" or not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в начале main.py")

    if ADMIN_ID <= 0:
        raise RuntimeError("Заполните ADMIN_ID в начале main.py")

    if API_ID <= 0 or API_HASH == "ВСТАВЬТЕ_API_HASH":
        raise RuntimeError("Заполните API_ID и API_HASH в начале main.py")

    if MIN_DELAY_SECONDS < 1 or MAX_DELAY_SECONDS < MIN_DELAY_SECONDS:
        raise RuntimeError("Проверьте задержки рассылки")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await admin_log(bot, "Бот запущен.")
        await dp.start_polling(bot)

    finally:
        for task in list(broadcast_tasks.values()):
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