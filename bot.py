import logging
from datetime import datetime, timedelta
from typing import Dict, List
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = "8911152200:AAF75Xn8M-9XoCBEOYRuq8azBknQxrnkLZ4"
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
    """Проверяет, какой этап соответствует тексту"""
    if not text:
        return None, None
    
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
                            text=f"⏰ Клиент @{name} стал неактуальным (6 часов без новых фраз)\n"
                                 f"Текущий этап: {STAGES[data['stage']]}"
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
        [InlineKeyboardButton("📂 Неактуальные клиенты", callback_data="show_inactive")]
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
        await show_active_clients(query, context)
    
    elif query.data == "show_inactive":
        await show_inactive_clients(query, context)
    
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

async def show_active_clients(query, context: ContextTypes.DEFAULT_TYPE):
    """Показать актуальных клиентов"""
    active = get_active_clients()
    
    if not active:
        await query.edit_message_text(
            "📭 Нет актуальных клиентов\n\n"
            "Отправьте клиенту ключевую фразу, чтобы он появился здесь"
        )
        return
    
    sorted_clients = sorted(active.items(), key=lambda x: x[1]["last_active"], reverse=True)
    
    text = "📋 Актуальные клиенты:\n\n"
    keyboard = []
    
    for user_id, data in sorted_clients:
        try:
            user = await context.bot.get_chat(user_id)
            name = user.username or user.first_name or str(user_id)
        except:
            name = str(user_id)
        
        stage_idx = data.get("stage", 0)
        stage_name = STAGES[stage_idx] if stage_idx < len(STAGES) else "✅ Завершён"
        
        last_active = data["last_active"].strftime("%d.%m %H:%M")
        text += f"• @{name} — {stage_name} (обновлён: {last_active})\n"
        
        keyboard.append([
            InlineKeyboardButton(
                f"⬇️ В неактуальные @{name}", 
                callback_data=f"hide_{user_id}"
            )
        ])
    
    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await query.edit_message_text(text)

async def show_inactive_clients(query, context: ContextTypes.DEFAULT_TYPE):
    """Показать неактуальных клиентов"""
    inactive = get_inactive_clients()
    
    if not inactive:
        await query.edit_message_text(
            "📭 Нет неактуальных клиентов\n\n"
            "Все клиенты либо актуальны, либо уже удалены"
        )
        return
    
    text = "📂 Неактуальные клиенты:\n\n"
    keyboard = []
    
    for user_id, data in inactive.items():
        try:
            user = await context.bot.get_chat(user_id)
            name = user.username or user.first_name or str(user_id)
        except:
            name = str(user_id)
        
        stage_idx = data.get("stage", 0)
        stage_name = STAGES[stage_idx] if stage_idx < len(STAGES) else "✅ Завершён"
        
        hidden_at = data.get("hidden_at", data["last_active"]).strftime("%d.%m %H:%M")
        text += f"• @{name} — {stage_name} (скрыт: {hidden_at})\n"
        
        keyboard.append([
            InlineKeyboardButton(
                f"⬆️ Вернуть @{name}", 
                callback_data=f"show_{user_id}"
            )
        ])
    
    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await query.edit_message_text(text)

# ================== ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ ==================
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает сообщения через Business API.
    Срабатывает на ЛЮБЫЕ сообщения в бизнес-чатах.
    """
    # Проверяем, что это бизнес-сообщение
    if not update.business_message:
        return
    
    msg = update.business_message
    
    # Проверяем, что сообщение от владельца (исходящее)
    if msg.from_user.id != OWNER_ID:
        return
    
    # Проверяем, что это текст
    if not msg.text:
        return
    
    text = msg.text
    
    print(f"📩 Business-сообщение от вас: {text[:50]}...")
    print(f"   ID отправителя: {msg.from_user.id}")
    
    # Проверяем, есть ли в сообщении ключевая фраза
    stage_idx, found_phrase = check_stage_phrase(text)
    
    if stage_idx is None:
        print("❌ Ключевая фраза НЕ найдена")
        return
    
    print(f"✅ Найдена фраза для этапа {stage_idx}: {STAGES[stage_idx]}")
    
    # Определяем ID клиента (получатель сообщения)
    # В бизнес-чате получатель - это тот, кому адресовано сообщение
    # Проверяем, есть ли реплай
    client_id = None
    
    if msg.reply_to_message:
        # Если есть реплай - берём ID из реплая
        client_id = msg.reply_to_message.from_user.id
        print(f"   Клиент определён через реплай: {client_id}")
    else:
        # В бизнес-чате исходящее сообщение обычно адресовано конкретному пользователю
        # Но мы не можем получить получателя напрямую, поэтому ищем упоминания
        if msg.entities:
            for entity in msg.entities:
                if entity.type == "mention":
                    username = text[entity.offset:entity.offset + entity.length]
                    print(f"   Найдено упоминание: {username}")
                    try:
                        user = await context.bot.get_chat(username)
                        client_id = user.id
                        print(f"   Клиент по username: {client_id}")
                        break
                    except Exception as e:
                        print(f"   Ошибка получения пользователя: {e}")
                elif entity.type == "text_mention":
                    client_id = entity.user.id
                    print(f"   Клиент через text_mention: {client_id}")
                    break
    
    # Если клиент не найден, пробуем найти получателя через чат
    if client_id is None:
        # В бизнес-чате это может быть личный чат с клиентом
        # Тогда получатель - это другой участник чата
        chat = msg.chat
        if chat.type == "private":
            # В личном чате получатель - это другой пользователь
            # Но мы не можем его определить, поэтому используем chat.id
            client_id = chat.id
            print(f"   Клиент определён как чат: {client_id}")
        else:
            print("❌ Не удалось определить клиента")
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text="⚠️ Не удалось определить клиента. Используйте реплай на сообщение клиента или @username"
            )
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
            print(f"✅ Новый клиент добавлен: @{name}, этап {STAGES[stage_idx]}")
        except Exception as e:
            print(f"❌ Ошибка при добавлении клиента: {e}")
        
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
                print(f"⬆️ Клиент @{name} перешёл на этап {STAGES[stage_idx]}")
            except Exception as e:
                print(f"❌ Ошибка при обновлении этапа: {e}")
        
        # Обновляем время активности и статус
        user_data[client_id]["last_active"] = now
        user_data[client_id]["status"] = "active"
        user_data[client_id]["hidden_at"] = None
        print(f"✅ Обновлено время активности для клиента")

# ================== ЗАПУСК ==================
async def post_init(app):
    """Запуск фоновой проверки"""
    asyncio.create_task(check_inactive_clients(app))
    print("🔄 Фоновая проверка запущена")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    
    # Callback-запросы от кнопок
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Обработчик БИЗНЕС-СООБЩЕНИЙ (важно!)
    app.add_handler(MessageHandler(filters.ALL, handle_business_message))
    
    # Устанавливаем команды
    app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
    ])
    
    print("\n" + "="*50)
    print("🚀 БОТ ЗАПУЩЕН (Business API)")
    print("="*50)
    print(f"👤 Владелец: {OWNER_ID}")
    print(f"🔑 Токен: {BOT_TOKEN[:15]}...")
    print(f"📌 Отслеживаются {len(STAGES)} этапов:")
    for i, stage in enumerate(STAGES):
        print(f"   {i+1}. {stage}")
    print("="*50)
    print("\n📩 Отправьте клиенту сообщение с ключевой фразой")
    print("   Бот определит клиента и добавит в список")
    print("\n💡 Используйте реплай на сообщение клиента или @username")
    print("\n" + "="*50 + "\n")
    
    # Важно: добавляем allowed_updates для бизнес-сообщений
    app.run_polling(allowed_updates=["message", "callback_query", "business_message"])

if __name__ == "__main__":
    main()