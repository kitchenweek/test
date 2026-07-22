import logging
from datetime import datetime, timedelta
from typing import Dict, List
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
import re

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = "8615323356:AAFb_jZCUbOymsg4IAs0beKVOOUko7SEab0"
OWNER_ID = 2010296191  # Ваш Telegram ID

# Хранилище: {user_id: {"stage": int, "status": "active"/"inactive", "last_active": datetime, "hidden_at": datetime}}
user_data: Dict[int, Dict] = {}

# Настройки автоочистки
HIDE_AFTER_HOURS = 6    # Скрывать через 6 часов без новых фраз
DELETE_AFTER_DAYS = 14  # Удалять через 14 дней неактивности

# ЭТАПЫ (строго по порядку)
STAGES: List[str] = [
    "Доставка",
    "СВ",
    "Залог",
    "Перерасчет СВ",
    "Перерасчет залога",
    "Лот по СВ",
    "Лот по залогу"
]

# КЛЮЧЕВЫЕ ФРАЗЫ ДЛЯ КАЖДОГО ЭТАПА (ваши точные формулировки)
STAGE_PHRASES: Dict[int, List[str]] = {
    0: [  # Доставка
        "Добрый день, Ваш заказ прибыл к нам на склад в мск"
    ],
    1: [  # СВ
        "Сумма полностью возвратная т.е при уведомлении СДЭКа/Почты о получении товара клиентом сумма будет возвращена в полном объеме на номер карты (имя получателя и банк должен быть тот же, с которого была отправлена сумма)",
        "на разных складах товары, сумма за СВ та же"
    ],
    2: [  # Залог
        "18-19МСК, также не получили реквизиты на возврат СВ (имя отправителя как в чеке и тот же банк)",
        "18-19МСК, возврат по реквизитам"
    ],
    3: [  # Перерасчет СВ
        "перерасчет по СВ (отмена категории заказов до 100тыс₽), СВ на все заказы теперь"
    ],
    4: [  # Перерасчет залога
        "Перерасчет по залогу (отмена категории заказов до 100тыс₽), залог на все заказы теперь"
    ],
    5: [  # Лот СВ
        "клиент оплатил залоги и СВ на одну отправку, лот по которой уже закрыт, сейчас ТК ждет"
    ],
    6: [  # Лот залога
        "сумма к оплате на новый лот по залогу"
    ]
}

logging.basicConfig(level=logging.INFO)

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def check_stage_phrase(text: str) -> tuple:
    """
    Проверяет, какой этап соответствует тексту
    Возвращает (индекс_этапа, найденная_фраза) или (None, None)
    """
    text_lower = text.lower()
    for stage_idx, phrases in STAGE_PHRASES.items():
        for phrase in phrases:
            if phrase.lower() in text_lower:
                return stage_idx, phrase
    return None, None

def get_active_clients() -> Dict[int, Dict]:
    """Возвращает только актуальных клиентов"""
    active = {}
    for user_id, data in user_data.items():
        if data.get("status") != "inactive":
            active[user_id] = data
    return active

def get_inactive_clients() -> Dict[int, Dict]:
    """Возвращает неактуальных клиентов"""
    inactive = {}
    for user_id, data in user_data.items():
        if data.get("status") == "inactive":
            inactive[user_id] = data
    return inactive

async def check_inactive_clients(app):
    """Проверяет, кто из клиентов стал неактуальным"""
    while True:
        await asyncio.sleep(3600)  # Проверка каждый час
        now = datetime.now()
        for user_id, data in list(user_data.items()):
            if data.get("status") != "inactive":
                if now - data["last_active"] > timedelta(hours=HIDE_AFTER_HOURS):
                    data["status"] = "inactive"
                    data["hidden_at"] = now
                    # Лог владельцу
                    try:
                        user = await app.bot.get_chat(user_id)
                        name = user.username or user.first_name or str(user_id)
                        await app.bot.send_message(
                            chat_id=OWNER_ID,
                            text=f"⏰ Клиент @{name} стал неактуальным (6 часов без новых фраз)\n"
                                 f"Текущий этап: {STAGES[data['stage']]}"
                        )
                    except:
                        pass
        
        # Удаление старых неактуальных
        to_delete = []
        for user_id, data in user_data.items():
            if data.get("status") == "inactive":
                hidden_at = data.get("hidden_at", data["last_active"])
                if now - hidden_at > timedelta(days=DELETE_AFTER_DAYS):
                    to_delete.append(user_id)
        for user_id in to_delete:
            stage = user_data[user_id].get("stage", 0)
            try:
                user = await app.bot.get_chat(user_id)
                name = user.username or user.first_name or str(user_id)
                await app.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"🗑 Клиент @{name} удалён (14 дней неактивности)\n"
                         f"Последний этап: {STAGES[stage]}"
                )
            except:
                pass
            del user_data[user_id]

# ================== КОМАНДЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет доступа к этому боту")
        return
    
    keyboard = [
        [InlineKeyboardButton("📋 Актуальные клиенты", callback_data="show_active")],
        [InlineKeyboardButton("📂 Неактуальные клиенты", callback_data="show_inactive")],
        [InlineKeyboardButton("🔄 Обновить список", callback_data="refresh")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 Бот для отслеживания этапов клиентов\n\n"
        "✅ Актуальные - клиенты, которым вы отправляли фразы в последние 6 часов\n"
        "❌ Неактуальные - скрываются автоматически через 6 часов\n"
        "🗑 Удаляются через 14 дней неактивности\n\n"
        "📌 Клиент переходит на следующий этап, когда вы отправляете ему соответствующую фразу\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка инлайн-кнопок"""
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("❌ У вас нет доступа")
        return
    
    if query.data == "show_active":
        await show_active_clients(query.message, context)
    
    elif query.data == "show_inactive":
        await show_inactive_clients(query.message, context)
    
    elif query.data == "refresh":
        await query.message.delete()
        await start(update, context)
    
    elif query.data.startswith("hide_"):
        user_id = int(query.data.split("_")[1])
        if user_id in user_data:
            user_data[user_id]["status"] = "inactive"
            user_data[user_id]["hidden_at"] = datetime.now()
            await query.edit_message_text(
                f"✅ Клиент перемещён в неактуальные\n"
                f"Он будет удалён через {DELETE_AFTER_DAYS} дней"
            )
        else:
            await query.edit_message_text("❌ Клиент не найден")
    
    elif query.data.startswith("show_"):
        user_id = int(query.data.split("_")[1])
        if user_id in user_data:
            user_data[user_id]["status"] = "active"
            user_data[user_id]["last_active"] = datetime.now()
            user_data[user_id]["hidden_at"] = None
            await query.edit_message_text(
                f"✅ Клиент возвращён в актуальные\n"
                f"Текущий этап: {STAGES[user_data[user_id]['stage']]}"
            )
        else:
            await query.edit_message_text("❌ Клиент не найден")

async def show_active_clients(message, context: ContextTypes.DEFAULT_TYPE):
    """Показать актуальных клиентов"""
    active = get_active_clients()
    
    if not active:
        await message.edit_text(
            "📭 Нет актуальных клиентов\n\n"
            "Отправьте клиенту ключевую фразу, чтобы он появился здесь"
        )
        return
    
    # Сортируем по времени последней активности
    sorted_clients = sorted(active.items(), key=lambda x: x[1]["last_active"], reverse=True)
    
    text = "📋 Актуальные клиенты:\n\n"
    keyboard = []
    
    for user_id, data in sorted_clients:
        # Получаем имя пользователя
        try:
            user = await context.bot.get_chat(user_id)
            name = user.username or user.first_name or str(user_id)
        except:
            name = str(user_id)
        
        stage_idx = data.get("stage", 0)
        stage_name = STAGES[stage_idx] if stage_idx < len(STAGES) else "✅ Завершён"
        
        # Время последней активности
        last_active = data["last_active"].strftime("%d.%m %H:%M")
        text += f"• @{name} — {stage_name} (обновлён: {last_active})\n"
        
        # Добавляем кнопку "В неактуальные"
        keyboard.append([
            InlineKeyboardButton(
                f"⬇️ В неактуальные @{name}", 
                callback_data=f"hide_{user_id}"
            )
        ])
    
    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await message.edit_text(text)

async def show_inactive_clients(message, context: ContextTypes.DEFAULT_TYPE):
    """Показать неактуальных клиентов"""
    inactive = get_inactive_clients()
    
    if not inactive:
        await message.edit_text(
            "📭 Нет неактуальных клиентов\n\n"
            "Все клиенты либо актуальны, либо уже удалены"
        )
        return
    
    text = "📂 Неактуальные клиенты:\n\n"
    keyboard = []
    
    for user_id, data in inactive.items():
        # Получаем имя пользователя
        try:
            user = await context.bot.get_chat(user_id)
            name = user.username or user.first_name or str(user_id)
        except:
            name = str(user_id)
        
        stage_idx = data.get("stage", 0)
        stage_name = STAGES[stage_idx] if stage_idx < len(STAGES) else "✅ Завершён"
        
        # Время скрытия
        hidden_at = data.get("hidden_at", data["last_active"]).strftime("%d.%m %H:%M")
        text += f"• @{name} — {stage_name} (скрыт: {hidden_at})\n"
        
        # Добавляем кнопку "Вернуть в актуальные"
        keyboard.append([
            InlineKeyboardButton(
                f"⬆️ Вернуть @{name}", 
                callback_data=f"show_{user_id}"
            )
        ])
    
    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await message.edit_text(text)

# ================== ОБРАБОТКА ИСХОДЯЩИХ СООБЩЕНИЙ ==================
async def handle_outgoing_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ЛЮБЫЕ исходящие сообщения от владельца"""
    # Проверяем, что сообщение от владельца
    if update.effective_user.id != OWNER_ID:
        return
    
    msg = update.message
    if not msg or not msg.text:
        return
    
    text = msg.text
    
    # Проверяем, есть ли в сообщении ключевая фраза
    stage_idx, found_phrase = check_stage_phrase(text)
    
    if stage_idx is not None:
        # Определяем ID клиента (если есть реплай - берём оттуда, иначе ищем в тексте)
        client_id = None
        
        # Если есть реплай - берём ID из реплая
        if msg.reply_to_message:
            client_id = msg.reply_to_message.from_user.id
        else:
            # Пытаемся найти @username или ID в тексте
            # Проверяем, есть ли упоминание через @
            if msg.entities:
                for entity in msg.entities:
                    if entity.type == "mention":
                        username = text[entity.offset:entity.offset + entity.length]
                        try:
                            # Пытаемся получить пользователя по username
                            user = await context.bot.get_chat(username)
                            client_id = user.id
                            break
                        except:
                            pass
                    elif entity.type == "text_mention":
                        client_id = entity.user.id
                        break
        
        # Если клиент не найден - игнорируем
        if client_id is None:
            return
        
        now = datetime.now()
        
        # Если клиент новый - добавляем
        if client_id not in user_data:
            user_data[client_id] = {
                "stage": stage_idx,
                "status": "active",
                "last_active": now,
                "hidden_at": None
            }
            
            try:
                user = await context.bot.get_chat(client_id)
                name = user.username or user.first_name or str(client_id)
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"🆕 Новый клиент @{name}\n"
                         f"Этап: {STAGES[stage_idx]}"
                )
            except:
                pass
            
        else:
            # Обновляем этап только если он выше текущего
            current_stage = user_data[client_id].get("stage", 0)
            if stage_idx > current_stage:
                user_data[client_id]["stage"] = stage_idx
                
                try:
                    user = await context.bot.get_chat(client_id)
                    name = user.username or user.first_name or str(client_id)
                    await context.bot.send_message(
                        chat_id=OWNER_ID,
                        text=f"⬆️ Клиент @{name} перешёл на этап: {STAGES[stage_idx]}"
                    )
                except:
                    pass
            
            # Обновляем время активности и статус
            user_data[client_id]["last_active"] = now
            user_data[client_id]["status"] = "active"
            user_data[client_id]["hidden_at"] = None

# ================== ЗАПУСК ==================
async def post_init(app):
    """Запуск фоновой проверки"""
    asyncio.create_task(check_inactive_clients(app))

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    
    # Callback-запросы от кнопок
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Обработчик ВСЕХ исходящих сообщений от владельца
    app.add_handler(MessageHandler(filters.TEXT & filters.User(OWNER_ID), handle_outgoing_message))
    
    # Устанавливаем команды
    app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
    ])
    
    print("🚀 Бот запущен...")
    print(f"📌 Отслеживаются {len(STAGES)} этапов:")
    for i, stage in enumerate(STAGES):
        print(f"   {i+1}. {stage}")
    print(f"\n👤 Владелец: {OWNER_ID}")
    print(f"🔑 Токен: {BOT_TOKEN[:15]}...")
    
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()