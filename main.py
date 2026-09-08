# -*- coding: utf-8 -*-
"""
LogoBot - Telegram orqali fayllarga (PDF, ZIP, CBZ, rasm va boshqa hujjatlarga) logotip qo'yish.
- Barcha fayllar QAT'IY KETMA-KETLIKDA BITTADAN (1-by-1) ishlanadi.
- /done olib tashlangan, fayllar cheksiz yuborilishi mumkin.
- Local Bot API orqali katta fayllarni yuborish imkoniyati qo'shildi.
"""

from __future__ import annotations
import os
import time
import uuid
import shutil
import sqlite3
import asyncio
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
import pymupdf as fitz  # PyMuPDF
from PIL import Image
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    InputMediaPhoto,
    InputMediaDocument,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    TelegramObject
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

logging.basicConfig(level=logging.WARNING)

# =====================================================================
# SOZLAMALAR VA PAPKALAR
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent

env_path = BASE_DIR / ".env"
if not env_path.exists():
    env_path = BASE_DIR.parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN") or "8684264908:AAE9FzHZH6LKG6hri8XJdsOvXMwqYlK0I_o"
PORT = int(os.getenv("PORT", "8000"))
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "") # e.g. "http://localhost:8081"
DB_PATH = BASE_DIR / "logobot.db"

SAVED_LOGOS_DIR = BASE_DIR / "saved_logos"
SAVED_LOGOS_DIR.mkdir(parents=True, exist_ok=True)

# Qat'iy ketma-ket (1-by-1) ishlash uchun Global Navbat (Queue)
task_queue = None

# =====================================================================
# SQLITE MA'LUMOTLAR BAZASI
# =====================================================================
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            saved_logo TEXT,
            files_count INTEGER DEFAULT 0,
            created_at TEXT,
            last_active TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            added_by INTEGER,
            created_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_name TEXT,
            file_type TEXT,
            processed_at TEXT
        )
    """)
    conn.commit()

    env_admins_str = os.getenv("ADMIN_IDS", "7052955513")
    parsed_env_admins = [int(x.strip()) for x in env_admins_str.split(",") if x.strip().isdigit()]
    initial_admins = list(set(parsed_env_admins + [7052955513]))

    cursor.execute("SELECT COUNT(*) as count FROM admins")
    row = cursor.fetchone()
    if row and row["count"] == 0:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for adm_id in initial_admins:
            cursor.execute("""
                INSERT OR IGNORE INTO admins (user_id, username, full_name, added_by, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (adm_id, "SuperAdmin", "Bosh Admin", 0, now_str))
        conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return True
    env_admins_str = os.getenv("ADMIN_IDS", "7052955513")
    if str(user_id) in [x.strip() for x in env_admins_str.split(",")]:
        return True
    return False

def get_admins() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM admins ORDER BY created_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_admin(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None, added_by: Optional[int] = None) -> bool:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT OR REPLACE INTO admins (user_id, username, full_name, added_by, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, full_name, added_by, now_str))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def remove_admin(user_id: int) -> bool:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def add_or_update_user(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("""
            UPDATE users SET username = ?, full_name = ?, last_active = ? WHERE user_id = ?
        """, (username, full_name, now_str, user_id))
    else:
        cursor.execute("""
            INSERT INTO users (user_id, username, full_name, created_at, last_active)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, full_name, now_str, now_str))
    conn.commit()
    conn.close()

def get_user_saved_logo(user_id: int) -> Optional[str]:
    logo_path = SAVED_LOGOS_DIR / f"logo_{user_id}.png"
    if logo_path.exists():
        return str(logo_path)
    return None

def set_user_saved_logo(user_id: int, source_path: str) -> str:
    target_path = SAVED_LOGOS_DIR / f"logo_{user_id}.png"
    shutil.copyfile(source_path, target_path)
    return str(target_path)

def clear_user_saved_logo(user_id: int) -> None:
    logo_path = SAVED_LOGOS_DIR / f"logo_{user_id}.png"
    if logo_path.exists():
        try:
            os.remove(logo_path)
        except Exception:
            pass

def log_processed_file(user_id: int, file_name: str, file_type: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO file_logs (user_id, file_name, file_type, processed_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, file_name, file_type, now_str))
    cursor.execute("""
        UPDATE users SET files_count = files_count + 1, last_active = ? WHERE user_id = ?
    """, (now_str, user_id))
    conn.commit()
    conn.close()


# =====================================================================
# AIOGRAM ROUTER VA MIDDLEWARE
# =====================================================================
router = Router()

class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)
        
        user_id = user.id
        add_or_update_user(user_id, user.username, user.full_name)

        if not is_admin(user_id):
            if isinstance(event, Message):
                await event.answer(
                    "⛔ <b>Kechirasiz, ushbu botdan faqat tasdiqlangan adminlar foydalana oladi.</b>\n"
                    f"Sizning Telegram ID: <code>{user_id}</code>",
                    parse_mode="HTML"
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Faqat tasdiqlangan adminlar uchun!", show_alert=True)
            return

        return await handler(event, data)

router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

class BotStates(StatesGroup):
    waiting_for_new_logo = State()
    waiting_for_permanent_logo = State()
    waiting_for_files = State()
    waiting_for_admin_id = State()


# --- KEYBOARDS ---
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 File logo qo'yish")],
            [KeyboardButton(text="🖼️ Doimiy logotip"), KeyboardButton(text="👥 Adminlar")]
        ],
        resize_keyboard=True
    )

def files_receiving_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔙 Bekor qilish (Asosiy menyuga qaytish)")]
        ],
        resize_keyboard=True
    )

def cancel_to_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔙 Asosiy menyu")]
        ],
        resize_keyboard=True
    )

def logo_choice_kb(has_saved_logo: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_saved_logo:
        buttons.append([InlineKeyboardButton(text="⭐ Saqlangan logoni qo'yish", callback_data="choice_saved_logo")])
    buttons.append([InlineKeyboardButton(text="📸 Yangi logo qo'yish", callback_data="choice_new_logo")])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def saved_logo_menu_kb(has_saved_logo: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_saved_logo:
        buttons.append([InlineKeyboardButton(text="🗑️ Saqlangan logoni o'chirish", callback_data="del_saved_logo")])
    buttons.append([InlineKeyboardButton(text="📸 Yangi doimiy logo yuklash", callback_data="upload_permanent_logo")])
    buttons.append([InlineKeyboardButton(text="❌ Yopish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="admin_add_prompt")],
        [InlineKeyboardButton(text="➖ Adminni o'chirish", callback_data="admin_remove_list")],
        [InlineKeyboardButton(text="🔙 Yopish", callback_data="cancel_action")]
    ])


# =====================================================================
# QAT'IY 1-BY-1 KETMA-KETLIK YUKLASH TIZIMI (WORKER)
# =====================================================================

async def edit_status(bot: Bot, chat_id: int, message_id: int, text: str):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
    except Exception:
        pass



ALBUMS_CACHE = {}

def extract_file_data(message: Message):
    if message.document:
        filename = message.document.file_name or f"file_{message.message_id}.bin"
        file_id = message.document.file_id
        file_ext = Path(filename).suffix.lower()
        return ("doc", filename, file_id, file_ext, message.message_id)
    elif message.photo:
        filename = f"photo_{message.message_id}.jpg"
        file_id = message.photo[-1].file_id
        return ("photo", filename, file_id, ".jpg", message.message_id)
    return None

async def process_album_after_delay(media_group_id, chat_id, active_logo, bot, status_msg):
    await asyncio.sleep(1.5)
    if media_group_id in ALBUMS_CACHE:
        messages = ALBUMS_CACHE.pop(media_group_id)
        messages.sort(key=lambda m: m.message_id)
        
        files_data = [extract_file_data(m) for m in messages if extract_file_data(m)]
        
        queue_size = task_queue.qsize()
        try:
            await bot.edit_message_text(f"⏳ <b>Albom navbatga qo'shildi</b> ({len(files_data)} ta fayl, oldinda {queue_size} ta vazifa bor)...", chat_id=chat_id, message_id=status_msg.message_id, parse_mode="HTML")
        except Exception:
            pass
            
        await task_queue.put({
            "type": "album",
            "chat_id": chat_id,
            "active_logo": active_logo,
            "files": files_data,
            "status_msg": status_msg
        })


async def queue_worker_loop(bot: Bot):
    while True:
        task = await task_queue.get()
        if task is None:
            break
            
        if isinstance(task, tuple):
            user_chat_id, user_msg_id, active_logo, file_name, status_msg, file_id = task
            task = {
                "type": "album",
                "chat_id": user_chat_id,
                "active_logo": active_logo,
                "files": [("doc", file_name, file_id, Path(file_name).suffix.lower(), user_msg_id)],
                "status_msg": status_msg
            }
            
        if isinstance(task, dict) and task.get("type") == "album":
            chat_id = task["chat_id"]
            active_logo = task["active_logo"]
            files = task["files"]
            status_msg = task["status_msg"]
            
            job_id = uuid.uuid4().hex[:8]
            temp_dir = Path(f"/tmp/job_{job_id}")
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            try:
                processed_media = []
                
                await edit_status(bot, chat_id, status_msg.message_id, f"📥 <b>Navbat keldi:</b> {len(files)} ta fayl yuklab olinmoqda va qayta ishlanmoqda...")
                
                for f_type, filename, file_id, file_ext, msg_id in files:
                    input_path = str(temp_dir / f"input_{msg_id}_{filename}")
                    tg_file = await bot.get_file(file_id)
                    await bot.download_file(tg_file.file_path, input_path)
                    
                    output_path = str(temp_dir / f"{msg_id}_{filename}")
                    thumb_path = str(temp_dir / f"thumb_{msg_id}.jpg")
                    has_thumb = False
                    
                    if active_logo and os.path.exists(active_logo):
                        make_telegram_thumbnail(active_logo, thumb_path)
                        has_thumb = True
                        
                    if file_ext == ".pdf":
                        process_pdf_file(input_path, output_path, active_logo)
                    elif file_ext in (".zip", ".cbz"):
                        process_archive_file(input_path, output_path, active_logo)
                    elif file_ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        process_image_file(input_path, output_path, active_logo)
                    else:
                        shutil.copyfile(input_path, output_path)
                        
                    processed_media.append((output_path, thumb_path if has_thumb else None, filename, f_type))
                    log_processed_file(chat_id, filename, file_ext)
                
                await edit_status(bot, chat_id, status_msg.message_id, "📤 Qayta ishlangan fayllar yuborilmoqda...")
                
                if len(processed_media) == 1:
                    out_p, th_p, fn, f_t = processed_media[0]
                    doc_input = FSInputFile(out_p, filename=fn)
                    thumb_input = FSInputFile(th_p) if th_p and os.path.exists(th_p) else None
                    if f_t == "photo":
                         await bot.send_photo(chat_id, photo=doc_input, caption=f"✅ <b>Tayyor:</b> <code>{fn}</code>", parse_mode="HTML")
                    else:
                         await bot.send_document(chat_id, document=doc_input, thumbnail=thumb_input, caption=f"✅ <b>Tayyor:</b> <code>{fn}</code>", parse_mode="HTML")
                else:
                    media_group = []
                    for i, (out_p, th_p, fn, f_t) in enumerate(processed_media):
                        fs_input = FSInputFile(out_p, filename=fn)
                        th_input = FSInputFile(th_p) if th_p and os.path.exists(th_p) else None
                        
                        caption = f"✅ <b>Tayyor:</b> <code>{fn}</code>" if i == 0 else ""
                        
                        if f_t == "photo":
                            media_group.append(InputMediaPhoto(media=fs_input, caption=caption, parse_mode="HTML"))
                        else:
                            media_group.append(InputMediaDocument(media=fs_input, thumbnail=th_input, caption=caption, parse_mode="HTML"))
                            
                        if len(media_group) == 10:
                            await bot.send_media_group(chat_id, media=media_group)
                            media_group = []
                            await asyncio.sleep(1)
                            
                    if media_group:
                        await bot.send_media_group(chat_id, media=media_group)
                        
                if status_msg:
                    try:
                        await bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
            except Exception as e:
                logging.exception(f"Error processing album: {e}")
                await edit_status(bot, chat_id, status_msg.message_id, f"❌ <b>Xatolik:</b> <code>{e}</code>")
            finally:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                task_queue.task_done()
                await asyncio.sleep(1)


# =====================================================================
# QAT'IY KETMA-KET NAVBATGA QO'SHISH (1-BY-1)
# =====================================================================
@router.message(BotStates.waiting_for_files, F.document | F.photo)
async def handle_incoming_files_in_queue(message: Message,
    InputMediaPhoto,
    InputMediaDocument, state: FSMContext, bot: Bot):
    data = await state.get_data()
    active_logo = data.get("active_logo")

    if not active_logo or not os.path.exists(active_logo):
        saved_logo = get_user_saved_logo(message.from_user.id)
        if saved_logo and os.path.exists(saved_logo):
            active_logo = saved_logo
            await state.update_data(active_logo=active_logo)
        else:
            await message.answer("⚠️ Logotip topilmadi! Iltimos, '📁 File logo qo'yish' tugmasini bosing.")
            return

    media_group_id = message.media_group_id
    if media_group_id:
        if media_group_id not in ALBUMS_CACHE:
            ALBUMS_CACHE[media_group_id] = []
            status_msg = await message.reply("⏳ <b>Albom</b> qabul qilinmoqda...")
            asyncio.create_task(process_album_after_delay(media_group_id, message.chat.id, active_logo, bot, status_msg))
        ALBUMS_CACHE[media_group_id].append(message)
    else:
        file_data = extract_file_data(message)
        if not file_data: return
        
        try:
            queue_size = task_queue.qsize()
            status_msg = await message.reply(f"⏳ Fayl navbatga qo'shildi (Oldinda {queue_size} ta vazifa bor)...")
            await task_queue.put({
                "type": "album",
                "chat_id": message.chat.id,
                "active_logo": active_logo,
                "files": [file_data],
                "status_msg": status_msg
            })
        except Exception:
            pass

@router.message()
async def handle_direct_photo_upload(message: Message,
    InputMediaPhoto,
    InputMediaDocument, state: FSMContext, bot: Bot):
    current_state = await state.get_state()
    if current_state in (BotStates.waiting_for_files, BotStates.waiting_for_new_logo, BotStates.waiting_for_permanent_logo, BotStates.waiting_for_admin_id):
        return
    if is_image_message(message):
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Doimiy logo sifatida saqlash", callback_data=f"save_as_perm:{file_id}")],
            [InlineKeyboardButton(text="📁 Joriy fayllarga logo qilish", callback_data=f"save_as_curr:{file_id}")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_action")]
        ])
        await message.reply(
            "🖼️ <b>Rasm qabul qilindi!</b>\n\nUshbu rasmni nima qilmoqchisiz?",
            parse_mode="HTML",
            reply_markup=kb
        )


# =====================================================================
# RENDER HEALTH CHECK SERVER
# =====================================================================
async def run_health_server():
    try:
        app = web.Application()
        async def health(request):
            return web.Response(text="Bot is running!")
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        print(f"Health check server listening on port {PORT}")
    except Exception as e:
        print(f"Health server error: {e}")


# =====================================================================
# POLLING STARTUP VA SHUTDOWN
# =====================================================================
async def on_startup(bot: Bot, dp: Dispatcher):
    global task_queue
    if task_queue is None:
        task_queue = asyncio.Queue()
    init_db()

    asyncio.create_task(run_health_server())
    asyncio.create_task(queue_worker_loop(bot))
    await bot.delete_webhook(drop_pending_updates=True)


async def on_shutdown(bot: Bot):
    await bot.session.close()


def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN topilmadi!")
        return

    api_url = LOCAL_API_URL
    # Agar Render-da ishlayotgan bo'lsa
    if os.getenv("RENDER"):
        if "localhost" in api_url or "127.0.0.1" in api_url:
            api_url = api_url.replace("localhost", "157.173.110.5").replace("127.0.0.1", "157.173.110.5")
        if not api_url:
            api_url = "http://157.173.110.5:8081"

    bot = None
    if api_url:
        is_local = os.getenv("IS_LOCAL", "").lower() in ("true", "1", "yes")
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(api_url, is_local=is_local),
            timeout=3600
        )
        bot = Bot(token=BOT_TOKEN, session=session)
        print(f"Local Bot API ishlatilmoqda: {api_url} (is_local={is_local})")
    else:
        session = AiohttpSession(timeout=3600)
        bot = Bot(token=BOT_TOKEN, session=session)
        print("Oddiy Telegram Bot API ishlatilmoqda (Max 50MB).")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    async def run_polling():
        nonlocal bot
        try:
            await on_startup(bot, dp)
            await dp.start_polling(bot)
        except Exception as e:
            if api_url:
                print(f"Local Bot API ulanishda xatolik: {e}. Standart Telegram API-ga o'tilmoqda...")
                try:
                    await bot.session.close()
                except Exception:
                    pass
                session = AiohttpSession(timeout=3600)
                bot = Bot(token=BOT_TOKEN, session=session)
                await on_startup(bot, dp)
                await dp.start_polling(bot)
            else:
                raise e
        finally:
            await on_shutdown(bot)

    asyncio.run(run_polling())


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
