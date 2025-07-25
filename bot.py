# -*- coding: utf-8 -*-


import asyncio
import logging
import json
import sqlite3
import pandas as pd
import nest_asyncio
import re
import os # <-- ДОБАВИТЬ ЭТУ СТРОКУ

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
# ИЗМЕНЕНО: Добавляем FSM для управления состояниями
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


# --- НАСТРОЙКИ И ИНИЦИАЛИЗАЦИЯ ---

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)

# ИЗМЕНЕНО: Получаем токен из переменной окружения
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')

ADMIN_IDS = [830902845]

# ИЗМЕНЕНО: Указываем путь к постоянному хранилищу Amvera
DB_FILE = '/data/quiz_database.db'

class QuizState(StatesGroup):
    answering = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    clean_text = raw_html.replace('<p>', '').replace('</p>', '\n')
    clean_text = clean_text.replace('<b>', '').replace('</b>', '')
    clean_text = clean_text.replace('<i>', '').replace('</i>', '')
    clean_text = clean_text.replace('<code>', '').replace('</code>', '')
    return clean_text.strip()

def format_user_answer(text: str) -> str:
    return "".join(sorted(re.sub(r'[\s,.]', '', text).lower()))

# --- РАБОТА С БД ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS user_answers (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, question_id INTEGER, is_correct INTEGER, topic TEXT)''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, topic TEXT NOT NULL, sub_topic TEXT,
                question_type TEXT NOT NULL, author TEXT, source TEXT,
                question_text TEXT NOT NULL, options_json TEXT NOT NULL,
                correct_answer TEXT NOT NULL, explanation TEXT
            )''')
        conn.commit()

def add_user(user_id, username, first_name):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user_id, username, first_name))
        conn.commit()

def log_answer(user_id, question_id, is_correct, topic):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO user_answers (user_id, question_id, is_correct, topic) VALUES (?, ?, ?, ?)", (user_id, question_id, is_correct, topic))
        conn.commit()

def get_available_topics():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT topic FROM tasks ORDER BY topic")
        topics = [row[0] for row in cur.fetchall()]
    return topics

# --- АДМИН-ПАНЕЛЬ: Функции для сбора статистики (без изменений) ---
def get_summary_stats():
    """Собирает краткую сводную статистику по всем пользователям."""
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        # Общее число пользователей
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]

        # Общая статистика по ответам
        cur.execute("SELECT COUNT(*), SUM(is_correct) FROM user_answers")
        total_answers, total_correct = cur.fetchone()
        accuracy = (total_correct / total_answers) * 100 if total_answers and total_correct else 0

        # Топ-3 самых сложных тем
        cur.execute("SELECT topic, SUM(1 - is_correct) as errors FROM user_answers GROUP BY topic ORDER BY errors DESC LIMIT 3")
        worst_topics = cur.fetchall()

    return {"total_users": total_users, "total_answers": total_answers, "accuracy": accuracy, "worst_topics": worst_topics}

def create_excel_report():
    """Создает детальный Excel-отчет и возвращает путь к файлу."""
    with sqlite3.connect(DB_FILE) as conn:
        # Запрос для сводки по каждому ученику
        query_summary = """
            SELECT
                u.user_id, u.first_name, u.username,
                COUNT(ua.id) as total_answers,
                SUM(ua.is_correct) as correct_answers,
                (CAST(SUM(ua.is_correct) AS REAL) / COUNT(ua.id)) * 100 as accuracy
            FROM users u
            LEFT JOIN user_answers ua ON u.user_id = ua.user_id
            GROUP BY u.user_id ORDER BY accuracy DESC
        """
        df_summary = pd.read_sql_query(query_summary, conn)

        # Запрос для выгрузки всех ответов
        query_all_answers = """
            SELECT ua.id, ua.user_id, u.first_name, ua.question_id, ua.topic, ua.is_correct
            FROM user_answers ua
            JOIN users u ON ua.user_id = u.user_id
            ORDER BY ua.id
        """
        df_all_answers = pd.read_sql_query(query_all_answers, conn)

    # Создание Excel файла с двумя листами
    file_path = "student_stats_report.xlsx"
    with pd.ExcelWriter(file_path) as writer:
        df_summary.to_excel(writer, sheet_name='Сводка по ученикам', index=False)
        df_all_answers.to_excel(writer, sheet_name='Все ответы', index=False)

    return file_path



# --- БЛОК ВСПОМОГАТЕЛЬНЫХ ФУНКЦИЙ БОТА ---

async def send_question(message: types.Message, state: FSMContext, topic: str = None):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        query = "SELECT id, topic, question_text, options_json, correct_answer, explanation, question_type FROM tasks "
        if topic and topic != "random":
            cur.execute(query + "WHERE topic = ? ORDER BY RANDOM() LIMIT 1", (topic,))
        else:
            cur.execute(query + "ORDER BY RANDOM() LIMIT 1")
        question_row = cur.fetchone()

    if not question_row:
        await message.answer("В этой категории пока нет вопросов.")
        await state.clear()
        return

    q_id, q_topic, q_text, q_options_json, q_correct, q_explanation, q_type = question_row

    cleaned_question_text = clean_html(q_text)
    question_text_formatted = f"<b>Тема: {q_topic}</b>\n\n<b>Вопрос {q_id}:</b>\n{cleaned_question_text}\n\n"

    options = json.loads(q_options_json)
    if options:
        for key, value in options.items():
            cleaned_option_text = clean_html(value)
            question_text_formatted += f"<b>{key.upper()}.</b> {cleaned_option_text}\n"

    if q_type in ["Верно/Неверно", "Один правильный ответ"]:
         question_text_formatted += "\n➡️ <i>Отправьте букву правильного ответа.</i>"
    elif q_type == "Все верные ответы":
         question_text_formatted += "\n➡️ <i>Отправьте буквы правильных ответов слитно (например, абв).</i>"
    else: # Открытый ответ
         question_text_formatted += "\n➡️ <i>Отправьте ваш ответ в виде текста или числа.</i>"

    await message.answer(question_text_formatted, parse_mode="HTML")

    await state.update_data(
        question_id=q_id,
        correct_answer=q_correct,
        topic=q_topic,
        explanation=q_explanation
    )
    await state.set_state(QuizState.answering)


# --- ОБРАБОТЧИКИ ---

@dp.message(CommandStart())
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.reply("Привет! Я твой помощник для подготовки к олимпиадам. 🏆\nЧтобы начать тест, используй команду /test")

@dp.message(Command("test"))
async def show_topics_menu(message: types.Message, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder()
    available_topics = get_available_topics()
    for index, topic in enumerate(available_topics):
        builder.button(text=topic, callback_data=f"topic_idx:{index}")
    builder.button(text="🎲 Случайный тест", callback_data="topic_idx:random")
    builder.adjust(1)
    await message.answer("Выбери тему для теста:", reply_markup=builder.as_markup())

# ... (админ-панель и /stats без изменений) ...
# --- ОБРАБОТЧИК СТАТИСТИКИ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(Command("stats"))
async def show_stats(message: types.Message):
    """Показывает личную статистику пользователя."""
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), SUM(is_correct) FROM user_answers WHERE user_id = ?", (message.from_user.id,))
        total_answered, total_correct = cur.fetchone()

    if not total_answered:
        return await message.answer("Ты еще не ответил ни на один вопрос. Начни с команды /test")

    accuracy = (total_correct / total_answered) * 100 if total_correct else 0

    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT topic, SUM(1 - is_correct) as errors FROM user_answers WHERE user_id = ? GROUP BY topic ORDER BY errors DESC LIMIT 1", (message.from_user.id,))
        worst_topic_row = cur.fetchone()

    worst_topic = worst_topic_row[0] if worst_topic_row else "Не определена"

    stats_text = (
        f"📊 <b>Твоя статистика:</b>\n\n"
        f"Всего отвечено вопросов: {total_answered}\n"
        f"Правильных ответов: {total_correct}\n"
        f"Точность: {accuracy:.2f}%\n\n"
        f"Тема с наибольшим количеством ошибок: <b>{worst_topic}</b>\n\nПродолжай в том же духе!"
    )
    await message.answer(stats_text, parse_mode="HTML")


# --- АДМИН-ПАНЕЛЬ ---

@dp.message(Command("admin"))
async def show_admin_panel(message: types.Message):
    """Показывает админ-панель, если у пользователя есть доступ."""
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("У вас нет доступа к этой команде.")

    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Краткая статистика", callback_data="admin:summary")
    builder.button(text="📄 Подробный отчет (Excel)", callback_data="admin:excel")
    builder.adjust(1)
    await message.answer("Добро пожаловать в панель администратора!", reply_markup=builder.as_markup())


@dp.callback_query(lambda c: c.data.startswith('admin:'))
async def process_admin_commands(callback_query: CallbackQuery):
    """Обрабатывает нажатия на кнопки в админ-панели."""
    if callback_query.from_user.id not in ADMIN_IDS:
        return await bot.answer_callback_query(callback_query.id, "Доступ запрещен", show_alert=True)

    command = callback_query.data.split(':')[1]

    if command == 'summary':
        stats = get_summary_stats()
        worst_topics_text = "\n".join([f"  - {topic} ({errors} ошибок)" for topic, errors in stats['worst_topics']]) if stats['worst_topics'] else "Нет данных"
        summary_text = (
            f"📈 <b>Краткая статистика по всем ученикам:</b>\n\n"
            f"👤 Всего учеников: <b>{stats['total_users']}</b>\n"
            f"📝 Всего ответов: <b>{stats['total_answers']}</b>\n"
            f"🎯 Средняя точность: <b>{stats['accuracy']:.2f}%</b>\n\n"
            f"ურთ Самые сложные темы:\n{worst_topics_text}"
        )
        await callback_query.message.answer(summary_text, parse_mode="HTML")

    elif command == 'excel':
        await callback_query.message.answer("Пожалуйста, подождите, генерирую отчет...")
        report_path = create_excel_report()
        document = FSInputFile(report_path)
        await bot.send_document(callback_query.from_user.id, document, caption="Подробный отчет по успеваемости учеников.")

    await bot.answer_callback_query(callback_query.id)

@dp.message(QuizState.answering)
async def process_text_answer(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    question_id = user_data.get('question_id')
    correct_answer = user_data.get('correct_answer')
    topic = user_data.get('topic')
    explanation = user_data.get('explanation')

    await state.clear() # Сразу сбрасываем состояние, чтобы избежать повторной обработки

    user_answer_formatted = format_user_answer(message.text)
    correct_answer_formatted = format_user_answer(correct_answer)

    is_correct = 1 if user_answer_formatted == correct_answer_formatted else 0
    log_answer(message.from_user.id, question_id, is_correct, topic)

    if is_correct:
        await message.answer("✅ Абсолютно верно!")
    else:
        cleaned_explanation = clean_html(explanation)
        explanation_text = cleaned_explanation if cleaned_explanation else 'Объяснение отсутствует.'
        response_text = f"❌ Неверно.\nПравильный ответ: <b>{correct_answer.upper()}</b>\n\n<b>Объяснение:</b> {explanation_text}"
        await message.answer(response_text, parse_mode="HTML")

    # ИЗМЕНЕНО: Находим индекс текущей темы, чтобы передать его в кнопку "Следующий вопрос"
    all_topics = get_available_topics()
    try:
        topic_to_continue_index = all_topics.index(topic)
    except ValueError:
        # Если по какой-то причине темы нет в списке, предлагаем случайный вопрос
        topic_to_continue_index = "random"

    builder = InlineKeyboardBuilder()
    builder.button(text="➡️ Следующий вопрос", callback_data=f"topic_idx:{topic_to_continue_index}")
    builder.button(text="📋 Выбрать другую тему", callback_data="show_topics_menu")
    await message.answer("Готов продолжить?", reply_markup=builder.as_markup())


@dp.callback_query(lambda c: c.data.startswith('topic_idx:'))
async def process_topic_selection(callback_query: CallbackQuery, state: FSMContext):
    topic_param = callback_query.data.split(':')[1]

    # Защита от случайных сообщений, когда бот не ждет ответа
    if await state.get_state() is not None:
        await state.clear()

    await bot.edit_message_reply_markup(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id, reply_markup=None)

    if topic_param == "random":
        topic_name = "random"
    else:
        try:
            topic_index = int(topic_param)
            available_topics = get_available_topics()
            if 0 <= topic_index < len(available_topics):
                topic_name = available_topics[topic_index]
            else:
                return await callback_query.message.answer("Ошибка: неверный индекс темы.")
        except (ValueError, IndexError):
            return await callback_query.message.answer("Ошибка: неверный формат callback_data.")

    await send_question(callback_query.message, state, topic_name)
    await bot.answer_callback_query(callback_query.id)

@dp.callback_query(lambda c: c.data == 'show_topics_menu')
async def process_show_topics(callback_query: CallbackQuery, state: FSMContext):
     await bot.edit_message_reply_markup(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id, reply_markup=None)
     await show_topics_menu(callback_query.message, state)
     await bot.answer_callback_query(callback_query.id)

# --- ТОЧКА ВХОДА И ЗАПУСК БОТА ---

async def main():
    init_db()
    print("База данных инициализирована.")
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    if API_TOKEN == 'YOUR_API_TOKEN_HERE':
        print("Ошибка: Пожалуйста, вставьте ваш API токен в переменную API_TOKEN.")
    else:
        asyncio.run(main())

