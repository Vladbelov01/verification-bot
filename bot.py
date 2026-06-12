#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Verification Bot (Render-optimized)
Бот для верификации пользователей перед одобрением заявки на вступление в приватную группу
"""

import logging
import random
import sqlite3
import os
import traceback
import asyncio
import sys
from aiohttp import web
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    ChatJoinRequestHandler,
    filters
)

# ==================== НАСТРОЙКИ ====================

TOKEN = os.environ.get("TOKEN", "")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "").strip()
MODERATION_CHAT_ID = os.environ.get("MODERATION_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change_me_please")

# Render-specific
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
PORT = int(os.environ.get("PORT", "10000"))

DB_FILE = "verification_bot.db"

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== СОСТОЯНИЯ ====================

(STATE_START, STATE_BIRTHDATE, STATE_VIDEO_NOTE) = range(3)

# ==================== БАЗА ДАННЫХ ====================

def init_db():
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
            group_chat_id INTEGER,
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

def get_pending_users() -> list:
    """Получить всех пользователей со статусом pending"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE status = 'pending'")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def save_user(user_id, username, first_name, last_name, group_chat_id=None,
              birthdate=None, age=None, emoji=None, status='pending', message_id=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name))
    cursor.execute('''
        UPDATE users
        SET username = ?, first_name = ?, last_name = ?,
            group_chat_id = COALESCE(?, group_chat_id),
            birthdate = COALESCE(?, birthdate),
            age = COALESCE(?, age),
            emoji = COALESCE(?, emoji),
            status = ?,
            message_id = COALESCE(?, message_id)
        WHERE user_id = ?
    ''', (username, first_name, last_name,
          group_chat_id, birthdate, age, emoji,
          status, message_id, user_id))
    conn.commit()
    conn.close()

def reset_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users
        SET status = 'new', birthdate = NULL, age = NULL,
            emoji = NULL, rejection_reason = NULL,
            verification_date = NULL, admin_id = NULL
        WHERE user_id = ?
    ''', (user_id,))
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
    else:
        cursor.execute('''
            UPDATE users SET status = ?, admin_id = ?
            WHERE user_id = ?
        ''', (status, admin_id, user_id))
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
    except Exception:
        return None

def generate_emoji() -> str:
    hand_emojis = ["👍", "👎", "👌", "🤏", "✌️", "🤞", "🤟", "🤘", "🤙", "👆", "🖕", "☝️", "👋", "🤚", "🖐️", "✋", "🖖"]
    return random.choice(hand_emojis)

async def send_to_moderation(context: ContextTypes.DEFAULT_TYPE, user, moderation_text: str, message_id: int, admin_keyboard):
    if MODERATION_CHAT_ID:
        mod_chat_id = int(MODERATION_CHAT_ID) if MODERATION_CHAT_ID.lstrip('-').isdigit() else MODERATION_CHAT_ID
        await context.bot.send_message(chat_id=mod_chat_id, text=moderation_text, reply_markup=admin_keyboard)
        await context.bot.forward_message(chat_id=mod_chat_id, from_chat_id=user.id, message_id=message_id)
        return

    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS не заданы и MODERATION_CHAT_ID не задан!")

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=moderation_text, reply_markup=admin_keyboard)
            await context.bot.forward_message(chat_id=admin_id, from_chat_id=user.id, message_id=message_id)
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id}: {e}")

# ==================== ОБРАБОТКА НАКОПИВШИХСЯ ЗАЯВОК ====================

async def process_pending_users(application: Application):
    """Обрабатывает накопившиеся заявки при старте бота"""
    logger.info("🔍 Проверка накопившихся заявок...")
    
    pending_users = get_pending_users()
    
    if not pending_users:
        logger.info("✅ Нет накопившихся заявок в БД")
        return
    
    logger.info(f"📋 Найдено {len(pending_users)} незавершённых верификаций в БД")
    
    for user_data in pending_users:
        user_id = user_data['user_id']
        
        if not user_data.get('birthdate'):
            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "👋 Привет! Ты подал заявку на вступление, но не завершил верификацию.\n\n"
                        "Шаг 1/2: Напиши свою дату рождения в формате ДД.ММ.ГГГГ\n"
                        "Пример: 15.03.1995"
                    )
                )
                logger.info(f"📨 Отправлено напоминание (шаг 1) пользователю {user_id}")
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {user_id}: {e}")
                if "blocked" in str(e).lower() or "not found" in str(e).lower():
                    update_user_status(user_id, 'rejected', rejection_reason="Пользователь заблокировал бота")
        
        elif not user_data.get('emoji'):
            emoji = generate_emoji()
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET emoji = ? WHERE user_id = ?", (emoji, user_id))
            conn.commit()
            conn.close()
            
            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"👋 Привет! Ты не завершил верификацию.\n\n"
                        f"📅 Возраст: {user_data['age']} лет\n\n"
                        f"Шаг 2/2: Твой персональный смайлик: {emoji}\n\n"
                        f"Запиши кружок, где показываешь этот смайлик руками или на листочке рядом с лицом.\n\n"
                        f"⚠️ Отправь именно кружок (круглое видео), а не обычное видео!"
                    )
                )
                logger.info(f"📨 Отправлено напоминание (шаг 2) пользователю {user_id}")
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {user_id}: {e}")
        
        else:
            emoji = user_data.get('emoji', '❓')
            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"👋 Привет! Ты не завершил верификацию.\n\n"
                        f"📅 Возраст: {user_data['age']} лет\n"
                        f"😀 Смайлик: {emoji}\n\n"
                        f"⚠️ Остался последний шаг — отправь кружок (круглое видео), "
                        f"где показываешь смайлик {emoji} руками или на листочке рядом с лицом.\n\n"
                        f"Подсказка: зажми кнопку микрофона и свайпни вверх, или нажми скрепку → Видеосообщение."
                    )
                )
                logger.info(f"📨 Отправлено напоминание (ожидание кружка) пользователю {user_id}")
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {user_id}: {e}")

# ==================== ОТЛАДКА ГРУППЫ ====================

async def debug_check_bot_status(application: Application):
    """Проверяем статус бота в группе через API"""
    if not GROUP_CHAT_ID:
        logger.warning("⚠️ GROUP_CHAT_ID не задан, пропускаем проверку группы")
        return
    
    try:
        chat_id = int(GROUP_CHAT_ID)
        
        # Получаем информацию о группе
        chat = await application.bot.get_chat(chat_id)
        logger.info(f"✅ Бот видит группу: {chat.title} (ID: {chat.id}, type: {chat.type})")
        
        # Проверяем статус бота в группе
        bot_member = await application.bot.get_chat_member(chat_id, application.bot.id)
        logger.info(f"🤖 Статус бота в группе: {bot_member.status}")
        
        if bot_member.status != 'administrator':
            logger.error(f"❌❌❌ БОТ НЕ АДМИНИСТРАТОР В ГРУППЕ! Статус: {bot_member.status}")
            logger.error(f"❌❌❌ ChatJoinRequest НЕ БУДЕТ РАБОТАТЬ! Назначь бота админом!")
            return
        
        # Проверяем права админа
        if hasattr(bot_member, 'can_invite_users'):
            logger.info(f"🤖 can_invite_users: {bot_member.can_invite_users}")
        if hasattr(bot_member, 'can_restrict_members'):
            logger.info(f"🤖 can_restrict_members: {bot_member.can_restrict_members}")
        
        # Проверяем, включены ли заявки на вступление
        # Пробуем получить chat_join_request — если нет прав, упадёт с ошибкой
        try:
            # Это не прямой метод, но можно проверить через get_chat
            logger.info(f"🤖 Проверка настроек группы...")
            logger.info(f"🤖 invite_link: {chat.invite_link}")
            logger.info(f"🤖 has_protected_content: {getattr(chat, 'has_protected_content', 'N/A')}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить доп. инфо: {e}")
        
        logger.info(f"✅ Бот админ в группе, ChatJoinRequest должен работать")
        
    except Exception as e:
        logger.error(f"❌ Не удалось проверить группу: {e}")
        logger.error(f"❌ traceback: {traceback.format_exc()}")

# ==================== ОБРАБОТЧИКИ ====================

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ОБРАБОТКА ЗАЯВОК НА ВСТУПЛЕНИЕ"""
    
    logger.info(f"🚨 ChatJoinRequest ПОЛУЧЕН! update={update}")
    logger.info(f"🚨 update.chat_join_request = {update.chat_join_request}")
    
    try:
        join_request = update.chat_join_request
        user = join_request.from_user
        chat = join_request.chat
    except Exception as e:
        logger.error(f"❌ Ошибка получения chat_join_request: {e}")
        logger.error(f"❌ traceback: {traceback.format_exc()}")
        return

    logger.info(f"👤 Пользователь {user.id} (@{user.username}) подал заявку в группу {chat.id} ({chat.title})")

    # ФИЛЬТРАЦИЯ ПО GROUP_CHAT_ID
    if GROUP_CHAT_ID:
        try:
            expected_chat_id = int(GROUP_CHAT_ID)
            if chat.id != expected_chat_id:
                logger.info(f"⏭️ Игнорируем заявку из группы {chat.id} (ожидалась {expected_chat_id})")
                return
            logger.info(f"✅ Группа {chat.id} совпадает с GROUP_CHAT_ID")
        except ValueError:
            logger.error(f"❌ Неверный GROUP_CHAT_ID: {GROUP_CHAT_ID}")
            return
    else:
        logger.warning("⚠️ GROUP_CHAT_ID не задан, принимаем заявки из любой группы")

    user_data = get_user(user.id)

    # Уже верифицирован — сразу одобряем
    if user_data and user_data['status'] == 'verified':
        try:
            await join_request.approve()
            logger.info(f"✅ Авто-одобрение заявки для {user.id}")
        except Exception as e:
            logger.error(f"Ошибка авто-одобрения: {e}")
        return

    # === СОХРАНЯЕМ ПОЛЬЗОВАТЕЛЯ В БД ===
    context.user_data['group_chat_id'] = chat.id
    context.user_data['join_request_chat_id'] = chat.id

    if user_data:
        # Старый пользователь — обновляем группу
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET group_chat_id = ? WHERE user_id = ?", (chat.id, user.id))
        conn.commit()
        conn.close()
    else:
        # НОВЫЙ пользователь — создаём запись, чтобы /start продолжил верификацию
        save_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            group_chat_id=chat.id,
            status='pending'
        )
        logger.info(f"💾 Новый пользователь {user.id} сохранён в БД")

    # === ОТПРАВЛЯЕМ СООБЩЕНИЕ С КНОПКОЙ ===
    keyboard = [[InlineKeyboardButton("👉 Начать верификацию", callback_data="start_verify")]]
    
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"👋 Привет! Ты подал заявку на вступление в группу \"{chat.title}\".\n\n"
                f"Для доступа нужно пройти верификацию.\n\n"
                f"⚠️ Бот является посредником между тобой и администрацией. "
                f"Твои данные и видеосообщения не хранятся на сервере, а лишь пересылаются администраторам для проверки."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info(f"📨 Отправлено приветствие с кнопкой пользователю {user.id} для группы {chat.id}")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить сообщение пользователю {user.id}: {e}")
        logger.error(f"❌ traceback: {traceback.format_exc()}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — точка входа для пользователей"""
    user = update.effective_user
    user_data = get_user(user.id)

    # Уже верифицирован
    if user_data and user_data['status'] == 'verified':
        await update.message.reply_text("✅ Ты уже прошёл верификацию!")
        return ConversationHandler.END

    # Отклонён
    if user_data and user_data['status'] == 'rejected':
        keyboard = []
        if ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("📞 Связаться с администрацией", url=f"tg://user?id={ADMIN_IDS[0]}")])
        keyboard.append([InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"retry_{user.id}")])
        await update.message.reply_text(
            "❌ Твоя верификация была отклонена.\n\nЕсли считаешь, что ошибка — нажми кнопку ниже:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    # Уже в процессе — продолжаем
    if user_data and user_data['status'] == 'pending':
        if not user_data.get('birthdate'):
            await update.message.reply_text(
                "⏳ Ты уже начал верификацию. Давай продолжим!\n\n"
                "Шаг 1/2: Напиши свою дату рождения в формате ДД.ММ.ГГГГ\n"
                "Пример: 15.03.1995"
            )
            return STATE_BIRTHDATE
        
        elif not user_data.get('emoji'):
            emoji = generate_emoji()
            context.user_data['birthdate'] = user_data['birthdate']
            context.user_data['age'] = user_data['age']
            context.user_data['emoji'] = emoji
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET emoji = ? WHERE user_id = ?", (emoji, user.id))
            conn.commit()
            conn.close()
            
            await update.message.reply_text(
                f"⏳ Продолжаем верификацию!\n\n"
                f"📅 Возраст: {user_data['age']} лет\n\n"
                f"Шаг 2/2: Твой персональный смайлик: {emoji}\n\n"
                f"Запиши кружок, где показываешь этот смайлик руками или на листочке рядом с лицом.\n\n"
                f"⚠️ Отправь именно кружок (круглое видео), а не обычное видео!"
            )
            return STATE_VIDEO_NOTE
        
        else:
            context.user_data['birthdate'] = user_data['birthdate']
            context.user_data['age'] = user_data['age']
            context.user_data['emoji'] = user_data['emoji']
            
            emoji = user_data['emoji']
            await update.message.reply_text(
                f"⏳ Продолжаем верификацию!\n\n"
                f"📅 Возраст: {user_data['age']} лет\n"
                f"😀 Смайлик: {emoji}\n\n"
                f"⚠️ Остался последний шаг — отправь кружок (круглое видео), "
                f"где показываешь смайлик {emoji} руками или на листочке рядом с лицом.\n\n"
                f"Подсказка: зажми кнопку микрофона и свайпни вверх, или нажми скрепку → Видеосообщение."
            )
            return STATE_VIDEO_NOTE

    # === НОВЫЙ ПОЛЬЗОВАТЕЛЬ ИЛИ НЕТ ЗАЯВКИ ===
    # Если GROUP_CHAT_ID задан — предлагаем подать заявку
    # Иначе сразу начинаем верификацию (для тестов)
    if GROUP_CHAT_ID:
        await update.message.reply_text(
            "👋 Привет! Это бот для верификации перед вступлением в приватную группу.\n\n"
            "📋 Чтобы начать:\n"
            "1. Подай заявку на вступление в группу\n"
            "2. Бот автоматически напишет тебе для верификации\n\n"
            "⚠️ Если бот не написал после заявки — напиши мне снова /start"
        )
    else:
        # Для тестов без группы — сразу начинаем верификацию
        await update.message.reply_text(
            "👋 Привет! Начинаем верификацию.\n\n"
            "Шаг 1/2: Напиши свою дату рождения в формате ДД.ММ.ГГГГ\n"
            "Пример: 15.03.1995"
        )
        return STATE_BIRTHDATE
    
    return ConversationHandler.END

async def start_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопки 'Начать верификацию' из handle_join_request"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = get_user(user.id)
    
    # Уже верифицирован
    if user_data and user_data['status'] == 'verified':
        await query.edit_message_text("✅ Ты уже прошёл верификацию!")
        return ConversationHandler.END

    # Уже в процессе — продолжаем с нужного шага
    if user_data and user_data['status'] == 'pending':
        if not user_data.get('birthdate'):
            await query.edit_message_text(
                "⏳ Ты уже начал верификацию. Давай продолжим!\n\n"
                "Шаг 1/2: Напиши свою дату рождения в формате ДД.ММ.ГГГГ\n"
                "Пример: 15.03.1995"
            )
            return STATE_BIRTHDATE
        
        elif not user_data.get('emoji'):
            emoji = generate_emoji()
            context.user_data['birthdate'] = user_data['birthdate']
            context.user_data['age'] = user_data['age']
            context.user_data['emoji'] = emoji
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET emoji = ? WHERE user_id = ?", (emoji, user.id))
            conn.commit()
            conn.close()
            
            await query.edit_message_text(
                f"⏳ Продолжаем верификацию!\n\n"
                f"📅 Возраст: {user_data['age']} лет\n\n"
                f"Шаг 2/2: Твой персональный смайлик: {emoji}\n\n"
                f"Запиши кружок, где показываешь этот смайлик руками или на листочке рядом с лицом.\n\n"
                f"⚠️ Отправь именно кружок (круглое видео), а не обычное видео!"
            )
            return STATE_VIDEO_NOTE
        
        else:
            context.user_data['birthdate'] = user_data['birthdate']
            context.user_data['age'] = user_data['age']
            context.user_data['emoji'] = user_data['emoji']
            
            emoji = user_data['emoji']
            await query.edit_message_text(
                f"⏳ Продолжаем верификацию!\n\n"
                f"📅 Возраст: {user_data['age']} лет\n"
                f"😀 Смайлик: {emoji}\n\n"
                f"⚠️ Остался последний шаг — отправь кружок (круглое видео), "
                f"где показываешь смайлик {emoji} руками или на листочке рядом с лицом.\n\n"
                f"Подсказка: зажми кнопку микрофона и свайпни вверх, или нажми скрепку → Видеосообщение."
            )
            return STATE_VIDEO_NOTE

    # Новый пользователь (на всякий случай, если в БД нет)
    save_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        status='pending'
    )
    
    await query.edit_message_text(
        "👋 Начинаем верификацию!\n\n"
        "Шаг 1/2: Напиши свою дату рождения в формате ДД.ММ.ГГГГ\n"
        "Пример: 15.03.1995"
    )
    return STATE_BIRTHDATE

async def retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, user_id_str = query.data.split("_", 1)
    user_id = int(user_id_str)

    if query.from_user.id != user_id:
        await query.edit_message_text("⛔ Это действие недоступно.")
        return

    reset_user(user_id)
    await query.edit_message_text(
        "🔄 Статус сброшен. Подай заявку на вступление в группу снова — бот напишет тебе."
    )

async def get_birthdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    age = calculate_age(text)

    if age is None or age < 0 or age > 120:
        await update.message.reply_text("❌ Неверный формат. Введи дату в формате ДД.ММ.ГГГГ\nПример: 15.03.1995")
        return STATE_BIRTHDATE

    context.user_data['birthdate'] = text
    context.user_data['age'] = age

    emoji = generate_emoji()
    context.user_data['emoji'] = emoji

    # Сохраняем прогресс в БД сразу
    user = update.effective_user
    save_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        birthdate=text,
        age=age,
        emoji=emoji,
        status='pending'
    )

    await update.message.reply_text(
        f"📅 Возраст: {age} лет\n\n"
        f"Шаг 2/2: Твой персональный смайлик: {emoji}\n\n"
        f"Запиши кружок, где показываешь этот смайлик руками или на листочке рядом с лицом.\n\n"
        f"⚠️ Отправь именно кружок (круглое видео), а не обычное видео!"
    )
    return STATE_VIDEO_NOTE

async def get_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    group_chat_id = context.user_data.get('group_chat_id')
    if not group_chat_id:
        user_db = get_user(user.id)
        if user_db:
            group_chat_id = user_db.get('group_chat_id')

    try:
        save_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            group_chat_id=group_chat_id,
            birthdate=context.user_data.get('birthdate'),
            age=context.user_data.get('age'),
            emoji=context.user_data.get('emoji'),
            status='pending'
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения в БД: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Внутренняя ошибка. Попробуй снова.")
        return ConversationHandler.END

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
        f"😀 Смайлик: {context.user_data.get('emoji')}\n"
        f"👥 Группа: {group_chat_id or 'не указана'}\n\n"
        f"Проверь кружок и нажми решение:"
    )

    try:
        await send_to_moderation(context, user, moderation_text, update.message.message_id, admin_keyboard)
    except Exception as e:
        logger.error(f"Ошибка отправки на модерацию: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Не удалось отправить на проверку. Попробуй ещё раз.")
        return ConversationHandler.END

    await update.message.reply_text(
        "⏳ Кружок отправлен на проверку администратору.\nОбычно проверка занимает несколько минут."
    )
    return ConversationHandler.END

async def wrong_message_in_video_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji = context.user_data.get('emoji', '❓')
    await update.message.reply_text(
        f"❌ Это не кружок!\n\nОтправь кружок (круглое видео), где показываешь смайлик {emoji}.\n\n"
        f"Подсказка: зажми кнопку микрофона и свайпни вверх, или нажми скрепку → Видеосообщение."
    )
    return STATE_VIDEO_NOTE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Верификация отменена. Подай заявку снова.")
    return ConversationHandler.END

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.edit_message_text("⛔ У тебя нет прав.")
        return

    parts = query.data.split("_", 1)
    if len(parts) != 2:
        await query.edit_message_text("⚠️ Неверный формат данных.")
        return

    action, user_id_str = parts
    try:
        user_id = int(user_id_str)
    except ValueError:
        await query.edit_message_text("⚠️ Неверный ID пользователя.")
        return

    user_data = get_user(user_id)
    if not user_data:
        await query.edit_message_text("⚠️ Пользователь не найден в базе.")
        return

    group_chat_id = user_data.get('group_chat_id')

    if action == "approve":
        update_user_status(user_id, 'verified', update.effective_user.id)

        if group_chat_id:
            try:
                await context.bot.approve_chat_join_request(chat_id=group_chat_id, user_id=user_id)
                logger.info(f"✅ Заявка {user_id} одобрена в группе {group_chat_id}")
            except Exception as e:
                logger.error(f"Ошибка одобрения: {e}\n{traceback.format_exc()}")
                await query.edit_message_text(
                    f"✅ Верифицирован, но не удалось автоматически одобрить заявку. Одобри вручную."
                )
                return
        else:
            logger.warning(f"⚠️ group_chat_id не найден для пользователя {user_id}")

        try:
            await context.bot.send_message(chat_id=user_id, text="✅ Верификация пройдена! Добро пожаловать! 🎉")
        except Exception as e:
            logger.error(f"Не удалось уведомить {user_id}: {e}")

        await query.edit_message_text(f"✅ Пользователь {user_id} верифицирован и заявка одобрена.")

    elif action == "reject":
        update_user_status(user_id, 'rejected', update.effective_user.id, "Не указана")

        if group_chat_id:
            try:
                await context.bot.decline_chat_join_request(chat_id=group_chat_id, user_id=user_id)
                logger.info(f"❌ Заявка {user_id} отклонена в группе {group_chat_id}")
            except Exception as e:
                logger.error(f"Ошибка отклонения: {e}")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ Верификация отклонена.\n\n"
                    "Возможные причины:\n"
                    "— Несоответствие возраста\n"
                    "— Плохое качество кружка\n"
                    "— Смайлик не виден\n\n"
                    "Напиши /start чтобы попробовать снова."
                )
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить {user_id}: {e}")

        await query.edit_message_text(f"❌ Пользователь {user_id} отклонён.")

# ==================== КОМАНДЫ АДМИНИСТРАТОРА ====================

async def admin_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /info <user_id>")
        return
    try:
        user_id = int(context.args[0])
        user_data = get_user(user_id)
        if not user_data:
            await update.message.reply_text("Пользователь не найден.")
            return

        status_emoji = {'pending': '⏳', 'verified': '✅', 'rejected': '❌', 'new': '🆕'}
        text = (
            f"📊 Информация:\n\n"
            f"🆔 ID: {user_data['user_id']}\n"
            f"👤 Имя: {user_data['first_name']} {user_data['last_name'] or ''}\n"
            f"📛 Username: @{user_data['username'] or 'нет'}\n"
            f"📅 ДР: {user_data['birthdate']}\n"
            f"🔢 Возраст: {user_data['age']}\n"
            f"😀 Смайлик: {user_data['emoji']}\n"
            f"📌 Статус: {status_emoji.get(user_data['status'], '❓')} {user_data['status']}\n"
            f"👥 Группа: {user_data['group_chat_id'] or 'не указана'}\n"
            f"📅 Регистрация: {user_data['created_at']}\n"
        )
        if user_data['verification_date']:
            text += f"✅ Дата верификации: {user_data['verification_date']}\n"
        if user_data['rejection_reason']:
            text += f"❌ Причина: {user_data['rejection_reason']}\n"
        await update.message.reply_text(text)
    except ValueError:
        await update.message.reply_text("Неверный формат ID.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"📊 Статистика:\n✅ Верифицированы: {verified}\n❌ Отклонены: {rejected}\n⏳ Ожидают: {pending}\n📊 Всего: {total}"
    )

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, first_name, status, created_at FROM users ORDER BY created_at DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("База пуста.")
        return
    text = "📋 Последние 20:\n\n"
    for row in rows:
        status = {'pending': '⏳', 'verified': '✅', 'rejected': '❌', 'new': '🆕'}.get(row['status'], '❓')
        text += f"{status} {row['first_name']} (ID: {row['user_id']}) — {row['created_at'][:10]}\n"
    await update.message.reply_text(text)

async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /reset <user_id>")
        return
    try:
        user_id = int(context.args[0])
        user_data = get_user(user_id)
        if not user_data:
            await update.message.reply_text("Пользователь не найден.")
            return
        reset_user(user_id)
        await update.message.reply_text(f"✅ Статус пользователя {user_id} сброшен.")
    except ValueError:
        await update.message.reply_text("Неверный формат ID.")

async def admin_process_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная команда для обработки накопившихся заявок"""
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Запуск обработки накопившихся заявок...")
    await process_pending_users(context.application)
    await update.message.reply_text("✅ Обработка завершена!")

async def admin_check_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для проверки статуса бота в группе"""
    if not is_admin(update.effective_user.id):
        return
    await debug_check_bot_status(context.application)
    await update.message.reply_text("✅ Проверка группы выполнена, смотри логи!")

# ==================== ОБРАБОТЧИК ОШИБОК ====================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}\n{traceback.format_exc()}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Ошибка. Попробуй /start.")
        except Exception:
            pass

# ==================== ЗАПУСК ====================

def main():
    init_db()

    if not TOKEN:
        logger.error("❌ TOKEN не задан! Завершение.")
        sys.exit(1)

    if not GROUP_CHAT_ID:
        logger.warning("⚠️ GROUP_CHAT_ID не задан! Бот будет принимать заявки из любой группы.")

    if not ADMIN_IDS and not MODERATION_CHAT_ID:
        logger.error("❌ Не заданы ни ADMIN_IDS, ни MODERATION_CHAT_ID! Верификация не будет работать.")
        sys.exit(1)

    if WEBHOOK_SECRET == "change_me_please":
        logger.warning("⚠️ Используется дефолтный WEBHOOK_SECRET. Установи переменную окружения WEBHOOK_SECRET!")

    application = Application.builder().token(TOKEN).build()

    # ConversationHandler для верификации (только /start и шаги)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_verify_callback, pattern=r"^start_verify$")  # ← КНОПКА НАЧАТЬ ВЕРИФИКАЦИЮ
        ],
        states={
            STATE_BIRTHDATE: [
                MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, get_birthdate)
            ],
            STATE_VIDEO_NOTE: [
                MessageHandler(filters.VIDEO_NOTE & filters.ChatType.PRIVATE, get_video_note),
                MessageHandler(
                    filters.ALL & ~filters.VIDEO_NOTE & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    wrong_message_in_video_state
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,  # ← позволяет перезапустить /start в любой момент
        per_chat=False,
        per_user=True,
    )

    # ChatJoinRequestHandler ВНЕ ConversationHandler — отдельно!
    application.add_handler(ChatJoinRequestHandler(handle_join_request))

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(approve|reject)_\d+$"))
    application.add_handler(CallbackQueryHandler(retry_callback, pattern=r"^retry_\d+$"))
    application.add_handler(CommandHandler("info", admin_info))
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler("list", admin_list))
    application.add_handler(CommandHandler("reset", admin_reset))
    application.add_handler(CommandHandler("process_pending", admin_process_pending))
    application.add_handler(CommandHandler("check_group", admin_check_group))
    application.add_error_handler(error_handler)

    # Webhook — для Render (с health check)
    if RENDER_EXTERNAL_HOSTNAME:
        webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/webhook"

        async def post_init(app: Application):
            logger.info("🚀 Бот инициализирован, проверяем настройки...")
            await debug_check_bot_status(app)
            await process_pending_users(app)

        application.post_init = post_init

        async def health(request):
            """Health check для Render — отвечаем 200 OK на корневой запрос"""
            return web.Response(text="OK", status=200)

        async def telegram_webhook(request):
            """Обработка входящих webhook-запросов от Telegram"""
            # Проверяем secret token
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if token != WEBHOOK_SECRET:
                return web.Response(status=403)

            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return web.Response(status=200)

        async def run_server():
            # Инициализируем и стартуем приложение PTB
            await application.initialize()
            await application.start()

            # Устанавливаем webhook в Telegram
            await application.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False
            )

            # Создаём aiohttp сервер
            aio_app = web.Application()
            aio_app.router.add_get("/", health)           # ← Render health check
            aio_app.router.add_post("/webhook", telegram_webhook)

            runner = web.AppRunner(aio_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", PORT)
            await site.start()

            logger.info(f"🌐 Сервер запущен: {webhook_url}")
            logger.info(f"🌐 Health check: https://{RENDER_EXTERNAL_HOSTNAME}/")
            logger.info("✅ Бот работает. Ожидаем запросы...")

            # Держим процесс живым бесконечно
            await asyncio.Event().wait()

        asyncio.run(run_server())

    else:
        # Локальный запуск (для тестов)
        logger.info("🔄 Локальный запуск через polling...")

        async def post_init(app: Application):
            logger.info("🚀 Бот инициализирован, проверяем настройки...")
            await debug_check_bot_status(app)
            await process_pending_users(app)

        application.post_init = post_init

        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False
        )

if __name__ == "__main__":
    main()
