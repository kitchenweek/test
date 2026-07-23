import logging
from datetime import datetime, timedelta
from typing import Dict, List
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
import json

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = "8911152200:AAF75Xn8M-9XoCBEOYRuq8azBknQxrnkLZ4"
OWNER_ID = 2010296191  # Ваш Telegram ID

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
    1: ["Сумма полностью возвратная т.е при уведомлении СДЭКа/Почты о получении товара клиентом сумма будет возвращена в полном объеме на номер карты (имя получателя и банк должен быть тот же, с которого была отправлена сумма)", "на разных складах товары, сумма за СВ та же"],
    2: ["18-19МСК, также не получили реквизиты на возврат СВ (имя отправителя как в чеке и тот же банк)", "18-19МСК, возврат по реквизитам"],
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
                    try:
                        user = await app.bot.get_chat(user_id)
                        name = user.username or user.first_name or str(user_id)
                        await app.bot.send_message(
                            chat_id=OWNER_ID,
                            text=f"⏰ Клиент @{name} стал неактуальным"
                        )
                    except:
                        pass
        
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
        "Отправьте клиенту ключевую фразу",
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
        
        text = "📋 Актуальные:\n\n"
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
        
        text = "📂 Неактуальные:\n\n"
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

# ================== ОБРАБОТЧИК ВСЕХ СООБЩЕНИЙ ==================
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ВСЕ сообщения и выводит отладку"""
    
    # Получаем сообщение
    msg = None
    msg_type = "unknown"
    
    if update.message:
        msg = update.message
        msg_type = "message"
    elif update.business_message:
        msg = update.business_message
        msg_type = "business_message"
    elif update.edited_message:
        msg = update.edited_message
        msg_type = "edited_message"
    elif update.channel_post:
        msg = update.channel_post
        msg_type = "channel_post"
    
    if not msg:
        logger.info(f"⚠️ Получено обновление без сообщения: {update}")
        return
    
    # Логируем ВСЁ
    logger.info("="*50)
    logger.info(f"📩 Тип: {msg_type}")
    logger.info(f"   ID чата: {msg.chat_id}")
    logger.info(f"   ID отправителя: {msg.from_user.id if msg.from_user else 'None'}")
    logger.info(f"   Текст: {msg.text[:100] if msg.text else 'None'}...")
    logger.info(f"   Есть реплай: {bool(msg.reply_to_message)}")
    if msg.reply_to_message:
        logger.info(f"   Реплай к: {msg.reply_to_message.from_user.id if msg.reply_to_message.from_user else 'None'}")
    logger.info(f"   Update ID: {update.update_id}")
    logger.info("="*50)
    
    # Проверяем, что сообщение от владельца
    if not msg.from_user or msg.from_user.id != OWNER_ID:
        logger.info("❌ Сообщение не от владельца, игнорируем")
        return
    
    if not msg.text:
        logger.info("❌ Нет текста, игнорируем")
        return
    
    text = msg.text
    logger.info(f"🔍 Проверяем текст: {text[:50]}...")
    
    # Проверяем ключевую фразу
    stage_idx, found_phrase = check_stage_phrase(text)
    
    if stage_idx is None:
        logger.info("❌ Ключевая фраза НЕ найдена")
        return
    
    logger.info(f"✅ НАЙДЕНА ФРАЗА для этапа {stage_idx}: {STAGES[stage_idx]}")
    
    # Определяем клиента
    client_id = None
    
    if msg.reply_to_message and msg.reply_to_message.from_user:
        client_id = msg.reply_to_message.from_user.id
        logger.info(f"   Клиент по реплаю: {client_id}")
    else:
        # Проверяем упоминания
        if msg.entities:
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
                        logger.info(f"   Ошибка: {e}")
                elif entity.type == "text_mention":
                    client_id = entity.user.id
                    logger.info(f"   Клиент по text_mention: {client_id}")
                    break
    
    if client_id is None:
        logger.info("❌ НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ КЛИЕНТА")
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text="⚠️ Не удалось определить клиента. Используйте реплай на сообщение клиента или @username"
        )
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
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")

# ================== ЗАПУСК ==================
async def post_init(app):
    asyncio.create_task(check_inactive_clients(app))
    logger.info("🔄 Фоновая проверка запущена")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Обработчик ВСЕХ сообщений (с отладкой)
    app.add_handler(MessageHandler(filters.ALL, handle_all_messages))
    
    # Устанавливаем команды
    app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
    ])
    
    print("\n" + "="*60)
    print("🚀 БОТ ЗАПУЩЕН (РЕЖИМ ОТЛАДКИ)")
    print("="*60)
    print(f"👤 Владелец: {OWNER_ID}")
    print(f"🔑 Токен: {BOT_TOKEN[:15]}...")
    print("="*60)
    print("\n📌 Теперь бот будет ЛОГИРОВАТЬ ВСЕ сообщения")
    print("   Смотрите в консоль, что происходит")
    print("\n" + "="*60 + "\n")
    
    # Важно: разрешаем все типы обновлений
    app.run_polling(allowed_updates=["message", "business_message", "callback_query", "edited_message"])

if __name__ == "__main__":
    main()