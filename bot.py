import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
import sqlite3
import math
import pytz

# Конфигурация
TOKEN = "8574655444:AAGlqVwKq1R_t0JcW9frVeo1ZEmnJirBwSI"
ADMIN_ID = 7517164478
TIMEZONE = pytz.timezone('Europe/Moscow')

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота
storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=storage)

# Состояния для FSM
class AddTagStates(StatesGroup):
    waiting_for_bulk_tags = State()

class AdminAddProfit(StatesGroup):
    waiting_for_tag = State()
    waiting_for_usd = State()
    waiting_for_rub = State()
    waiting_for_message = State()

class MarkUnsubscribed(StatesGroup):
    waiting_for_selection = State()

class ClientListState(StatesGroup):
    viewing = State()

class CheckTagState(StatesGroup):
    waiting_for_tag = State()

class PayoffState(StatesGroup):
    waiting_for_worker_tag = State()
    waiting_for_confirmation = State()

class EditDeadlineState(StatesGroup):
    waiting_for_tag = State()
    waiting_for_new_deadline = State()

# Глобальная переменная для времени (для тестов)
TEST_TIME = None
marked_tags = {}

def get_current_time():
    if TEST_TIME:
        return TEST_TIME
    return datetime.now(TIMEZONE)

# Инициализация БД
def init_db():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        is_admin BOOLEAN DEFAULT 0
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag TEXT UNIQUE,
        user_id INTEGER,
        deadline TEXT,
        is_active BOOLEAN DEFAULT 1,
        created_at TEXT,
        skipped_count INTEGER DEFAULT 0,
        last_shown TEXT,
        is_archived BOOLEAN DEFAULT 0,
        show_in_profit BOOLEAN DEFAULT 1,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag_id INTEGER,
        amount_usd REAL,
        amount_rub REAL,
        profit REAL,
        message TEXT,
        payment_date TEXT,
        FOREIGN KEY (tag_id) REFERENCES tags (id)
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS unsubscribed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag_id INTEGER,
        unsubscribed_date TEXT,
        FOREIGN KEY (tag_id) REFERENCES tags (id)
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS payoffs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        payoff_date TEXT,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('last_update', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('last_check', '')")
    
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, is_admin) VALUES (?, ?, ?)", 
                  (ADMIN_ID, "admin", 1))
    
    conn.commit()
    conn.close()

def get_last_update_time():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'last_update'")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else ''

def set_last_update_time(time_str):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'last_update'", (time_str,))
    conn.commit()
    conn.close()

def get_last_check_time():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'last_check'")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else ''

def set_last_check_time(time_str):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'last_check'", (time_str,))
    conn.commit()
    conn.close()

# Вспомогательные функции
def get_user(user_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def get_user_by_username(username):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username LIKE ?", (f"%{username}%",))
    user = cursor.fetchone()
    conn.close()
    return user

def add_user(user_id, username, full_name):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)", 
                  (user_id, username, full_name))
    conn.commit()
    conn.close()

def add_tag(user_id, tag, deadline):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tags (user_id, tag, deadline, created_at, skipped_count, last_shown, show_in_profit) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                  (user_id, tag, deadline, get_current_time().strftime("%Y-%m-%d %H:%M:%S"), 0, '', 1))
    conn.commit()
    conn.close()

def get_tag_by_name(tag_name):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tags WHERE tag = ? AND is_active = 1 AND show_in_profit = 1", (tag_name,))
    tag = cursor.fetchone()
    conn.close()
    return tag

def get_tag_info(tag_name):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''
    SELECT t.tag, t.deadline, u.username, u.user_id,
           CASE WHEN u2.tag_id IS NOT NULL THEN 1 ELSE 0 END as is_unsubscribed
    FROM tags t 
    JOIN users u ON t.user_id = u.user_id 
    LEFT JOIN unsubscribed u2 ON t.id = u2.tag_id
    WHERE t.tag = ? AND t.is_active = 1 AND t.show_in_profit = 1
    ''', (tag_name,))
    tag = cursor.fetchone()
    conn.close()
    return tag

def get_all_user_tags_with_status(user_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''
    SELECT t.id, t.tag, t.deadline, t.created_at,
           CASE WHEN u.tag_id IS NOT NULL THEN 1 ELSE 0 END as is_unsubscribed,
           CASE WHEN p.tag_id IS NOT NULL THEN 1 ELSE 0 END as has_payment
    FROM tags t 
    LEFT JOIN unsubscribed u ON t.id = u.tag_id 
    LEFT JOIN payments p ON t.id = p.tag_id
    WHERE t.user_id = ? AND t.is_active = 1 AND t.show_in_profit = 1
    ORDER BY t.created_at DESC
    ''', (user_id,))
    tags = cursor.fetchall()
    conn.close()
    return tags

def add_payment(tag_id, amount_usd, amount_rub, profit, message=""):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments (tag_id, amount_usd, amount_rub, profit, message, payment_date) VALUES (?, ?, ?, ?, ?, ?)", 
                  (tag_id, amount_usd, amount_rub, profit, message, get_current_time().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def add_unsubscribed(tag_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO unsubscribed (tag_id, unsubscribed_date) VALUES (?, ?)", 
                  (tag_id, get_current_time().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    set_last_update_time(get_current_time().strftime("%H:%M"))

def remove_from_unsubscribed(tag_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unsubscribed WHERE tag_id = ?", (tag_id,))
    conn.commit()
    conn.close()

def is_tag_unsubscribed_by_name(tag_name):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''
    SELECT t.id FROM tags t 
    JOIN unsubscribed u ON t.id = u.tag_id
    WHERE t.tag = ? AND t.is_active = 1
    ''', (tag_name,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_tag_id_by_name(tag_name):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM tags WHERE tag = ? AND is_active = 1", (tag_name,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def get_unsubscribed_tags():
    today = get_current_time().strftime("%d.%m")
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    UPDATE tags 
    SET is_archived = 1 
    WHERE deadline < ? AND is_active = 1 AND is_archived = 0
    ''', (today,))
    conn.commit()
    
    cursor.execute('''
    SELECT t.id, t.tag, u.username, u.user_id, t.skipped_count, t.last_shown, t.deadline
    FROM tags t 
    JOIN users u ON t.user_id = u.user_id 
    WHERE t.is_active = 1 AND t.is_archived = 0
    AND t.id NOT IN (SELECT tag_id FROM unsubscribed)
    ORDER BY t.created_at DESC
    ''')
    all_tags = cursor.fetchall()
    conn.close()
    
    result = []
    for tag in all_tags:
        tag_id, tag_name, username, user_id, skipped_count, last_shown, deadline = tag
        if not last_shown:
            result.append(tag)
            update_tag_shown(tag_id)
        else:
            if skipped_count == 0:
                result.append(tag)
                reset_tag_skipped(tag_id)
            else:
                decrement_tag_skipped(tag_id)
    
    return result

def update_tag_shown(tag_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE tags SET last_shown = ? WHERE id = ?", 
                  (get_current_time().strftime("%Y-%m-%d %H:%M:%S"), tag_id))
    conn.commit()
    conn.close()

def reset_tag_skipped(tag_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE tags SET skipped_count = 0 WHERE id = ?", (tag_id,))
    conn.commit()
    conn.close()

def decrement_tag_skipped(tag_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE tags SET skipped_count = skipped_count - 1 WHERE id = ? AND skipped_count > 0", (tag_id,))
    conn.commit()
    conn.close()

def update_deadline(tag_name, new_deadline):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE tags SET deadline = ? WHERE tag = ? AND is_active = 1 AND show_in_profit = 1", 
                  (new_deadline, tag_name))
    conn.commit()
    conn.close()

def calculate_profit(amount_rub):
    if amount_rub <= 1000:
        return amount_rub * 0.8
    elif amount_rub <= 5000:
        return amount_rub * 0.7
    elif amount_rub <= 10000:
        return amount_rub * 0.6
    else:
        return amount_rub * 0.5

def get_user_stats(user_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM tags WHERE user_id = ? AND is_active = 1 AND show_in_profit = 1", (user_id,))
    tags = cursor.fetchall()
    tag_ids = [tag[0] for tag in tags]
    
    stats = {
        'total_payments_usd': 0,
        'total_payments_rub': 0,
        'total_profit_usd': 0,
        'total_profit_rub': 0,
        'clients_count': len(tags),
        'unsubscribed_count': 0,
        'balance': 0,
        'total_payoffs': 0
    }
    
    if tag_ids:
        placeholders = ','.join('?' * len(tag_ids))
        
        cursor.execute(f"SELECT SUM(amount_rub) FROM payments WHERE tag_id IN ({placeholders})", tag_ids)
        result = cursor.fetchone()[0]
        stats['total_payments_rub'] = result if result else 0
        
        cursor.execute(f"SELECT SUM(amount_usd) FROM payments WHERE tag_id IN ({placeholders})", tag_ids)
        result = cursor.fetchone()[0]
        stats['total_payments_usd'] = result if result else 0
        
        cursor.execute(f"SELECT SUM(profit) FROM payments WHERE tag_id IN ({placeholders})", tag_ids)
        result = cursor.fetchone()[0]
        stats['total_profit_rub'] = result if result else 0
        
        cursor.execute(f"SELECT SUM(amount_usd * profit / amount_rub) FROM payments WHERE tag_id IN ({placeholders}) AND amount_rub > 0", tag_ids)
        result = cursor.fetchone()[0]
        stats['total_profit_usd'] = result if result else 0
        
        cursor.execute(f"SELECT COUNT(*) FROM unsubscribed WHERE tag_id IN ({placeholders})", tag_ids)
        stats['unsubscribed_count'] = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(amount) FROM payoffs WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()[0]
    stats['total_payoffs'] = result if result else 0
    
    total_earned = stats['unsubscribed_count'] * 0.4
    stats['balance'] = total_earned - stats['total_payoffs']
    
    conn.close()
    return stats

def get_worker_unsubscribed_amount(user_id):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM tags WHERE user_id = ? AND is_active = 1 AND show_in_profit = 1", (user_id,))
    tags = cursor.fetchall()
    tag_ids = [tag[0] for tag in tags]
    
    amount = 0
    if tag_ids:
        placeholders = ','.join('?' * len(tag_ids))
        cursor.execute(f"SELECT COUNT(*) FROM unsubscribed WHERE tag_id IN ({placeholders})", tag_ids)
        count = cursor.fetchone()[0]
        amount = count * 0.4
    else:
        count = 0
        amount = 0
    
    conn.close()
    return amount, count

def add_payoff(user_id, amount):
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payoffs (user_id, amount, payoff_date) VALUES (?, ?, ?)", 
                  (user_id, amount, get_current_time().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_team_stats():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    stats = {
        'active_workers': 0,
        'total_payments_usd': 0,
        'total_payments_rub': 0,
        'total_profit_usd': 0,
        'total_profit_rub': 0,
        'total_clients': 0,
        'total_unsubscribed': 0
    }
    
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM tags WHERE is_active = 1 AND show_in_profit = 1")
    stats['active_workers'] = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(amount_rub) FROM payments")
    result = cursor.fetchone()[0]
    stats['total_payments_rub'] = result if result else 0
    
    cursor.execute("SELECT SUM(amount_usd) FROM payments")
    result = cursor.fetchone()[0]
    stats['total_payments_usd'] = result if result else 0
    
    cursor.execute("SELECT SUM(profit) FROM payments")
    result = cursor.fetchone()[0]
    stats['total_profit_rub'] = result if result else 0
    
    cursor.execute("SELECT SUM(amount_usd * profit / amount_rub) FROM payments WHERE amount_rub > 0")
    result = cursor.fetchone()[0]
    stats['total_profit_usd'] = result if result else 0
    
    cursor.execute("SELECT COUNT(*) FROM tags WHERE is_active = 1 AND show_in_profit = 1")
    stats['total_clients'] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM unsubscribed")
    stats['total_unsubscribed'] = cursor.fetchone()[0]
    
    conn.close()
    return stats

def get_team_weekly_stats():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    week_ago = (get_current_time() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    
    stats = {
        'daily': [],
        'total_profit': 0,
        'total_payments': 0,
        'new_clients': 0,
        'unsubscribed': 0,
        'active_workers': 0
    }
    
    cursor.execute('''
    SELECT DATE(payment_date), 
           SUM(amount_usd * profit / amount_rub) as profit_usd,
           SUM(amount_usd) as payments_usd
    FROM payments 
    WHERE payment_date > ? AND amount_rub > 0
    GROUP BY DATE(payment_date)
    ORDER BY DATE(payment_date) DESC
    ''', (week_ago,))
    daily = cursor.fetchall()
    
    for day in daily:
        stats['daily'].append({
            'date': day[0],
            'profit': day[1] if day[1] else 0,
            'payments': day[2] if day[2] else 0
        })
    
    cursor.execute("SELECT SUM(amount_usd * profit / amount_rub) FROM payments WHERE payment_date > ? AND amount_rub > 0", (week_ago,))
    result = cursor.fetchone()[0]
    stats['total_profit'] = result if result else 0
    
    cursor.execute("SELECT SUM(amount_usd) FROM payments WHERE payment_date > ?", (week_ago,))
    result = cursor.fetchone()[0]
    stats['total_payments'] = result if result else 0
    
    cursor.execute("SELECT COUNT(*) FROM tags WHERE created_at > ? AND show_in_profit = 1", (week_ago,))
    stats['new_clients'] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM unsubscribed WHERE unsubscribed_date > ?", (week_ago,))
    stats['unsubscribed'] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM tags WHERE is_active = 1 AND show_in_profit = 1")
    stats['active_workers'] = cursor.fetchone()[0]
    
    conn.close()
    return stats

def get_expiring_tags(user_id):
    today = get_current_time().strftime("%d.%m")
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''
    SELECT tag, deadline 
    FROM tags 
    WHERE user_id = ? AND is_active = 1 AND show_in_profit = 1 AND deadline = ?
    ''', (user_id, today))
    tags = cursor.fetchall()
    conn.close()
    return tags

def get_all_unsubscribed_tags():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    today = get_current_time().strftime("%d.%m")
    
    cursor.execute('''
    UPDATE tags 
    SET is_archived = 1 
    WHERE deadline < ? AND is_active = 1 AND is_archived = 0
    ''', (today,))
    conn.commit()
    
    cursor.execute('''
    SELECT t.id, t.tag, u.username, u.user_id, t.deadline, t.created_at
    FROM tags t 
    JOIN users u ON t.user_id = u.user_id 
    WHERE t.is_active = 1 AND t.is_archived = 0
    AND t.id NOT IN (SELECT tag_id FROM unsubscribed)
    ORDER BY t.created_at DESC
    ''')
    tags = cursor.fetchall()
    conn.close()
    return tags

def paginate_items(items, page, per_page=20):
    total_pages = math.ceil(len(items) / per_page)
    if page < 1:
        page = 1
    if page > total_pages and total_pages > 0:
        page = total_pages
    
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]
    
    return page_items, page, total_pages

def get_main_keyboard(user_id):
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    user = get_user(user_id)
    is_admin = user[3] if user else 0
    
    if is_admin:
        buttons_row1 = [KeyboardButton("📝 Добавить мамонта")]
        buttons_row2 = [
            KeyboardButton("💰 Добавить профит"),
            KeyboardButton("📌 Отметить отписку")
        ]
        buttons_row3 = [
            KeyboardButton("📊 Статистика команды"),
            KeyboardButton("👥 Мои мамонты")
        ]
        buttons_row4 = [
            KeyboardButton("🔍 Проверить тег"),
            KeyboardButton("💸 Списать переведенных")
        ]
        buttons_row5 = [
            KeyboardButton("📊 Личная статистика"),
            KeyboardButton("📈 Статистика за неделю")
        ]
        
        keyboard.row(*buttons_row1)
        keyboard.row(*buttons_row2)
        keyboard.row(*buttons_row3)
        keyboard.row(*buttons_row4)
        keyboard.row(*buttons_row5)
    else:
        buttons_row1 = [KeyboardButton("📝 Добавить мамонта")]
        buttons_row2 = [KeyboardButton("👥 Мои мамонты")]
        buttons_row3 = [KeyboardButton("📊 Личная статистика")]
        
        keyboard.row(*buttons_row1)
        keyboard.row(*buttons_row2)
        keyboard.row(*buttons_row3)
    
    return keyboard

@dp.message_handler(lambda message: message.text == "◀️ Назад")
async def back_to_menu(message: types.Message, state: FSMContext):
    await state.finish()
    user_id = message.from_user.id
    await message.answer(
        "👋 Добро пожаловать в главное меню!\n\n"
        "Используй кнопки, чтобы:\n"
        "🦣 Ввести тег мамонта\n"
        "🔎 Проверить мамонтов\n"
        "📊 Посмотреть свою статистику\n\n"
        "Также сюда приходят уведомления о профите 💰",
        reply_markup=get_main_keyboard(user_id)
    )

# Функция для отправки напоминания админу (каждые 30 минут с 10:00 до 22:00)
async def send_reminder():
    while True:
        now = get_current_time()
        if 10 <= now.hour < 22:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "🔔 Напоминание!\n\n"
                    "Проверьте отписки мамонтов 📌\n"
                    "Возможно, кто-то отписался и нужно отметить."
                )
            except Exception as e:
                logging.error(f"Ошибка отправки напоминания: {e}")
        await asyncio.sleep(1800)

async def check_deadlines():
    while True:
        now = get_current_time()
        if now.hour == 10 and now.minute == 0:
            try:
                today = now.strftime("%d.%m")
                conn = sqlite3.connect('bot_database.db')
                cursor = conn.cursor()
                cursor.execute('''
                SELECT DISTINCT u.user_id, u.username 
                FROM tags t 
                JOIN users u ON t.user_id = u.user_id 
                WHERE t.deadline = ? AND t.is_active = 1 AND t.show_in_profit = 1
                ''', (today,))
                workers = cursor.fetchall()
                conn.close()
                
                for worker_id, username in workers:
                    tags = get_expiring_tags(worker_id)
                    if tags:
                        text = f"🔔 Напоминание о дедлайне!\n\n"
                        text += f"🦣 У тебя заканчивается срок у мамонтов:\n\n"
                        for tag, deadline in tags:
                            text += f"• {tag} (срок: {deadline})\n"
                        text += f"\n📢 Не забудь напомнить мамонту о промокоде!"
                        
                        await bot.send_message(worker_id, text)
            except Exception as e:
                logging.error(f"Ошибка проверки дедлайнов: {e}")
        await asyncio.sleep(60)

# Обработчики команд
@dp.message_handler(commands=["start"])
async def start_command(message: types.Message, state: FSMContext):
    await state.finish()
    
    user_id = message.from_user.id
    username = message.from_user.username or "unknown"
    full_name = message.from_user.full_name
    
    add_user(user_id, username, full_name)
    
    await message.answer(
        "👋 Добро пожаловать в главное меню!\n\n"
        "Используй кнопки, чтобы:\n"
        "🦣 Ввести тег мамонта\n"
        "🔎 Проверить мамонтов\n"
        "📊 Посмотреть свою статистику\n\n"
        "Также сюда приходят уведомления о профите 💰",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message_handler(commands=["stats"])
async def stats_command(message: types.Message, state: FSMContext):
    await state.finish()
    await personal_stats(message)

@dp.message_handler(commands=["top"])
async def top_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''
    SELECT u.username, 
           COUNT(t.id) as clients,
           SUM(p.amount_usd) as total_usd
    FROM users u
    JOIN tags t ON u.user_id = t.user_id
    LEFT JOIN payments p ON t.id = p.tag_id
    WHERE t.is_active = 1 AND t.show_in_profit = 1
    GROUP BY u.user_id
    ORDER BY total_usd DESC
    LIMIT 10
    ''')
    top = cursor.fetchall()
    conn.close()
    
    if not top:
        await message.answer("📊 Пока нет данных для рейтинга.")
        return
    
    text = "🏆 ТОП воркеров по профитам:\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (username, clients, total) in enumerate(top, 1):
        medal = medals[i-1] if i <= 3 else f"{i}."
        text += f"{medal} @{username} - {total:.2f}$ ({clients} мамонтов)\n"
    
    await message.answer(text, reply_markup=get_main_keyboard(message.from_user.id))

@dp.message_handler(commands=["add"])
async def add_command(message: types.Message):
    await AddTagStates.waiting_for_bulk_tags.set()
    back_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    back_keyboard.add(KeyboardButton("◀️ Назад"))
    await message.answer(
        "Введите теги и сроки через новую строку:\n"
        "Формат: @user ДД.ММ\n"
        "Пример:\n"
        "@user1 31.12\n"
        "@user2 15.01\n"
        "@user3 20.02",
        reply_markup=back_keyboard
    )

@dp.message_handler(commands=["check"])
async def check_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /check @user")
        return
    
    tag_name = args.strip()
    tag_info = get_tag_info(tag_name)
    
    if not tag_info:
        await message.answer("❌ Тег не найден в базе данных!")
        return
    
    tag, deadline, username, user_id, is_unsubscribed = tag_info
    status = "✅ Отписался" if is_unsubscribed else "❌ Не отписался"
    
    await message.answer(
        f"🔍 Информация о теге:\n\n"
        f"📌 Тег: {tag}\n"
        f"📅 Срок: {deadline}\n"
        f"👤 Воркер: @{username}\n"
        f"📊 Статус: {status}",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message_handler(commands=["cancel"])
async def cancel_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /cancel @user")
        return
    
    tag_name = args.strip()
    
    tag_info = get_tag_info(tag_name)
    if not tag_info:
        await message.answer(f"❌ Тег {tag_name} не найден в базе данных!")
        return
    
    if not is_tag_unsubscribed_by_name(tag_name):
        await message.answer(f"❌ Тег {tag_name} не отмечен как отписавшийся!")
        return
    
    tag_id = get_tag_id_by_name(tag_name)
    if tag_id:
        remove_from_unsubscribed(tag_id)
        await message.answer(
            f"✅ Тег {tag_name} удален из списка отписавшихся!\n"
            f"📅 Срок: {tag_info[1]}\n"
            f"👤 Воркер: @{tag_info[2]}"
        )
    else:
        await message.answer("❌ Ошибка при удалении тега!")

@dp.message_handler(commands=["checkmsg"])
async def checkmsg_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    await show_unsubscribed_tags(message, None, force=True)

@dp.message_handler(commands=["time"])
async def time_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ Использование: /time X (X - количество часов для прокрута)")
        return
    
    try:
        hours = int(args)
        global TEST_TIME
        TEST_TIME = datetime.now(TIMEZONE) + timedelta(hours=hours)
        await message.answer(f"✅ Время прокручено на {hours} часов.\nТекущее время: {TEST_TIME.strftime('%d.%m.%Y %H:%M')}")
    except ValueError:
        await message.answer("❌ Введите корректное число часов")

@dp.message_handler(state='*', commands=["start", "stats", "top", "add", "check", "cancel", "checkmsg", "time"])
async def command_state_handler(message: types.Message, state: FSMContext):
    await state.finish()
    command = message.get_command()
    if command == "/start":
        await start_command(message, state)
    elif command == "/stats":
        await personal_stats(message)
    elif command == "/top":
        await top_command(message)
    elif command == "/add":
        await add_command(message)
    elif command == "/check":
        await check_command(message)
    elif command == "/cancel":
        await cancel_command(message)
    elif command == "/checkmsg":
        await checkmsg_command(message)
    elif command == "/time":
        await time_command(message)

@dp.message_handler(lambda message: message.text == "📝 Добавить мамонта")
async def add_tag_start(message: types.Message):
    await AddTagStates.waiting_for_bulk_tags.set()
    back_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    back_keyboard.add(KeyboardButton("◀️ Назад"))
    await message.answer(
        "Введите теги и сроки через новую строку:\n"
        "Формат: @user ДД.ММ\n"
        "Пример:\n"
        "@user1 31.12\n"
        "@user2 15.01\n"
        "@user3 20.02",
        reply_markup=back_keyboard
    )

@dp.message_handler(state=AddTagStates.waiting_for_bulk_tags)
async def process_bulk_tags(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    lines = message.text.strip().split('\n')
    added = 0
    errors = []
    user_id = message.from_user.id
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        parts = line.split()
        if len(parts) < 2:
            errors.append(f"❌ {line} - неверный формат")
            continue
        
        tag = parts[0]
        if not tag.startswith('@'):
            errors.append(f"❌ {line} - тег должен начинаться с @")
            continue
        
        deadline = ' '.join(parts[1:])
        try:
            datetime.strptime(deadline, "%d.%m")
        except ValueError:
            errors.append(f"❌ {line} - неверный формат даты")
            continue
        
        existing_tag = get_tag_by_name(tag)
        if existing_tag:
            errors.append(f"❌ {line} - такой тег уже существует")
            continue
        
        add_tag(user_id, tag, deadline)
        added += 1
    
    await state.finish()
    
    result_text = f"✅ Добавлено мамонтов: {added}\n\n"
    if errors:
        result_text += "Ошибки:\n" + "\n".join(errors)
    
    await message.answer(result_text, reply_markup=get_main_keyboard(user_id))

@dp.message_handler(lambda message: message.text == "💰 Добавить профит")
async def admin_add_profit_start(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    await AdminAddProfit.waiting_for_tag.set()
    back_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    back_keyboard.add(KeyboardButton("◀️ Назад"))
    await message.answer(
        "Введите тег мамонта:",
        reply_markup=back_keyboard
    )

@dp.message_handler(state=AdminAddProfit.waiting_for_tag)
async def admin_process_tag(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    tag_name = message.text.strip()
    tag = get_tag_by_name(tag_name)
    
    if not tag:
        await message.answer("❌ Тег не найден! Введите существующий тег:")
        return
    
    await state.update_data(tag_id=tag[0], tag_name=tag[1])
    await AdminAddProfit.waiting_for_usd.set()
    await message.answer("Введите сумму в $:")

@dp.message_handler(state=AdminAddProfit.waiting_for_usd)
async def admin_process_usd(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    try:
        amount_usd = float(message.text.replace(',', '.'))
        await state.update_data(amount_usd=amount_usd)
        await AdminAddProfit.waiting_for_rub.set()
        await message.answer("Введите сумму в рублях:")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message_handler(state=AdminAddProfit.waiting_for_rub)
async def admin_process_rub(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    try:
        amount_rub = float(message.text.replace(',', '.'))
        await state.update_data(amount_rub=amount_rub)
        await AdminAddProfit.waiting_for_message.set()
        await message.answer("Введите сообщение для воркера (можно пропустить, отправив '-'):")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message_handler(state=AdminAddProfit.waiting_for_message)
async def admin_process_message(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    msg_text = message.text.strip()
    if msg_text == "-":
        msg_text = ""
    
    data = await state.get_data()
    tag_id = data['tag_id']
    tag_name = data['tag_name']
    amount_usd = data['amount_usd']
    amount_rub = data['amount_rub']
    
    profit_rub = calculate_profit(amount_rub)
    profit_usd = (profit_rub / amount_rub) * amount_usd if amount_rub > 0 else 0
    
    add_payment(tag_id, amount_usd, amount_rub, profit_rub, msg_text)
    
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM tags WHERE id = ?", (tag_id,))
    user_id = cursor.fetchone()[0]
    conn.close()
    
    notification = f"💰 Новый профит!\n\n"
    notification += f"🦣 Тег мамонта: {tag_name}\n"
    notification += f"💵 Сумма: {amount_rub:.0f}₽\n"
    notification += f"🪎 Твоя выплата: {profit_usd:.2f}$"
    if msg_text:
        notification += f"\n\n💬 Сообщение от ТСа: {msg_text}"
    
    await bot.send_message(user_id, notification)
    
    await state.finish()
    await message.answer(
        f"✅ Профит успешно добавлен!\n\n"
        f"🦣 Тег: {tag_name}\n"
        f"💵 Сумма в $: {amount_usd:.2f}$\n"
        f"💵 Сумма в ₽: {amount_rub:.0f}₽\n"
        f"🪎 Выплата: {profit_usd:.2f}$\n"
        f"💬 Сообщение: {msg_text if msg_text else 'Отсутствует'}",
        reply_markup=get_main_keyboard(ADMIN_ID)
    )

@dp.message_handler(lambda message: message.text == "📌 Отметить отписку")
async def mark_unsubscribed_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    last_check = get_last_check_time()
    if last_check:
        try:
            last_time = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
            time_diff = (get_current_time() - last_time).total_seconds()
            if time_diff < 1800:
                remaining = int(1800 - time_diff)
                minutes = remaining // 60
                seconds = remaining % 60
                await message.answer(
                    f"⏳ Подождите {minutes} минут {seconds} секунд до следующей проверки."
                )
                return
        except:
            pass
    
    set_last_check_time(get_current_time().strftime("%Y-%m-%d %H:%M:%S"))
    await state.finish()
    
    if user_id in marked_tags:
        del marked_tags[user_id]
    
    await show_unsubscribed_tags(message, state)

async def show_unsubscribed_tags(message_or_callback, state, force=False):
    tags = get_all_unsubscribed_tags()
    
    if not tags:
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer("✅ Нет активных тегов для отметки!")
        else:
            await message_or_callback.message.edit_text("✅ Нет активных тегов для отметки!")
        return
    
    if state:
        await state.update_data(tags_list=tags)
        await MarkUnsubscribed.waiting_for_selection.set()
    
    user_id = message_or_callback.from_user.id if hasattr(message_or_callback, 'from_user') else message_or_callback.message.from_user.id
    
    if user_id not in marked_tags:
        marked_tags[user_id] = set()
    
    for i, tag in enumerate(tags, 1):
        tag_id, tag_name, username, user_id, deadline, created_at = tag
        
        created_date = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        created_str = created_date.astimezone(TIMEZONE).strftime("%d.%m %H:%M")
        
        is_marked = tag_id in marked_tags.get(user_id, set())
        
        text = f"{i}. {tag_name} (@{username}) {deadline}\n"
        text += f"   изменено {created_str}"
        
        if is_marked:
            text += f"\n   ✅ Отмечен"
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        
        if is_marked:
            keyboard.add(InlineKeyboardButton(f"❌ Отменить #{i}", callback_data=f"cancel_unsub_{i}"))
        else:
            keyboard.add(InlineKeyboardButton(f"✅ Отметить #{i}", callback_data=f"mark_unsub_{i}"))
        
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer(text, reply_markup=keyboard)
        else:
            await message_or_callback.message.answer(text, reply_markup=keyboard)
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("✅ Завершить", callback_data="finish_unsub"))
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            "📌 Для отметки/отмены отписки нажмите соответствующую кнопку под тегом.",
            reply_markup=keyboard
        )
    else:
        await message_or_callback.message.answer(
            "📌 Для отметки/отмены отписки нажмите соответствующую кнопку под тегом.",
            reply_markup=keyboard
        )

@dp.callback_query_handler(lambda c: c.data.startswith("mark_unsub_"), state=MarkUnsubscribed.waiting_for_selection)
async def mark_unsubscribed_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    index = int(callback_query.data.split('_')[2]) - 1
    
    data = await state.get_data()
    tags_list = data.get('tags_list', [])
    
    if not tags_list or index >= len(tags_list):
        await callback_query.answer("❌ Ошибка! Тег не найден.")
        return
    
    tag_id = tags_list[index][0]
    tag_name = tags_list[index][1]
    
    if tag_id in marked_tags.get(user_id, set()):
        await callback_query.answer("❌ Этот тег уже отмечен!")
        return
    
    if user_id not in marked_tags:
        marked_tags[user_id] = set()
    marked_tags[user_id].add(tag_id)
    
    add_unsubscribed(tag_id)
    
    await callback_query.message.edit_text(
        f"✅ Отмечен: {tag_name}",
        reply_markup=None
    )
    await callback_query.answer(f"✅ Отписка для {tag_name} отмечена!")

@dp.callback_query_handler(lambda c: c.data.startswith("cancel_unsub_"), state=MarkUnsubscribed.waiting_for_selection)
async def cancel_unsubscribed_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    index = int(callback_query.data.split('_')[2]) - 1
    
    data = await state.get_data()
    tags_list = data.get('tags_list', [])
    
    if not tags_list or index >= len(tags_list):
        await callback_query.answer("❌ Ошибка! Тег не найден.")
        return
    
    tag_id = tags_list[index][0]
    tag_name = tags_list[index][1]
    
    if tag_id not in marked_tags.get(user_id, set()):
        await callback_query.answer("❌ Этот тег не был отмечен!")
        return
    
    marked_tags[user_id].remove(tag_id)
    remove_from_unsubscribed(tag_id)
    
    await callback_query.message.edit_text(
        f"❌ Отменено: {tag_name}",
        reply_markup=None
    )
    await callback_query.answer(f"❌ Отписка для {tag_name} отменена!")

@dp.callback_query_handler(lambda c: c.data == "finish_unsub", state=MarkUnsubscribed.waiting_for_selection)
async def finish_unsub_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    
    if user_id in marked_tags:
        del marked_tags[user_id]
    
    await state.finish()
    await callback_query.message.delete()
    await callback_query.answer("✅ Проверка завершена!")
    await bot.send_message(
        callback_query.from_user.id,
        "👋 Добро пожаловать в главное меню!\n\n"
        "Используй кнопки, чтобы:\n"
        "🦣 Ввести тег мамонта\n"
        "🔎 Проверить мамонтов\n"
        "📊 Посмотреть свою статистику\n\n"
        "Также сюда приходят уведомления о профите 💰",
        reply_markup=get_main_keyboard(callback_query.from_user.id)
    )

@dp.message_handler(lambda message: message.text == "👥 Мои мамонты")
async def view_all_clients(message: types.Message, state: FSMContext):
    await state.finish()
    
    user_id = message.from_user.id
    tags = get_all_user_tags_with_status(user_id)
    
    if not tags:
        await message.answer("У вас нет мамонтов.")
        return
    
    await state.update_data(clients_list=tags)
    await ClientListState.viewing.set()
    
    last_update = get_last_update_time()
    if not last_update:
        last_update = "еще не обновлялось"
    
    page = 1
    await send_clients_page(message, user_id, tags, page, last_update)

async def send_clients_page(message_or_callback, user_id, tags, page, update_time):
    items, page, total_pages = paginate_items(tags, page)
    
    text = f"📋 Список мамонтов\n"
    text += f"🕐 Последнее обновление: {update_time} UTC+3\n\n"
    
    for item in items:
        tag_name = item[1]
        deadline = item[2]
        is_unsubscribed = item[4]
        has_payment = item[5]
        
        if has_payment:
            status = "💰"
        elif is_unsubscribed:
            status = "✅"
        else:
            status = "❌"
        
        text += f"{status} {tag_name} | 📅 {deadline}\n"
    
    text += f"\nСтраница {page}/{total_pages}"
    
    keyboard = InlineKeyboardMarkup(row_width=3)
    buttons = []
    
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️", callback_data=f"clients_page_{page-1}"))
    
    buttons.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="current"))
    
    if page < total_pages:
        buttons.append(InlineKeyboardButton("➡️", callback_data=f"clients_page_{page+1}"))
    
    if buttons:
        keyboard.row(*buttons)
    
    keyboard.row(
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh_clients"),
        InlineKeyboardButton("✏️ Изменить срок", callback_data="edit_deadline")
    )
    keyboard.row(InlineKeyboardButton("❌ Закрыть", callback_data="close_clients"))
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(text, reply_markup=keyboard)
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("clients_page_"), state=ClientListState.viewing)
async def clients_pagination_callback(callback_query: types.CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split('_')[2])
    user_id = callback_query.from_user.id
    
    data = await state.get_data()
    tags = data.get('clients_list', [])
    
    if not tags:
        await callback_query.message.edit_text("❌ Список мамонтов не найден. Нажмите 'Мои мамонты' заново.")
        await callback_query.answer()
        await state.finish()
        return
    
    last_update = get_last_update_time()
    if not last_update:
        last_update = "еще не обновлялось"
    
    await send_clients_page(callback_query, user_id, tags, page, last_update)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "refresh_clients", state=ClientListState.viewing)
async def refresh_clients_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    tags = get_all_user_tags_with_status(user_id)
    
    if not tags:
        await callback_query.message.edit_text("У вас нет мамонтов.")
        await callback_query.answer()
        await state.finish()
        return
    
    await state.update_data(clients_list=tags)
    
    last_update = get_last_update_time()
    if not last_update:
        last_update = "еще не обновлялось"
    
    page = 1
    await send_clients_page(callback_query, user_id, tags, page, last_update)
    await callback_query.answer("🔄 Список обновлен!")

@dp.callback_query_handler(lambda c: c.data == "edit_deadline", state=ClientListState.viewing)
async def edit_deadline_start(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await EditDeadlineState.waiting_for_tag.set()
    await callback_query.message.delete()
    await callback_query.answer()
    back_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    back_keyboard.add(KeyboardButton("◀️ Назад"))
    await bot.send_message(
        callback_query.from_user.id,
        "Введите тег мамонта, у которого нужно изменить срок:",
        reply_markup=back_keyboard
    )

@dp.message_handler(state=EditDeadlineState.waiting_for_tag)
async def edit_deadline_process_tag(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    tag_name = message.text.strip()
    tag = get_tag_by_name(tag_name)
    
    if not tag:
        await message.answer("❌ Тег не найден! Введите существующий тег:")
        return
    
    await state.update_data(tag_name=tag_name)
    await EditDeadlineState.waiting_for_new_deadline.set()
    await message.answer(f"Введите новый срок для {tag_name} (в формате ДД.ММ):")

@dp.message_handler(state=EditDeadlineState.waiting_for_new_deadline)
async def edit_deadline_process_new(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    new_deadline = message.text.strip()
    
    try:
        datetime.strptime(new_deadline, "%d.%m")
    except ValueError:
        await message.answer("❌ Неверный формат! Используйте ДД.ММ (например, 31.12):")
        return
    
    data = await state.get_data()
    tag_name = data['tag_name']
    
    update_deadline(tag_name, new_deadline)
    
    await state.finish()
    await message.answer(
        f"✅ Срок для {tag_name} успешно обновлен!\n"
        f"📅 Новый срок: {new_deadline}",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.callback_query_handler(lambda c: c.data == "close_clients", state=ClientListState.viewing)
async def close_clients_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    await state.finish()
    user_id = callback_query.from_user.id
    await bot.send_message(
        user_id, 
        "👋 Добро пожаловать в главное меню!\n\n"
        "Используй кнопки, чтобы:\n"
        "🦣 Ввести тег мамонта\n"
        "🔎 Проверить мамонтов\n"
        "📊 Посмотреть свою статистику\n\n"
        "Также сюда приходят уведомления о профите 💰",
        reply_markup=get_main_keyboard(user_id)
    )
    await callback_query.answer()

@dp.message_handler(lambda message: message.text == "🔍 Проверить тег")
async def check_tag_start(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    await CheckTagState.waiting_for_tag.set()
    back_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    back_keyboard.add(KeyboardButton("◀️ Назад"))
    await message.answer(
        "Введите тег для проверки:",
        reply_markup=back_keyboard
    )

@dp.message_handler(state=CheckTagState.waiting_for_tag)
async def check_tag_process(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    tag_name = message.text.strip()
    tag_info = get_tag_info(tag_name)
    
    if not tag_info:
        await message.answer("❌ Тег не найден в базе данных!")
        await state.finish()
        await message.answer(
            "👋 Добро пожаловать в главное меню!\n\n"
            "Используй кнопки, чтобы:\n"
            "🦣 Ввести тег мамонта\n"
            "🔎 Проверить мамонтов\n"
            "📊 Посмотреть свою статистику\n\n"
            "Также сюда приходят уведомления о профите 💰",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    tag, deadline, username, user_id, is_unsubscribed = tag_info
    status = "✅ Отписался" if is_unsubscribed else "❌ Не отписался"
    
    await state.finish()
    await message.answer(
        f"🔍 Информация о теге:\n\n"
        f"📌 Тег: {tag}\n"
        f"📅 Срок: {deadline}\n"
        f"👤 Воркер: @{username}\n"
        f"📊 Статус: {status}",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message_handler(lambda message: message.text == "💸 Списать переведенных")
async def payoff_start(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    await PayoffState.waiting_for_worker_tag.set()
    back_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    back_keyboard.add(KeyboardButton("◀️ Назад"))
    await message.answer(
        "Введите тег воркера (например: @username):",
        reply_markup=back_keyboard
    )

@dp.message_handler(state=PayoffState.waiting_for_worker_tag)
async def payoff_process_worker(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await back_to_menu(message, state)
        return
    
    worker_tag = message.text.strip()
    
    if not worker_tag.startswith('@'):
        worker_tag = '@' + worker_tag
    
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username FROM users WHERE username LIKE ?", (f"%{worker_tag[1:]}%",))
    worker = cursor.fetchone()
    conn.close()
    
    if not worker:
        await message.answer("❌ Воркер не найден! Проверьте тег.")
        return
    
    worker_id, worker_username = worker
    
    stats = get_user_stats(worker_id)
    balance = stats['balance']
    unsubscribed_count = stats['unsubscribed_count']
    
    if balance <= 0:
        await message.answer(
            f"📊 У воркера @{worker_username} нет средств для списания.\n"
            f"Баланс за переведенных: 0$"
        )
        await state.finish()
        await message.answer(
            "👋 Добро пожаловать в главное меню!\n\n"
            "Используй кнопки, чтобы:\n"
            "🦣 Ввести тег мамонта\n"
            "🔎 Проверить мамонтов\n"
            "📊 Посмотреть свою статистику\n\n"
            "Также сюда приходят уведомления о профите 💰",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    await state.update_data(worker_id=worker_id, worker_username=worker_username, amount=balance, unsubscribed_count=unsubscribed_count)
    await PayoffState.waiting_for_confirmation.set()
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✅ Выплатил", callback_data="confirm_payoff"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_payoff")
    )
    
    await message.answer(
        f"📊 Информация о списании:\n\n"
        f"👤 Воркер: @{worker_username}\n"
        f"🦣 Отписавшихся мамонтов: {unsubscribed_count}\n"
        f"💲 Баланс к списанию: {balance:.2f}$\n\n"
        f"Подтвердите списание:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == "confirm_payoff", state=PayoffState.waiting_for_confirmation)
async def confirm_payoff(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    worker_id = data.get('worker_id')
    worker_username = data.get('worker_username')
    amount = data.get('amount')
    
    if not worker_id or not amount:
        await callback_query.message.edit_text("❌ Ошибка! Попробуйте заново.")
        await state.finish()
        await callback_query.answer()
        return
    
    add_payoff(worker_id, amount)
    
    await state.finish()
    await callback_query.message.edit_text(
        f"✅ Списание выполнено!\n\n"
        f"👤 Воркер: @{worker_username}\n"
        f"💲 Сумма: {amount:.2f}$\n"
        f"📅 Дата: {get_current_time().strftime('%d.%m.%Y %H:%M')}"
    )
    
    await bot.send_message(
        worker_id,
        f"📢 Вам начислена выплата за переведенных мамонтов!\n\n"
        f"💰 Сумма: {amount:.2f}$"
    )
    
    await callback_query.answer("✅ Списание подтверждено!")
    await bot.send_message(
        callback_query.from_user.id, 
        "👋 Добро пожаловать в главное меню!\n\n"
        "Используй кнопки, чтобы:\n"
        "🦣 Ввести тег мамонта\n"
        "🔎 Проверить мамонтов\n"
        "📊 Посмотреть свою статистику\n\n"
        "Также сюда приходят уведомления о профите 💰",
        reply_markup=get_main_keyboard(callback_query.from_user.id)
    )

@dp.callback_query_handler(lambda c: c.data == "cancel_payoff", state=PayoffState.waiting_for_confirmation)
async def cancel_payoff(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await callback_query.message.edit_text("❌ Списание отменено.")
    await callback_query.answer()
    await bot.send_message(
        callback_query.from_user.id, 
        "👋 Добро пожаловать в главное меню!\n\n"
        "Используй кнопки, чтобы:\n"
        "🦣 Ввести тег мамонта\n"
        "🔎 Проверить мамонтов\n"
        "📊 Посмотреть свою статистику\n\n"
        "Также сюда приходят уведомления о профите 💰",
        reply_markup=get_main_keyboard(callback_query.from_user.id)
    )

@dp.message_handler(lambda message: message.text == "📊 Личная статистика")
async def personal_stats(message: types.Message):
    user_id = message.from_user.id
    stats = get_user_stats(user_id)
    
    text = f"📊 Ваша личная статистика:\n\n"
    text += f"💵 Сумма профитов: ${stats['total_profit_usd']:.2f}\n"
    text += f"🦣 Количество мамонтов: {stats['clients_count']}\n"
    text += f"📝 Количество переведенных: {stats['unsubscribed_count']}\n"
    text += f"🗂️ Баланс за переведенных: ${stats['balance']:.2f}\n"
    text += f"💸 Заработок с переведенных: ${stats['total_payoffs']:.2f}"
    
    await message.answer(text, reply_markup=get_main_keyboard(user_id))

@dp.message_handler(lambda message: message.text == "📈 Статистика за неделю")
async def team_weekly_stats(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    stats = get_team_weekly_stats()
    
    text = f"📈 Статистика команды за неделю:\n\n"
    
    if stats['daily']:
        for day in stats['daily'][:7]:
            date = datetime.strptime(day['date'], "%Y-%m-%d").strftime("%d.%m")
            profit = day['profit']
            payments = day['payments']
            bars = int(profit / 5) if profit > 0 else 0
            bar_str = "█" * min(bars, 40)
            text += f"📅 {date}: {profit:.2f}$ ({payments:.2f}$) {bar_str}\n"
    else:
        text += "За неделю нет данных.\n"
    
    text += f"\n📊 Итого за неделю:\n"
    text += f"👥 Активных воркеров: {stats['active_workers']}\n"
    text += f"💰 Общий профит: ${stats['total_profit']:.2f}\n"
    text += f"💵 Общая сумма оплат: ${stats['total_payments']:.2f}\n"
    text += f"🦣 Новых мамонтов: {stats['new_clients']}\n"
    text += f"📝 Отписавшихся: {stats['unsubscribed']}"
    
    await message.answer(text, reply_markup=get_main_keyboard(user_id))

@dp.message_handler(lambda message: message.text == "📊 Статистика команды")
async def team_stats(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user[3]:
        await message.answer("❌ У вас нет прав для этого действия!")
        return
    
    stats = get_team_stats()
    
    text = f"📊 Статистика команды:\n\n"
    text += f"👥 Активных воркеров: {stats['active_workers']}\n"
    text += f"💰 Общая сумма оплат: ${stats['total_payments_usd']:.2f}\n"
    text += f"💵 Общая сумма профитов: ${stats['total_profit_usd']:.2f}\n"
    text += f"🦣 Всего мамонтов: {stats['total_clients']}\n"
    text += f"📉 Всего отписавшихся: {stats['total_unsubscribed']}"
    
    await message.answer(text, reply_markup=get_main_keyboard(user_id))

@dp.message_handler()
async def handle_other_messages(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await message.answer(
            "⏳ Пожалуйста, завершите текущее действие или нажмите '◀️ Назад'"
        )
    else:
        user_id = message.from_user.id
        await message.answer(
            "👋 Добро пожаловать в главное меню!\n\n"
            "Используй кнопки, чтобы:\n"
            "🦣 Ввести тег мамонта\n"
            "🔎 Проверить мамонтов\n"
            "📊 Посмотреть свою статистику\n\n"
            "Также сюда приходят уведомления о профите 💰",
            reply_markup=get_main_keyboard(user_id)
        )

if __name__ == "__main__":
    init_db()
    print("🚀 Бот запущен!")
    
    loop = asyncio.get_event_loop()
    loop.create_task(send_reminder())
    loop.create_task(check_deadlines())
    
    executor.start_polling(dp, skip_updates=True)