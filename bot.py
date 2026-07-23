import logging
from datetime import datetime, timedelta
from typing import Dict, List
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
import re

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = "8911152200:AAF75Xn8M-9XoCBEOYRuq8azBknQxrnkLZ4"
OWNER_ID = 2010296191  # Ваш Telegram ID
BOT_ID = 8911152200  # ID вашего бота (не менять!)

# Хранилище
user_data: Dict[int, Dict] = {}

# Настройки
HIDE_AFTER_HOURS = 6
DELETE_AFTER_DAYS = 14

# ЭТАПЫ
STAGES: List[str] = [
    "Доставка",
    "СВ",
    "Залог",
    "Перерасчет СВ",
    "Перерасчет залога",
    "Лот по СВ",
    "Лот по залогу"
]

# КЛЮЧЕВЫЕ ФРАЗЫ
STAGE_PHRASES: Dict[int, List[str]] = {
    0: ["Добрый день, Ваш заказ прибыл к нам на склад в мск"],
    1: [
        "Сумма полностью возвратная т.е при уведомлении СДЭКа/Почты о получении товара клиентом сумма будет возвращена в полном объеме на номер карты (имя получателя и банк должен быть тот же, с которого была отправлена сумма)",
        "на разных складах товары, сумма за СВ та же"
    ],
    2: [
        "18-19МСК, также не получили реквизиты на возврат СВ (имя отправителя как в чеке и тот же банк)",
        "18-19МСК, возврат по реквизитам"
    ],
    3: ["перерасчет по СВ (отмена категории заказов до 100тыс₽), СВ на все заказы теперь"],
    4: ["Перерасчет по залогу (отмена категории заказов до 100тыс₽), залог на все заказы теперь"],
    5: ["клиент оплатил залоги и СВ на одну отправку, лот по которой уже закрыт, сейчас ТК ждет"],
    6: ["сумма к оплате на новый лот по залогу"]
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================== ФУНКЦИИ ==================
def check_stage_phrase(text: str) -> tuple:
    if not text:
        return None, None
    text_lower = text.lower()
    for stage_idx, phrases in STAGE_PHRASES.items():
        for phrase in phrases:
            if phrase.lower() in text_lower:
                return stage_idx, phrase
    return None, None

def get_active_clients() -> Dict[int, Dict]:
    active = {}
    for user_id, data in user_data.items():
        if data.get("status") != "inactive":
            active[user_id] = data
    return active

async def check_inactive_clients(app):
    while True:
        await asyncio.sleep(3600)
        now = datetime.now()
        for user_id, data in list(user_data.items()):
            if data.get("status") != "inactive":
                if now - data["last_active"] > timedelta(hours=HIDE_AFTER_HOURS):
                    data["status"] = "inactive"
                    data["hidden_at"] = now
        
        to_delete = []
        for user_id, data in user_data.items():
            if data.get("status") == "inactive":
                hidden_at = data.get("hidden_at", data["last_active"])
                if now - hidden_at > timedelta(days=DELETE_AFTER_DAYS):
                    to_delete.append(user_id)
        for user_id in to_delete:
            del user_data[user_id]

# ================== КОМАНДЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    keyboard = [
        [InlineKeyboardButton("📋 Актуальные", callback_data="show_active")],
        [InlineKeyboardButton("📂 Неактуальные", callback_data="show_inactive")]
    ]
    
    await update.message.reply_text(
        "🤖 Бот для отслеживания клиентов\n\n"
        "Отправьте клиенту ключевую фразу в личном чате",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("❌ Нет доступа")
        return
    
    if query.data == "show_active":
        active = get_active_clients()
        if not active:
            await query.edit_message_text("📭 Нет актуальных клиентов")
            return
        
        text = "📋 Актуальные клиенты:\n\n"
        for user_id, data in active.items():
            try:
                user = await context.bot.get_chat(user_id)
                name = user.username or user.first_name or str(user_id)
            except:
                name = str(user_id)
            stage_idx = data.get("stage", 0)
            stage_name = STAGES[stage_idx] if stage_idx < len(STAGES) else "✅ Завершён"
            text += f"• @{name} — {stage_name}\n"
        
        await query.edit_message_text(text)
    
    elif query.data == "show_inactive":
        inactive = {}
        for user_id, data in user_data.items():
            if data.get("status") == "inactive":
                inactive[user_id] = data
        
        if not inactive:
            await query.edit_message_text("📭 Нет неактуальных клиентов")
            return
        
        text = "📂 Неактуальные клиенты:\n\n"
        for user_id, data in inactive.items():
            try:
                user = await context.bot.get_chat(user_id)
                name = user.username or user.first_name or str(user_id)
            except:
                name = str(user_id)
            stage_idx = data.get("stage", 0)
            stage_name = STAGES[stage_idx] if stage_idx < len(STAGES) else "✅ Завершён"
            text += f"• @{name} — {stage_name}\n"
        
        await query.edit_message_text(text)

# ================== ОСНОВНОЙ ОБРАБОТЧИК ==================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает сообщения из бизнес-чатов (личные чаты с клиентами)
    """
    msg = None
    
    # Проверяем, что это бизнес-сообщение (пришло из бизнес-чата)
    if update.business_message:
        msg = update.business_message
        logger.info("📩 Получено BUSINESS_MESSAGE")
    elif update.message:
        # Обычное сообщение - игнорируем, если это не чат с ботом
        if update.message.chat.type == "private" and update.message.from_user.id == OWNER_ID:
            # Это сообщение в чате с ботом - игнорируем
            logger.info("📩 Сообщение в чате с ботом - игнорируем")
            return
        msg = update.message
        logger.info("📩 Получено MESSAGE (не бизнес)")
    else:
        return
    
    if not msg:
        return
    
    # Проверяем, что сообщение от владельца
    if not msg.from_user or msg.from_user.id != OWNER_ID:
        logger.info(f"❌ Сообщение не от владельца (от {msg.from_user.id}), игнорируем")
        return
    
    if not msg.text:
        logger.info("❌ Нет текста")
        return
    
    text = msg.text
    
    logger.info(f"🔍 Текст: {text[:50]}...")
    
    # Проверяем ключевую фразу
    stage_idx, found_phrase = check_stage_phrase(text)
    
    if stage_idx is None:
        logger.info("❌ Ключевая фраза НЕ найдена")
        return
    
    logger.info(f"✅ Найдена фраза для этапа {stage_idx}: {STAGES[stage_idx]}")
    
    # Определяем клиента
    client_id = None
    
    # 1. Если есть реплай - берём ID из реплая
    if msg.reply_to_message and msg.reply_to_message.from_user:
        client_id = msg.reply_to_message.from_user.id
        logger.info(f"   Клиент по реплаю: {client_id}")
    
    # 2. Если нет реплая, ищем @username в тексте
    if client_id is None and msg.entities:
        for entity in msg.entities:
            if entity.type == "mention":
                username = text[entity.offset:entity.offset + entity.length]
                logger.info(f"   Найдено упоминание: {username}")
                try:
                    user = await context.bot.get_chat(username)
                    client_id = user.id
                    logger.info(f"   Клиент по username: {client_id}")
                    break
                except Exception as e:
                    logger.error(f"   Ошибка: {e}")
            elif entity.type == "text_mention":
                client_id = entity.user.id
                logger.info(f"   Клиент по text_mention: {client_id}")
                break
    
    # 3. В бизнес-чате получатель - это другой участник
    if client_id is None and update.business_message:
        # В бизнес-чате мы можем получить получателя через chat
        chat = msg.chat
        if chat.type == "private":
            # В личном чате получатель - это другой пользователь
            # Но мы не можем его определить напрямую
            # Поэтому используем chat.id (это ID клиента в личном чате)
            if chat.id != OWNER_ID and chat.id != BOT_ID:
                client_id = chat.id
                logger.info(f"   Клиент по chat.id: {client_id}")
    
    # 4. Если всё ещё None - пробуем получить из контекста
    if client_id is None:
        # В бизнес-чате это может быть личный чат
        if msg.chat.type == "private" and msg.chat.id != OWNER_ID:
            client_id = msg.chat.id
            logger.info(f"   Клиент по chat.id (private): {client_id}")
    
    if client_id is None:
        logger.error("❌ НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ КЛИЕНТА")
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text="⚠️ Не удалось определить клиента. Используйте реплай на сообщение клиента или @username"
        )
        return
    
    # Пропускаем, если клиент - это бот или сам владелец
    if client_id == BOT_ID:
        logger.warning("⚠️ Клиент определён как БОТ, игнорируем")
        return
    
    if client_id == OWNER_ID:
        logger.warning("⚠️ Клиент определён как ВЛАДЕЛЕЦ, игнорируем")
        return
    
    logger.info(f"✅ КЛИЕНТ ОПРЕДЕЛЁН: {client_id}")
    
    # Обновляем данные
    now = datetime.now()
    
    if client_id not in user_data:
        user_data[client_id] = {
            "stage": stage_idx,
            "status": "active",
            "last_active": now,
            "hidden_at": None
        }
        logger.info(f"✅ НОВЫЙ КЛИЕНТ добавлен, этап {STAGES[stage_idx]}")
    else:
        current_stage = user_data[client_id].get("stage", 0)
        if stage_idx > current_stage:
            user_data[client_id]["stage"] = stage_idx
            logger.info(f"⬆️ КЛИЕНТ перешёл на этап {STAGES[stage_idx]}")
        
        user_data[client_id]["last_active"] = now
        user_data[client_id]["status"] = "active"
        user_data[client_id]["hidden_at"] = None
        logger.info(f"✅ Обновлено время активности")
    
    # Уведомление владельцу
    try:
        user = await context.bot.get_chat(client_id)
        name = user.username or user.first_name or str(client_id)
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"✅ Клиент @{name}\n"
                 f"Этап: {STAGES[user_data[client_id]['stage']]}"
        )
        logger.info(f"📨 Уведомление отправлено владельцу")
    except Exception as e:
        logger.error(f"❌ Ошибка уведомления: {e}")

# ================== ЗАПУСК ==================
async def post_init(app):
    asyncio.create_task(check_inactive_clients(app))
    logger.info("🔄 Фоновая проверка запущена")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Обработчик ВСЕХ сообщений
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    
    # Устанавливаем команды
    app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
    ])
    
    print("\n" + "="*60)
    print("🚀 БОТ ЗАПУЩЕН")
    print("="*60)
    print(f"👤 Владелец: {OWNER_ID}")
    print(f"🤖 Бот ID: {BOT_ID}")
    print(f"🔑 Токен: {BOT_TOKEN[:15]}...")
    print("="*60)
    print("\n📌 Инструкция:")
    print("   1. Подключите бота в Telegram Business")
    print("   2. Включите Secretary Mode у бота")
    print("   3. Отправьте клиенту сообщение с ключевой фразой")
    print("   4. Используйте РЕПЛАЙ на сообщение клиента")
    print("="*60 + "\n")
    
    app.run_polling(allowed_updates=["message", "business_message", "callback_query"])

if __name__ == "__main__":
    main()