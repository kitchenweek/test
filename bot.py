import logging
import random
from typing import List, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Глобальные переменные для хранения состояния пользователей
user_data: Dict[int, Dict[str, Any]] = {}

# Константы
VIEWS_STAGE1 = [1300, 1400, 1500, 1600]
VIEWS_STAGE2 = [100, 200, 300]
VIEWS_STAGE3 = {
    9: 200,
    8: 400,
    6: 500,
    4: 700,
    3: 900,
    2: 1300
}
MAX_REPEATS = 4  # Максимальное количество повторений этапа 2

class NumberBot:
    @staticmethod
    def parse_numbers(text: str) -> List[int]:
        """Извлекает все числа из текста"""
        import re
        numbers = re.findall(r'\d+', text)
        return [int(num) for num in numbers]
    
    @staticmethod
    def create_groups(numbers: List[int], group_size: int = 40) -> List[List[int]]:
        """Разбивает список чисел на группы по указанному размеру"""
        groups = []
        for i in range(0, len(numbers), group_size):
            groups.append(numbers[i:i+group_size])
        return groups
    
    @staticmethod
    def format_group_message(groups: List[List[int]], views: List[int], stage: int, group_index: int = None) -> str:
        """Форматирует сообщение с группами (моноширный шрифт для цифр)"""
        message = f"📊 Этап {stage}\n\n"
        
        if group_index is not None:
            # Для пагинации (этап 1)
            if group_index < len(groups):
                group = groups[group_index]
                view_count = views[group_index] if group_index < len(views) else views[-1]
                message += f"📌 Группа {group_index + 1} - {view_count} просмотров\n"
                # Моноширный шрифт для цифр
                digits_str = ' '.join(map(str, group))
                message += f"📝 Цифры: <code>{digits_str}</code>\n"
                message += f"📊 Количество: {len(group)} цифр"
        else:
            # Для всех групп (этап 2)
            for i, group in enumerate(groups):
                view_count = views[i] if i < len(views) else views[-1]
                message += f"📌 Группа {i + 1} - {view_count} просмотров\n"
                # Моноширный шрифт для цифр
                digits_str = ' '.join(map(str, group))
                message += f"📝 Цифры: <code>{digits_str}</code>\n"
                message += f"📊 Количество: {len(group)} цифр\n\n"
        
        return message
    
    @staticmethod
    def format_stage3_message(numbers: List[int], count: int) -> str:
        """Форматирует сообщение для этапа 3"""
        views = VIEWS_STAGE3.get(count, 0)
        digits_str = ' '.join(map(str, numbers))
        
        message = f"📊 Этап 3\n\n"
        message += f"📌 Последние {count} номеров - {views} просмотров\n"
        message += f"📝 Цифры: <code>{digits_str}</code>\n"
        message += f"📊 Количество: {count} цифр"
        
        return message
    
    @staticmethod
    def get_keyboard_stage1(total_groups: int, current_index: int) -> InlineKeyboardMarkup:
        """Создает клавиатуру для этапа 1 с пагинацией"""
        keyboard = []
        
        # Навигационные кнопки
        nav_row = []
        if current_index > 0:
            nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"prev_{current_index}"))
        if current_index < total_groups - 1:
            nav_row.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"next_{current_index}"))
        
        if nav_row:
            keyboard.append(nav_row)
        
        # Кнопка завершения этапа (только на последней группе)
        if current_index == total_groups - 1:
            keyboard.append([InlineKeyboardButton("✅ Завершить этап", callback_data="finish_stage1")])
        
        # Кнопка загрузки номеров
        keyboard.append([InlineKeyboardButton("📥 Загрузить номера", callback_data="load_numbers")])
        
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def get_keyboard_stage2() -> InlineKeyboardMarkup:
        """Создает клавиатуру для этапа 2"""
        keyboard = [
            [InlineKeyboardButton("▶️ Далее", callback_data="next_stage2")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_stage1")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def get_keyboard_stage3() -> InlineKeyboardMarkup:
        """Создает клавиатуру для этапа 3"""
        keyboard = [
            [InlineKeyboardButton("▶️ Далее", callback_data="next_stage3")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_stage2")]
        ]
        return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    user_data[user_id] = {
        'stage': 0,  # 0 - ожидание загрузки, 1 - этап 1, 2 - этап 2, 3 - этап 3
        'original_numbers': [],  # Исходный список (никогда не меняется)
        'current_numbers': [],   # Текущий список (меняется на каждом этапе 2)
        'current_group_index': 0,
        'repeat_count': 0,
        'groups_stage1': [],
        'groups_stage2': [],
        'removed_counts': [],  # История удаленных цифр
        'stage3_index': 0,  # Индекс для этапа 3 (0-5)
        'stage3_counts': [9, 8, 6, 4, 3, 2]  # Последовательность количества цифр
    }
    
    await update.message.reply_text(
        "👋 Привет! Я бот для обработки номеров.\n\n"
        "📤 Отправьте мне список цифр (от 115 до 125 цифр) любым способом.\n"
        "Цифры могут быть в любом формате (в ряд, с пробелами, с переносами строк и т.д.)",
        parse_mode='HTML'
    )

async def handle_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик получения чисел"""
    user_id = update.effective_user.id
    
    if user_id not in user_data:
        user_data[user_id] = {
            'stage': 0,
            'original_numbers': [],
            'current_numbers': [],
            'current_group_index': 0,
            'repeat_count': 0,
            'groups_stage1': [],
            'groups_stage2': [],
            'removed_counts': [],
            'stage3_index': 0,
            'stage3_counts': [9, 8, 6, 4, 3, 2]
        }
    
    # Парсим числа из сообщения
    numbers = NumberBot.parse_numbers(update.message.text)
    
    if len(numbers) < 115 or len(numbers) > 125:
        await update.message.reply_text(
            f"❌ Ошибка! Вы отправили {len(numbers)} цифр.\n"
            "Необходимо отправить от 115 до 125 цифр.\n\n"
            "Пожалуйста, отправьте корректное количество цифр.",
            parse_mode='HTML'
        )
        return
    
    # Сохраняем данные
    user_data[user_id]['original_numbers'] = numbers.copy()
    user_data[user_id]['current_numbers'] = numbers.copy()  # Текущий список = исходный
    user_data[user_id]['stage'] = 1
    user_data[user_id]['repeat_count'] = 0
    user_data[user_id]['removed_counts'] = []
    user_data[user_id]['stage3_index'] = 0
    
    # Создаем группы для этапа 1
    groups = NumberBot.create_groups(numbers, 40)
    user_data[user_id]['groups_stage1'] = groups
    
    # Показываем первую группу
    await show_stage1_group(update, context, user_id, 0)

async def show_stage1_group(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, index: int) -> None:
    """Показывает группу на этапе 1"""
    data = user_data[user_id]
    groups = data['groups_stage1']
    
    if index < 0 or index >= len(groups):
        return
    
    message = NumberBot.format_group_message(groups, VIEWS_STAGE1, 1, index)
    keyboard = NumberBot.get_keyboard_stage1(len(groups), index)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message, 
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(
            message, 
            reply_markup=keyboard,
            parse_mode='HTML'
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    user_id = update.effective_user.id
    callback_data = query.data
    
    if user_id not in user_data:
        await query.answer("❌ Сессия истекла. Используйте /start")
        return
    
    user_info = user_data[user_id]
    
    # Проверяем тип callback_data
    if callback_data.startswith('prev_'):
        # Навигация назад
        try:
            current = int(callback_data.split('_')[1])
            new_index = current - 1
            if new_index >= 0:
                user_info['current_group_index'] = new_index
                await show_stage1_group(update, context, user_id, new_index)
        except (IndexError, ValueError) as e:
            logger.error(f"Error parsing prev_ callback: {e}")
            await query.answer("❌ Ошибка навигации")
    
    elif callback_data.startswith('next_'):
        # Проверяем, что это не 'next_stage2' или 'next_stage3'
        if callback_data == 'next_stage2':
            # Переход к следующей итерации этапа 2
            await next_stage2_iteration(update, context, user_id)
        elif callback_data == 'next_stage3':
            # Переход к следующему шагу этапа 3
            await next_stage3_step(update, context, user_id)
        else:
            # Навигация вперед (next_0, next_1, etc.)
            try:
                current = int(callback_data.split('_')[1])
                new_index = current + 1
                if new_index < len(user_info['groups_stage1']):
                    user_info['current_group_index'] = new_index
                    await show_stage1_group(update, context, user_id, new_index)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing next_ callback: {e}")
                await query.answer("❌ Ошибка навигации")
    
    elif callback_data == 'finish_stage1':
        # Завершение этапа 1 и переход к этапу 2
        await start_stage2(update, context, user_id)
    
    elif callback_data == 'load_numbers':
        # Загрузка номеров
        await query.edit_message_text(
            "📥 Отправьте новый список номеров\n"
            "Требования: от 115 до 125 цифр",
            parse_mode='HTML'
        )
        user_info['stage'] = 0  # Ожидание новой загрузки
    
    elif callback_data == 'back_to_stage1':
        # Возврат к этапу 1
        await back_to_stage1(update, context, user_id)
    
    elif callback_data == 'back_to_stage2':
        # Возврат к этапу 2
        await back_to_stage2(update, context, user_id)
    
    elif callback_data == 'restart':
        # Перезапуск
        await restart_command(update, context)
    
    else:
        logger.warning(f"Unknown callback data: {callback_data}")
        await query.answer("❌ Неизвестная команда")
    
    await query.answer()

async def start_stage2(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Запускает этап 2"""
    data = user_data[user_id]
    
    # Берем текущий список (уже измененный на предыдущих итерациях)
    current_numbers = data['current_numbers'].copy()
    
    # Убираем 5-8 цифр случайным образом
    remove_count = random.randint(5, 8)
    
    # Проверяем, что можно удалить столько цифр
    if len(current_numbers) <= remove_count:
        remove_count = max(1, len(current_numbers) - 1)  # Оставляем хотя бы 1 цифру
    
    if len(current_numbers) > remove_count:
        indices_to_remove = sorted(random.sample(range(len(current_numbers)), remove_count), reverse=True)
        for idx in indices_to_remove:
            current_numbers.pop(idx)
    
    # Сохраняем новый список
    data['current_numbers'] = current_numbers
    data['repeat_count'] += 1
    data['removed_counts'].append(remove_count)
    
    # Создаем группы для этапа 2
    groups = NumberBot.create_groups(current_numbers, 40)
    data['groups_stage2'] = groups
    
    # Показываем все группы
    message = NumberBot.format_group_message(groups, VIEWS_STAGE2, 2)
    message += f"\n🔄 Повторение {data['repeat_count']} из {MAX_REPEATS}"
    message += f"\n📊 Удалено цифр: {remove_count}"
    message += f"\n📊 Осталось цифр: {len(current_numbers)}"
    
    # Добавляем историю удалений
    if data['removed_counts']:
        history = ' | '.join([f"#{i+1}: {cnt}" for i, cnt in enumerate(data['removed_counts'])])
        message += f"\n📋 История удалений: {history}"
    
    # Проверяем, закончились ли повторения
    if data['repeat_count'] >= MAX_REPEATS:
        # Переходим к этапу 3
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Перейти к этапу 3", callback_data="next_stage3")
        ]])
        message += "\n\n✅ Все повторения этапа 2 завершены!"
        message += "\nНажмите 'Перейти к этапу 3' для продолжения"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                message, 
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                message, 
                reply_markup=keyboard,
                parse_mode='HTML'
            )
    else:
        keyboard = NumberBot.get_keyboard_stage2()
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                message, 
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                message, 
                reply_markup=keyboard,
                parse_mode='HTML'
            )

async def next_stage2_iteration(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Переходит к следующей итерации этапа 2"""
    data = user_data[user_id]
    
    if data['repeat_count'] >= MAX_REPEATS:
        # Переходим к этапу 3
        await start_stage3(update, context, user_id)
        return
    
    # Запускаем следующую итерацию
    await start_stage2(update, context, user_id)

async def start_stage3(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Запускает этап 3"""
    data = user_data[user_id]
    data['stage'] = 3
    data['stage3_index'] = 0
    
    await show_stage3_step(update, context, user_id)

async def show_stage3_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Показывает текущий шаг этапа 3"""
    data = user_data[user_id]
    index = data['stage3_index']
    counts = data['stage3_counts']
    
    if index >= len(counts):
        # Все шаги этапа 3 завершены
        await finish_stage3(update, context, user_id)
        return
    
    count = counts[index]
    original_numbers = data['original_numbers']
    
    # Берем последние count цифр из исходного списка
    last_numbers = original_numbers[-count:] if count <= len(original_numbers) else original_numbers
    
    message = NumberBot.format_stage3_message(last_numbers, count)
    
    keyboard = NumberBot.get_keyboard_stage3()
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message, 
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            message, 
            reply_markup=keyboard,
            parse_mode='HTML'
        )

async def next_stage3_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Переходит к следующему шагу этапа 3"""
    data = user_data[user_id]
    data['stage3_index'] += 1
    
    await show_stage3_step(update, context, user_id)

async def finish_stage3(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Завершает этап 3"""
    data = user_data[user_id]
    
    message = "✅ Все этапы завершены!\n\n"
    message += "📊 Итоговая статистика:\n"
    message += f"📌 Исходное количество цифр: {len(data['original_numbers'])}\n"
    message += f"📌 Повторений этапа 2: {data['repeat_count']}\n"
    
    if data['removed_counts']:
        message += "📌 История удалений:\n"
        for i, cnt in enumerate(data['removed_counts'], 1):
            message += f"  - Повторение {i}: удалено {cnt} цифр\n"
    
    message += f"📌 Итоговое количество цифр после этапа 2: {len(data['current_numbers'])}\n"
    message += f"📌 Всего просмотрено комбинаций: {len(data['stage3_counts'])}\n\n"
    message += "🔄 Для начала заново используйте /start"
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Начать заново", callback_data="restart")
    ]])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=keyboard,
            parse_mode='HTML'
        )

async def back_to_stage1(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Возвращает к этапу 1"""
    data = user_data[user_id]
    # Восстанавливаем исходный список
    data['current_numbers'] = data['original_numbers'].copy()
    data['repeat_count'] = 0
    data['stage'] = 1
    data['removed_counts'] = []
    data['stage3_index'] = 0
    
    groups = NumberBot.create_groups(data['original_numbers'], 40)
    data['groups_stage1'] = groups
    
    await show_stage1_group(update, context, user_id, 0)

async def back_to_stage2(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Возвращает к этапу 2"""
    data = user_data[user_id]
    data['stage'] = 2
    
    # Показываем последний результат этапа 2
    groups = data['groups_stage2']
    current_numbers = data['current_numbers']
    
    message = NumberBot.format_group_message(groups, VIEWS_STAGE2, 2)
    message += f"\n🔄 Повторение {data['repeat_count']} из {MAX_REPEATS}"
    message += f"\n📊 Осталось цифр: {len(current_numbers)}"
    
    if data['removed_counts']:
        history = ' | '.join([f"#{i+1}: {cnt}" for i, cnt in enumerate(data['removed_counts'])])
        message += f"\n📋 История удалений: {history}"
    
    keyboard = NumberBot.get_keyboard_stage2()
    
    await update.callback_query.edit_message_text(
        message,
        reply_markup=keyboard,
        parse_mode='HTML'
    )

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки рестарта"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Сбрасываем все данные
    user_data[user_id] = {
        'stage': 0,
        'original_numbers': [],
        'current_numbers': [],
        'current_group_index': 0,
        'repeat_count': 0,
        'groups_stage1': [],
        'groups_stage2': [],
        'removed_counts': [],
        'stage3_index': 0,
        'stage3_counts': [9, 8, 6, 4, 3, 2]
    }
    
    await query.edit_message_text(
        "🔄 Начинаем заново!\n\n"
        "📤 Отправьте мне список цифр (от 115 до 125 цифр) любым способом.",
        parse_mode='HTML'
    )
    await query.answer()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help"""
    help_text = """
🤖 Помощь по боту:

1️⃣ Отправьте список из 115-125 цифр
2️⃣ Этап 1: Разбивка на группы по 40 цифр с пагинацией
3️⃣ Этап 2: 4 повторения удаления 5-8 цифр и разбивка на группы
4️⃣ Этап 3: Показ последних N цифр с разными просмотрами:
   • 9 цифр - 200 просмотров
   • 8 цифр - 400 просмотров
   • 6 цифр - 500 просмотров
   • 4 цифры - 700 просмотров
   • 3 цифры - 900 просмотров
   • 2 цифры - 1300 просмотров

Команды:
/start - Начать работу
/help - Показать эту справку
    """
    await update.message.reply_text(help_text, parse_mode='HTML')

def main() -> None:
    """Запуск бота"""
    # Токен бота
    application = Application.builder().token("8623083352:AAHPhZkAFymFxs272OO_YYECCeXQUXfH8is").build()
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_numbers))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()