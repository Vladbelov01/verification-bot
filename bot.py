#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Verification Bot
Бот для верификации пользователей перед доступом к приватной группе

⚠️ ДИСКЛЕЙМЕР: Бот не хранит видеосообщения и персональные данные пользователей.
Вся информация является посредником между администрацией и пользователем.
Видеосообщения пересылаются администраторам для проверки и не сохраняются на сервере.
"""

import logging
import random
import sqlite3
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)
from telegram.constants import ChatType

# ==================== НАСТРОЙКИ ====================

# Токен бота (получить у @BotFather)
TOKEN = os.environ.get("TOKEN", "")
# ID администраторов (узнать через @userinfobot или @getidsbot)
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x]

# Ссылка на приватную группу (пригласительная ссылка)
PRIVATE_GROUP_LINK = os.environ.get("PRIVATE_GROUP_LINK", "")

# ID чата для пересылки кружочков (None = отправлять админам в ЛС)
MODERATION_CHAT_ID = os.environ.get("MODERATION_CHAT_ID", "")

# Файл базы данных
DB_FILE = "verification_bot.db"

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== СОСТОЯНИЯ ====================

(STATE_START, STATE_BIRTHDATE, STATE_EMOJI, STATE_VIDEO_NOTE) = range(4)

# ==================== БАЗА ДАННЫХ ====================

def init_db():
    """Инициализация SQLite базы данных"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            birthdate TEXT,
            age INTEGER,
            emoji TEXT,
            status TEXT DEFAULT 'pending',
            verification_date TEXT,
            rejection_reason TEXT,
            message_id INTEGER,
            admin_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def save_user(user_id, username, first_name, last_name, 
              birthdate=None, age=None, emoji=None, status='pending', message_id=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users 
        (user_id, username, first_name, last_name, birthdate, age, emoji, status, message_id, verification_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name, birthdate, age, emoji, status, 
          message_id, datetime.now().isoformat() if status != 'pending' else None))
    conn.commit()
    conn.close()

def update_user_status(user_id, status, admin_id=None, rejection_reason=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if status == 'verified':
        cursor.execute('''
            UPDATE users SET status = ?, verification_date = ?, admin_id = ? 
            WHERE user_id = ?
        ''', (status, datetime.now().isoformat(), admin_id, user_id))
    elif status == 'rejected':
        cursor.execute('''
            UPDATE users SET status = ?, rejection_reason = ?, admin_id = ? 
            WHERE user_id = ?
        ''', (status, rejection_reason, admin_id, user_id))
    conn.commit()
    conn.close()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def calculate_age(birthdate: str) -> int:
    try:
        birth = datetime.strptime(birthdate, "%d.%m.%Y")
        today = datetime.today()
        age = today.year - birth.year
        if (today.month, today.day) < (birth.month, birth.day):
            age -= 1
        return age
    except:
        return None

def generate_emoji() -> str:
    hand_emojis = [
        "👍", "👎", "👌", "🤌", "🤏", "✌️", "🤞", "🤟", "🤘", "🤙"  "👆", "🖕", "☝️","🤚", "🖐️",
        "🖖", "🫰"
    ]
    return random.choice(hand_emojis)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт - проверка статуса пользователя"""
    user = update.effective_user
    user_data = get_user(user.id)
    
    # Если пользователь уже верифицирован - сразу даём ссылку
    if user_data and user_data['status'] == 'verified':
        await update.message.reply_text(
            f"✅ Ты уже прошёл верификацию! Вот ссылка на группу:\n\n{PRIVATE_GROUP_LINK}"
        )
        return ConversationHandler.END
    
    # Если отклонён - предлагаем связаться с админом
    if user_data and user_data['status'] == 'rejected':
        keyboard = [[InlineKeyboardButton("📞 Связаться с администрацией", 
                    url=f"tg://user?id={ADMIN_IDS[0]}")]]
        await update.message.reply_text(
            "❌ Твоя верификация была отклонена.\n\n"
            "Если ты считаешь, что произошла ошибка, нажми кнопку ниже:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    
    # Если уже в процессе - продолжаем
    if user_data and user_data['status'] == 'pending':
        await update.message.reply_text(
            "⏳ Ты уже начал верификацию. Дождись проверки администратором."
        )
        return ConversationHandler.END
    
    # Новый пользователь - начинаем верификацию
    await update.message.reply_text(
        "👋 Привет! Для доступа к приватной группе нужно пройти верификацию.\n\n"
        "⚠️ Бот является посредником между тобой и администрацией. "
        "Твои данные и видеосообщения не хранятся на сервере, а лишь пересылаются администраторам для проверки.\n\n"
        "Шаг 1/3: Напиши свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Пример: 15.03.1995"
    )
    return STATE_BIRTHDATE

async def get_birthdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение даты рождения"""
    text = update.message.text.strip()
    age = calculate_age(text)
    
    if age is None or age < 0 or age > 120:
        await update.message.reply_text(
            "❌ Неверный формат. Введи дату в формате ДД.ММ.ГГГГ\n"
            "Пример: 15.03.1995"
        )
        return STATE_BIRTHDATE
    
    # Сохраняем данные во временном хранилище
    context.user_data['birthdate'] = text
    context.user_data['age'] = age
    
    # Генерируем случайный смайлик
    emoji = generate_emoji()
    context.user_data['emoji'] = emoji
    
    await update.message.reply_text(
        f"📅 Возраст: {age} лет\n\n"
        f"Шаг 2/3: Твой персональный смайлик для верификации: {emoji}\n\n"
        f"Шаг 3/3: Запиши кружок (видео-сообщение), где ты показываешь "
        f"этот смайлик руками или держишь его на листочке рядом с лицом.\n\n"
        f"⚠️ Важно: отправь именно кружок (круглое видео), а не обычное видео или фото!"
    )
    return STATE_VIDEO_NOTE

async def get_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение кружочка и отправка на модерацию"""
    user = update.effective_user
    video_note = update.message.video_note
    
    # Сохраняем пользователя в БД
    save_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        birthdate=context.user_data.get('birthdate'),
        age=context.user_data.get('age'),
        emoji=context.user_data.get('emoji'),
        status='pending'
    )
    
    # Отправляем кружок админам на проверку
    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{user.id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user.id}")
        ]
    ])
    
    moderation_text = (
        f"🔍 Новая верификация!\n\n"
        f"👤 Пользователь: {user.first_name} {user.last_name or ''}\n"
        f"🆔 ID: {user.id}\n"
        f"📛 Username: @{user.username or 'нет'}\n"
        f"📅 Дата рождения: {context.user_data.get('birthdate')}\n"
        f"🔢 Возраст: {context.user_data.get('age')} лет\n"
        f"😀 Смайлик: {context.user_data.get('emoji')}\n\n"
        f"Проверь кружок и нажми решение:"
    )
    
    # Отправляем в чат модерации или всем админам
    if MODERATION_CHAT_ID:
        await context.bot.send_message(
            chat_id=MODERATION_CHAT_ID,
            text=moderation_text,
            reply_markup=admin_keyboard
        )
        # Пересылаем кружок
        await context.bot.forward_message(
            chat_id=MODERATION_CHAT_ID,
            from_chat_id=user.id,
            message_id=update.message.message_id
        )
    else:
        # Отправляем каждому админу
        for admin_id in ADMIN_IDS:
            try:
                msg = await context.bot.send_message(
                    chat_id=admin_id,
                    text=moderation_text,
                    reply_markup=admin_keyboard
                )
                # Пересылаем кружок
                await context.bot.forward_message(
                    chat_id=admin_id,
                    from_chat_id=user.id,
                    message_id=update.message.message_id
                )
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")
    
    await update.message.reply_text(
        "⏳ Кружок отправлен на проверку администратору.\n"
        "Обычно проверка занимает несколько минут. Я пришлю результат!"
    )
    return ConversationHandler.END

async def wrong_message_in_video_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка НЕправильных сообщений в состоянии ожидания кружка"""
    emoji = context.user_data.get('emoji', '❓')
    
    await update.message.reply_text(
        f"❌ Это не кружок!\n\n"
        f"Ты должен отправить именно кружок (круглое видео-сообщение), "
        f"где показываешь смайлик {emoji}.\n\n"
        f"⚠️ Подсказка: в Telegram зажми кнопку микрофона и свайпни вверх, "
        f"чтобы записать кружок. Или нажми скрепку → Видеосообщение.\n\n"
        f"Попробуй ещё раз!"
    )
    return STATE_VIDEO_NOTE  # Остаёмся в том же состоянии

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена верификации"""
    await update.message.reply_text(
        "❌ Верификация отменена. Напиши /start чтобы начать заново."
    )
    return ConversationHandler.END

# ==================== ОБРАБОТКА КНОПОК АДМИНА ====================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка решений администратора"""
    query = update.callback_query
    await query.answer()
    
    # Проверяем, что нажал админ
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("⛔ У тебя нет прав для этого действия.")
        return
    
    data = query.data
    action, user_id = data.split("_")
    user_id = int(user_id)
    
    if action == "approve":
        # Одобряем пользователя
        update_user_status(user_id, 'verified', update.effective_user.id)
        
        # Отправляем ссылку пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Верификация пройдена!\n\nВот ссылка на приватную группу:\n{PRIVATE_GROUP_LINK}\n\nДобро пожаловать! 🎉"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
        
        await query.edit_message_text(
            f"✅ Пользователь {user_id} одобрен.\nСсылка отправлена."
        )
        
    elif action == "reject":
        # Сохраняем ID пользователя для дальнейшего ввода причины
        context.user_data['reject_user_id'] = user_id
        await query.edit_message_text(
            "📝 Введи причину отклонения (или отправь '-' без причины):"
        )
        # Здесь можно добавить state для ввода причины, но для простоты:
        update_user_status(user_id, 'rejected', update.effective_user.id, "Не указана")
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ К сожалению, верификация отклонена.\n\n"
                     "Возможные причины:\n"
                     "- Несоответствие возраста\n"
                     "- Плохое качество кружка\n"
                     "- Смайлик не виден\n\n"
                     "Напиши /start чтобы попробовать снова."
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

# ==================== КОМАНДЫ АДМИНА ====================

async def admin_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение информации о пользователе по ID"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.args:
        await update.message.reply_text("Использование: /info <user_id>")
        return
    
    try:
        user_id = int(context.args[0])
        user_data = get_user(user_id)
        
        if not user_data:
            await update.message.reply_text("Пользователь не найден в базе.")
            return
        
        status_emoji = {
            'pending': '⏳',
            'verified': '✅',
            'rejected': '❌'
        }
        
        text = (
            f"📊 Информация о пользователе:\n\n"
            f"🆔 ID: {user_data['user_id']}\n"
            f"👤 Имя: {user_data['first_name']} {user_data['last_name'] or ''}\n"
            f"📛 Username: @{user_data['username'] or 'нет'}\n"
            f"📅 Дата рождения: {user_data['birthdate']}\n"
            f"🔢 Возраст: {user_data['age']}\n"
            f"😀 Смайлик: {user_data['emoji']}\n"
            f"📌 Статус: {status_emoji.get(user_data['status'], '❓')} {user_data['status']}\n"
            f"📅 Дата регистрации: {user_data['created_at']}\n"
        )
        
        if user_data['verification_date']:
            text += f"✅ Дата верификации: {user_data['verification_date']}\n"
        if user_data['rejection_reason']:
            text += f"❌ Причина отклонения: {user_data['rejection_reason']}\n"
        
        await update.message.reply_text(text)
        
    except ValueError:
        await update.message.reply_text("Неверный формат ID. Используй только цифры.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по пользователям"""
    if not is_admin(update.effective_user.id):
        return
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'verified'")
    verified = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'rejected'")
    rejected = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'")
    pending = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]
    
    conn.close()
    
    await update.message.reply_text(
        f"📊 Статистика верификации:\n\n"
        f"✅ Подтверждено: {verified}\n"
        f"❌ Отклонено: {rejected}\n"
        f"⏳ Ожидает: {pending}\n"
        f"📊 Всего: {total}"
    )

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех пользователей"""
    if not is_admin(update.effective_user.id):
        return
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, first_name, status, created_at FROM users ORDER BY created_at DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("База данных пуста.")
        return
    
    text = "📋 Последние 20 пользователей:\n\n"
    for row in rows:
        status = {'pending': '⏳', 'verified': '✅', 'rejected': '❌'}.get(row['status'], '❓')
        text += f"{status} {row['first_name']} (ID: {row['user_id']}) - {row['created_at'][:10]}\n"
    
    await update.message.reply_text(text)

# ==================== ЗАПУСК ====================

def main():
    # Инициализация БД
    init_db()
    
    # Создаём приложение
    application = Application.builder().token(TOKEN).build()
    
    # Conversation handler для верификации
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_BIRTHDATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_birthdate)
            ],
            STATE_VIDEO_NOTE: [
                MessageHandler(filters.VIDEO_NOTE, get_video_note),
                # Любое другое сообщение (фото, текст, обычное видео) — отклоняем
                MessageHandler(filters.ALL & ~filters.VIDEO_NOTE & ~filters.COMMAND, wrong_message_in_video_state)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Регистрация обработчиков
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve|reject)_"))
    application.add_handler(CommandHandler("info", admin_info))
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler("list", admin_list))
    
    # Запускаем бота и веб-сервер для Render
    import asyncio
    from aiohttp import web
    
    async def health_check(request):
        return web.Response(text="Bot is running!")
    
    async def start_web_server():
        app = web.Application()
        app.router.add_get('/', health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()
        print("🌐 Web server on port 8080")
    
    async def start_bot():
        print("🤖 Бот запущен!")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    async def main_async():
        await start_web_server()
        await start_bot()
        while True:
            await asyncio.sleep(3600)
    
    asyncio.run(main_async())

if __name__ == "__main__":
    main()