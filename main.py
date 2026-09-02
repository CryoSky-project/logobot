# -*- coding: utf-8 -*-
"""
LogoBot - Telegram orqali fayllarga (PDF, ZIP, CBZ, rasm va boshqa hujjatlarga)
logotip / muqova qo'yish va Telegram prevyusi (thumbnail) o'rnatish boti.

- Barcha logika bitta faylda (main.py)
- 2 GB (2000 MB) GACHA FAYLLARNI QO'LLAB-QUVVATLASH:
  SESSION_STRING (Telethon User Session) orqali 2 GB-gacha bo'lgan har qanday katta fayllarni yuklaydi va yuboradi!
- PARALLEL QAYTA ISHLASH + TARTIBLI YUBORISH (Fast Parallel Processing & Ordered Delivery):
  Barcha kelgan fayllar (10+) bir vaqtda darhol parallel yuklab olinib ishlanadi (maksimal tezlik),
  lekin Telegram'ga yuborishda xabarlarning asl kelish tartibi (message_id) bo'yicha qat'iy navbat bilan chiqariladi!
- To'liq avtomatik Render.com Webhook (RENDER_EXTERNAL_URL orqali)
- Barcha loglar to'liq o'chirilgan (Silent mode)
- SQLite ma'lumotlar bazasi va Adminlar tizimi (Faqat adminlar uchun)
- Doimiy logotiplar faqat fayl sifatida saqlanadi (saved_logos/ papkasida)
- /done buyrug'i yuborilmaguncha cheksiz fayllarni qabul qiladi
- Yuklab olingan va qayta ishlangan fayllar Telegram'ga yuborilishi bilan darhol o'chiriladi
- Render.com-da 24/7 uxlab qolmaslik uchun har 5 daqiqada avtomatik Self-Ping
"""

from __future__ import annotations
import os
import io
import sys
import time
import uuid
import shutil
import sqlite3
import asyncio
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, Tuple, List, Dict, Any

from dotenv import load_dotenv
import fitz  # PyMuPDF
from PIL import Image
import aiohttp
from aiohttp import web

from telethon import TelegramClient
from telethon.sessions import StringSession

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    TelegramObject
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# =====================================================================
# BARCHA LOGLARNI TO'LIQ O'CHIRISH (SILENT MODE)
# =====================================================================
logging.disable(logging.CRITICAL)
for logger_name in (
    "aiogram", "aiohttp", "aiohttp.access", "aiohttp.server",
    "aiohttp.web", "asyncio", "fitz", "PIL", "telethon"
):
    l = logging.getLogger(logger_name)
    l.setLevel(logging.CRITICAL + 10)
    l.propagate = False
    l.handlers = [logging.NullHandler()]


# =====================================================================
# SOZLAMALAR VA PAPKALAR
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent

# .env yuklash
env_path = BASE_DIR / ".env"
if not env_path.exists():
    env_path = BASE_DIR.parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN") or "8684264908:AAE9FzHZH6LKG6hri8XJdsOvXMwqYlK0I_o"
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_PATH = "/webhook"
DB_PATH = BASE_DIR / "logobot.db"

API_ID = int(os.getenv("API_ID", "39181210"))
API_HASH = os.getenv("API_HASH", "fb40947940c94722c5f0b7560df570fd")
SESSION_STRING = (
    os.getenv("SESSION_STRING")
    or os.getenv("STRING_SESSION")
    or "1ApWapzMBuwttkl3-A-RLgaoU1FpcJYDOvM59_0yuFKhNZ7G_523L7MHamFrxBPlVBGfY_-mfc4VC4niGEEsCsvZCn9nBtR-30CgZ42QkDaoKpVkit0UTUZBXzYvWYeM8KzNIDFIqJk2sDpKDoJ1X760M0Q1Lr_iUQilWCGQnhfrBKAgeBTCP9uYTXIa--GklDQSHWKBHHhP1oHHimzqt-S8qXgUjsyaqK6qSCJrhiM19t_wLetbM77szqA3VLj_8AQF5Y4hIyrW7kFqXXe7JMJrbP9-VBbMK9P8MqKa04twmQBjm5anUJ4dGtKumEqSianpPglrDDiqQvICqpO6SWIr181VpWyY="
)

SAVED_LOGOS_DIR = BASE_DIR / "saved_logos"
SAVED_LOGOS_DIR.mkdir(parents=True, exist_ok=True)

# Render 512MB RAM xavfsizligi va tezligi uchun bir vaqtning o'zida ko'pi bilan 3 ta fayl parallel ishlanadi
PROCESSING_SEMAPHORE = asyncio.Semaphore(3)

# Telethon mijoz obyekti
telethon_client: Optional[TelegramClient] = None


# =====================================================================
# SQLITE MA'LUMOTLAR BAZASI
# =====================================================================

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Ma'lumotlar bazasini yaratadi va dastlabki adminni kiritadi."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Foydalanuvchilar
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
    
    # Adminlar
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            added_by INTEGER,
            created_at TEXT
        )
    """)
    
    # Fayllar statistikasi
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

    # Standart boshlang'ich adminlar
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
    """Foydalanuvchining adminligini tekshiradi."""
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
    """Foydalanuvchining faylda saqlangan doimiy logosini qaytaradi."""
    logo_path = SAVED_LOGOS_DIR / f"logo_{user_id}.png"
    if logo_path.exists():
        return str(logo_path)
    return None

def set_user_saved_logo(user_id: int, source_path: str) -> str:
    """Logotipni fayl tizimida saqlaydi."""
    target_path = SAVED_LOGOS_DIR / f"logo_{user_id}.png"
    shutil.copyfile(source_path, target_path)
    return str(target_path)

def clear_user_saved_logo(user_id: int) -> None:
    """Foydalanuvchining doimiy logosini fayldan o'chiradi."""
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


# --- FSM STATES ---
class BotStates(StatesGroup):
    waiting_for_new_logo = State()       # Yangi logo yuklash
    waiting_for_permanent_logo = State() # Doimiy logo yuklash
    waiting_for_files = State()          # Fayllarni uzluksiz qabul qilish (/done gacha)
    waiting_for_admin_id = State()       # Yangi admin ID kiritish


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
            [KeyboardButton(text="✅ Tugatish (/done)")],
            [KeyboardButton(text="🔙 Asosiy menyu")]
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
# FAYLLARNI QAYTA ISHLASH (PDF, ZIP, CBZ, RASMLAR)
# =====================================================================

def make_telegram_thumbnail(image_path: str, thumb_path: str) -> str:
    """Telegram uchun 320x320 JPEG thumbnail tayyorlaydi."""
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            img.thumbnail((320, 320), Image.Resampling.LANCZOS)
            img.save(thumb_path, "JPEG", quality=85, optimize=True)
    except Exception:
        blank = Image.new("RGB", (320, 320), color=(40, 40, 40))
        blank.save(thumb_path, "JPEG")
    return thumb_path


def process_pdf_file(input_path: str, output_path: str, cover_path: Optional[str] = None) -> str:
    """PDF fayldagi 1-chi va oxirgi sahifani o'chirib, o'rniga yangi logotipni joylaydi."""
    if not cover_path or not os.path.exists(cover_path):
        shutil.copyfile(input_path, output_path)
        return output_path

    doc = fitz.open(input_path)
    total_pages = len(doc)
    pw, ph = 595.0, 842.0
    if total_pages > 0:
        pw, ph = doc[0].rect.width, doc[0].rect.height

    if total_pages >= 2:
        doc.delete_page(total_pages - 1)
        doc.delete_page(0)
        front_page = doc.new_page(0, width=pw, height=ph)
        front_page.insert_image(front_page.rect, filename=cover_path, keep_proportion=True)
        back_page = doc.new_page(len(doc), width=pw, height=ph)
        back_page.insert_image(back_page.rect, filename=cover_path, keep_proportion=True)
    elif total_pages == 1:
        doc.delete_page(0)
        front_page = doc.new_page(0, width=pw, height=ph)
        front_page.insert_image(front_page.rect, filename=cover_path, keep_proportion=True)
    else:
        front_page = doc.new_page(0, width=pw, height=ph)
        front_page.insert_image(front_page.rect, filename=cover_path, keep_proportion=True)

    doc.save(output_path, garbage=3, deflate=True)
    doc.close()
    return output_path


def process_archive_file(input_path: str, output_path: str, cover_path: Optional[str] = None) -> str:
    """ZIP/CBZ arxivlaridagi 1-chi va oxirgi rasmlarni yangi logotipga almashtiradi."""
    if not cover_path or not os.path.exists(cover_path):
        shutil.copyfile(input_path, output_path)
        return output_path

    with open(cover_path, "rb") as f:
        cover_bytes = f.read()

    image_exts = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')

    with zipfile.ZipFile(input_path, "r") as z_in:
        file_list = [f for f in z_in.namelist() if not f.endswith('/')]
        image_files = sorted([f for f in file_list if f.lower().endswith(image_exts)])

        first_img = image_files[0] if image_files else None
        last_img = image_files[-1] if len(image_files) > 1 else None

        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.infolist():
                if item.filename == first_img or item.filename == last_img:
                    z_out.writestr(item.filename, cover_bytes)
                else:
                    z_out.writestr(item, z_in.read(item.filename))

    return output_path


def process_image_file(input_path: str, output_path: str, cover_path: Optional[str] = None) -> str:
    """Rasmni yangi logotip rasmiga almashtiradi."""
    if cover_path and os.path.exists(cover_path):
        shutil.copyfile(cover_path, output_path)
    else:
        shutil.copyfile(input_path, output_path)
    return output_path


# =====================================================================
# TEZKOR PARALLEL ISHLOV BERISH VA QAT'IY KETMA-KET YUBORISH (ORDERED DELIVERY)
# =====================================================================

class ChatOrderedDelivery:
    """
    Barcha parallel ishlangan fayllarni Telegram'ga qat'iy ravishda
    kelgan xabarlar tartibi bo'yicha ketma-ket yuboruvchi navbat menejeri.
    """
    def __init__(self, chat_id: int, bot: Bot):
        self.chat_id = chat_id
        self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None

    def ensure_started(self):
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self):
        while True:
            item = await self.queue.get()
            if item is None:
                break

            future, msg_id, orig_filename, status_msg = item
            try:
                res = await future

                if res and res.get("success"):
                    output_file_path = res["output_path"]
                    thumb_path = res["thumb_path"]
                    has_thumb = res["has_thumb"]
                    temp_dir = res["temp_dir"]

                    caption = f"✅ <b>Tayyor:</b> <code>{orig_filename}</code>"

                    # 1. Agar Telethon sessiyasi ulangan bo'lsa (2 GB-gacha yuborish)
                    sent_successfully = False
                    if telethon_client and telethon_client.is_connected():
                        try:
                            thumb_to_send = thumb_path if has_thumb and os.path.exists(thumb_path) else None
                            await telethon_client.send_file(
                                self.chat_id,
                                file=output_file_path,
                                caption=caption,
                                parse_mode="html",
                                thumb=thumb_to_send,
                                reply_to=msg_id
                            )
                            sent_successfully = True
                        except Exception:
                            sent_successfully = False

                    # 2. Agar Telethon orqali ketmasa yoki ulanmagan bo'lsa -> Standart Bot API
                    if not sent_successfully:
                        doc_input = FSInputFile(output_file_path, filename=orig_filename)
                        thumb_input = FSInputFile(thumb_path) if has_thumb and os.path.exists(thumb_path) else None
                        await self.bot.send_document(
                            chat_id=self.chat_id,
                            document=doc_input,
                            thumbnail=thumb_input,
                            caption=caption,
                            parse_mode="HTML",
                            reply_to_message_id=msg_id
                        )

                    log_processed_file(self.chat_id, orig_filename, Path(orig_filename).suffix.lower())

                    # Telegram'ga ketishi bilan darhol o'chirish
                    if temp_dir and os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir, ignore_errors=True)

                    if status_msg:
                        try:
                            await self.bot.delete_message(chat_id=self.chat_id, message_id=status_msg.message_id)
                        except Exception:
                            pass

                elif res and not res.get("success"):
                    err = res.get("error", "Noma'lum xatolik")
                    temp_dir = res.get("temp_dir")
                    if temp_dir and os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir, ignore_errors=True)

                    if status_msg:
                        try:
                            await self.bot.edit_message_text(
                                chat_id=self.chat_id,
                                message_id=status_msg.message_id,
                                text=f"❌ <b>Xatolik ({orig_filename}):</b> <code>{err}</code>",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

            except Exception:
                pass
            finally:
                self.queue.task_done()


chat_deliveries: Dict[int, ChatOrderedDelivery] = {}

def get_or_create_delivery(chat_id: int, bot: Bot) -> ChatOrderedDelivery:
    if chat_id not in chat_deliveries:
        chat_deliveries[chat_id] = ChatOrderedDelivery(chat_id, bot)
    return chat_deliveries[chat_id]


async def parallel_process_worker(
    bot: Bot,
    chat_id: int,
    file_id: str,
    orig_filename: str,
    active_logo: str,
    original_message_id: int,
    future: asyncio.Future
):
    """Faylni darhol parallel yuklab olib, muqovasini almashtiradi (2GB-gacha qo'llab-quvvatlaydi)."""
    job_id = f"job_{chat_id}_{original_message_id}_{uuid.uuid4().hex[:6]}"
    temp_dir = Path(f"/tmp/{job_id}")
    temp_dir.mkdir(parents=True, exist_ok=True)

    async with PROCESSING_SEMAPHORE:
        try:
            input_file_path = str(temp_dir / orig_filename)

            # 1. Faylni yuklab olish (Telethon orqali 2 GB-gacha, yoki Bot API)
            downloaded = False
            if telethon_client and telethon_client.is_connected():
                try:
                    # Telethon orqali xabarni topib to'g'ridan-to'g'ri MTProto orqali yuklab olamiz
                    tg_msg = await telethon_client.get_messages(chat_id, ids=original_message_id)
                    if tg_msg and tg_msg.media:
                        await telethon_client.download_media(tg_msg.media, file=input_file_path)
                        downloaded = True
                except Exception:
                    downloaded = False

            if not downloaded:
                tg_file = await bot.get_file(file_id)
                await bot.download_file(tg_file.file_path, input_file_path)

            file_ext = Path(orig_filename).suffix.lower()
            output_filename = f"edited_{orig_filename}"
            output_file_path = str(temp_dir / output_filename)
            thumb_path = str(temp_dir / "thumb.jpg")

            has_thumb = False
            if active_logo and os.path.exists(active_logo):
                make_telegram_thumbnail(active_logo, thumb_path)
                has_thumb = True

            if file_ext == ".pdf":
                process_pdf_file(input_file_path, output_file_path, active_logo)
            elif file_ext in (".zip", ".cbz"):
                process_archive_file(input_file_path, output_file_path, active_logo)
            elif file_ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                process_image_file(input_file_path, output_file_path, active_logo)
            else:
                shutil.copyfile(input_file_path, output_file_path)

            if not future.done():
                future.set_result({
                    "success": True,
                    "output_path": output_file_path,
                    "thumb_path": thumb_path,
                    "has_thumb": has_thumb,
                    "temp_dir": str(temp_dir)
                })
        except Exception as e:
            if not future.done():
                future.set_result({
                    "success": False,
                    "error": str(e),
                    "temp_dir": str(temp_dir)
                })


# =====================================================================
# ASOSIY MENYU VA START HANDLERS
# =====================================================================

@router.message(CommandStart())
@router.message(F.text == "🔙 Asosiy menyu")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    saved_logo = get_user_saved_logo(user.id)
    logo_status = "✅ O'rnatilgan" if saved_logo else "❌ O'rnatilmagan"
    limit_status = "🚀 2000 MB (2 GB)" if telethon_client else "📦 50 MB"

    text = (
        f"👋 <b>Assalomu alaykum, {user.first_name}!</b>\n\n"
        f"🤖 <b>LogoBot boshqaruv paneliga xush kelibsiz.</b>\n\n"
        f"📌 <b>Holat:</b>\n"
        f"• Sizning ID: <code>{user.id}</code> (Admin)\n"
        f"• Doimiy logotip: <b>{logo_status}</b>\n"
        f"• Maksimal fayl hajmi: <b>{limit_status}</b>\n\n"
        f"👇 <i>Fayllarga logotip qo'yish uchun quyidagi tugmani bosing:</i>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())


@router.message(Command("done"))
@router.message(F.text == "✅ Tugatish (/done)")
async def cmd_done_processing(message: Message, state: FSMContext):
    """Fayllar yuborish bosqichini tugatib, asosiy menyuga qaytaradi."""
    await state.clear()
    await message.answer(
        "✅ <b>Fayllarni qabul qilish yakunlandi!</b>\nBarcha fayllar navbat bilan o'z tartibida yetkaziladi.",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


# =====================================================================
# FILE LOGO QO'YISH BOSQICHI
# =====================================================================

@router.message(F.text == "📁 File logo qo'yish")
async def cmd_file_logo_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    saved_logo = get_user_saved_logo(user_id)
    has_saved = bool(saved_logo and os.path.exists(saved_logo))

    text = (
        "🖌️ <b>Fayllarga qaysi logotipni qo'ymoqchisiz?</b>\n\n"
        "Tanlang:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=logo_choice_kb(has_saved))


@router.callback_query(F.data == "choice_saved_logo")
async def cb_choice_saved_logo(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    saved_logo = get_user_saved_logo(user_id)

    if not saved_logo or not os.path.exists(saved_logo):
        await callback.answer("⚠️ Saqlangan logotip topilmadi!", show_alert=True)
        return

    await state.update_data(active_logo=saved_logo)
    await state.set_state(BotStates.waiting_for_files)

    try:
        await callback.message.delete()
    except Exception:
        pass

    text = (
        "✅ <b>Saqlangan logotip tanlandi!</b>\n\n"
        "📤 <b>Endi fayllarni yuboring:</b>\n"
        "<i>(Bir vaqtning o'zida 1 ta yoki 10+ ta 2 GB-gacha bo'lgan PDF, ZIP, CBZ, rasm va boshqa fayllarni tashlashingiz mumkin)</i>\n\n"
        "⚡ Bot barcha fayllarni bir vaqtda parallel yuklab olib, o'z navbati bilan tartib bilan qaytarib beradi.\n"
        "Tugatgach pastdagi <b>«✅ Tugatish (/done)»</b> tugmasini bosing."
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=files_receiving_kb())
    await callback.answer()


@router.callback_query(F.data == "choice_new_logo")
async def cb_choice_new_logo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_new_logo)
    try:
        await callback.message.delete()
    except Exception:
        pass

    text = (
        "📸 <b>Fayllarga qo'yiladigan yangi LOGOTIP rasmini (PNG / JPG) yuboring:</b>"
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cancel_to_main_kb())
    await callback.answer()


@router.message(BotStates.waiting_for_new_logo, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_new_logo_uploaded(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    timestamp = int(time.time() * 1000)
    temp_logo_path = SAVED_LOGOS_DIR / f"temp_{user_id}_{timestamp}.png"

    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id

    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, temp_logo_path)

    set_user_saved_logo(user_id, str(temp_logo_path))

    await state.update_data(active_logo=str(temp_logo_path))
    await state.set_state(BotStates.waiting_for_files)

    text = (
        "✅ <b>Yangi logotip qabul qilindi va saqlandi!</b>\n\n"
        "📤 <b>Endi fayllarni yuboring:</b>\n"
        "<i>(Bir vaqtning o'zida 1 ta yoki 10+ ta 2 GB-gacha bo'lgan PDF, ZIP, CBZ, rasm va boshqa fayllarni tashlashingiz mumkin)</i>\n\n"
        "⚡ Bot barcha fayllarni bir vaqtda parallel yuklab olib, o'z navbati bilan tartib bilan qaytarib beradi.\n"
        "Tugatgach pastdagi <b>«✅ Tugatish (/done)»</b> tugmasini bosing."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=files_receiving_kb())


# =====================================================================
# DOIMIY LOGOTIPNI BOSHQARISH
# =====================================================================

@router.message(F.text == "🖼️ Doimiy logotip")
async def cmd_saved_logo_menu(message: Message):
    user_id = message.from_user.id
    saved_logo = get_user_saved_logo(user_id)

    if saved_logo and os.path.exists(saved_logo):
        photo = FSInputFile(saved_logo)
        await message.answer_photo(
            photo=photo,
            caption=(
                "✅ <b>Sizning saqlangan doimiy logotipingiz.</b>\n\n"
                "Faylga logo qo'yishda har safar rasm yubormasdan, shu logotipdan foydalanishingiz mumkin."
            ),
            parse_mode="HTML",
            reply_markup=saved_logo_menu_kb(has_saved_logo=True)
        )
    else:
        await message.answer(
            "ℹ️ <b>Sizda hali doimiy logotip saqlanmagan.</b>\n"
            "Logotip saqlash uchun quyidagi tugmani bosing:",
            parse_mode="HTML",
            reply_markup=saved_logo_menu_kb(has_saved_logo=False)
        )


@router.callback_query(F.data == "upload_permanent_logo")
async def cb_upload_permanent_logo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_permanent_logo)
    await callback.message.answer(
        "📸 <b>Doimiy logotip sifatida saqlamoqchi bo'lgan RASMNI yuboring:</b>",
        parse_mode="HTML",
        reply_markup=cancel_to_main_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "del_saved_logo")
async def cb_del_saved_logo(callback: CallbackQuery):
    user_id = callback.from_user.id
    clear_user_saved_logo(user_id)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption="🗑️ <b>Doimiy logotip o'chirildi!</b>", parse_mode="HTML")
        else:
            await callback.message.edit_text("🗑️ <b>Doimiy logotip o'chirildi!</b>", parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("O'chirildi")


@router.message(BotStates.waiting_for_permanent_logo, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_save_permanent_logo(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    temp_path = SAVED_LOGOS_DIR / f"temp_upload_{user_id}.png"

    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id

    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, temp_path)

    set_user_saved_logo(user_id, str(temp_path))
    if temp_path.exists():
        os.remove(temp_path)

    await state.clear()
    await message.answer(
        "✅ <b>Doimiy logotip muvaffaqiyatli saqlandi!</b>",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


# =====================================================================
# ADMINLARNI BOSHQARISH
# =====================================================================

@router.message(F.text == "👥 Adminlar")
async def cmd_admins_menu(message: Message):
    admins = get_admins()
    admin_list_text = ""
    for idx, adm in enumerate(admins, 1):
        uname = f"@{adm['username']}" if adm.get('username') else "username yo'q"
        name = adm.get('full_name') or "Admin"
        admin_list_text += f"{idx}. <b>{name}</b> — <code>{adm['user_id']}</code> ({uname})\n"

    text = (
        f"👥 <b>Barcha adminlar ro'yxati ({len(admins)} ta):</b>\n\n"
        f"{admin_list_text}\n"
        f"👇 <i>Admin qo'shish yoki o'chirish uchun quyidagi tugmalardan foydalaning:</i>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_panel_kb())


@router.callback_query(F.data == "admin_add_prompt")
async def cb_admin_add_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_admin_id)
    text = (
        "✍️ <b>Qo'shmoqchi bo'lgan yangi adminning Telegram ID raqamini yuboring:</b>\n\n"
        "<i>Masalan: <code>1234567890</code></i>"
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cancel_to_main_kb())
    await callback.answer()


@router.message(BotStates.waiting_for_admin_id)
async def handle_admin_id_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqamlardan iborat to'g'ri Telegram ID yuboring!")
        return

    new_admin_id = int(text)
    adder_id = message.from_user.id
    success = add_admin(user_id=new_admin_id, username=None, full_name="Yangi Admin", added_by=adder_id)
    await state.clear()

    if success:
        await message.answer(
            f"✅ <b>Yangi admin qo'shildi:</b> <code>{new_admin_id}</code>",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )
    else:
        await message.answer("❌ Adminni qo'shishda xatolik yuz berdi.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "admin_remove_list")
async def cb_admin_remove_list(callback: CallbackQuery):
    admins = get_admins()
    buttons = []
    for adm in admins:
        uid = adm["user_id"]
        buttons.append([InlineKeyboardButton(text=f"❌ O'chirish: {uid}", callback_data=f"del_adm:{uid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="cancel_action")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("➖ <b>O'chirmoqchi bo'lgan adminni tanlang:</b>", parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("del_adm:"))
async def cb_delete_admin(callback: CallbackQuery):
    target_id = int(callback.data.split(":")[1])

    admins = get_admins()
    if len(admins) <= 1:
        await callback.answer("⚠️ Botda kamida 1 ta admin qolishi shart!", show_alert=True)
        return

    remove_admin(target_id)
    await callback.answer("Admin o'chirildi")
    await callback.message.edit_text(f"🗑️ <b>Admin o'chirildi:</b> <code>{target_id}</code>", parse_mode="HTML")


@router.callback_query(F.data == "cancel_action")
async def cb_cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("🔙 Asosiy menyu", reply_markup=main_menu_kb())
    await callback.answer()


# =====================================================================
# BIR VAQTNING O'ZIDA KELGAN FAYLLARNI DARHOL PARALLEL ISHGA TUSHIRISH
# =====================================================================

@router.message(BotStates.waiting_for_files, F.document)
async def handle_incoming_documents_in_queue(message: Message, state: FSMContext, bot: Bot):
    """Kelgan har bir hujjatni darhol parallel ishlovga qo'yadi va yuborish navbatiga yozadi."""
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

    doc = message.document
    filename = doc.file_name or f"file_{message.message_id}.bin"
    file_size_mb = doc.file_size / (1024 * 1024) if doc.file_size else 0

    # Telethon bo'lsa 2000 MB gacha, bo'lmasa 50 MB
    max_size = 2000 if telethon_client else 50
    if file_size_mb > max_size:
        await message.reply(f"⚠️ Fayl hajmi {max_size} MB dan katta. Kichikroq fayl yuboring.")
        return

    status_msg = None
    try:
        status_msg = await message.reply(f"⏳ <b>'{filename}'</b> qabul qilindi, ishlanmoqda...")
    except Exception:
        pass

    # 1. Tartibli yetkazib berish (Ordered Delivery) navbatiga qo'shamiz
    delivery = get_or_create_delivery(message.chat.id, bot)
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    await delivery.queue.put((future, message.message_id, filename, status_msg))
    delivery.ensure_started()

    # 2. Hech kutmasdan DARHOL parallel tarzda yuklab olish va qayta ishlashni boshlaymiz!
    asyncio.create_task(
        parallel_process_worker(
            bot=bot,
            chat_id=message.chat.id,
            file_id=doc.file_id,
            orig_filename=filename,
            active_logo=active_logo,
            original_message_id=message.message_id,
            future=future
        )
    )


@router.message(BotStates.waiting_for_files, F.photo)
async def handle_incoming_photos_in_queue(message: Message, state: FSMContext, bot: Bot):
    """Kelgan har bir rasmni darhol parallel ishlovga qo'yadi va yuborish navbatiga yozadi."""
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

    file_id = message.photo[-1].file_id
    filename = f"photo_{message.message_id}.jpg"

    status_msg = None
    try:
        status_msg = await message.reply(f"⏳ <b>'{filename}'</b> qabul qilindi, ishlanmoqda...")
    except Exception:
        pass

    # 1. Tartibli yetkazib berish navbatiga qo'shish
    delivery = get_or_create_delivery(message.chat.id, bot)
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    await delivery.queue.put((future, message.message_id, filename, status_msg))
    delivery.ensure_started()

    # 2. DARHOL parallel tarzda ishga tushirish!
    asyncio.create_task(
        parallel_process_worker(
            bot=bot,
            chat_id=message.chat.id,
            file_id=file_id,
            orig_filename=filename,
            active_logo=active_logo,
            original_message_id=message.message_id,
            future=future
        )
    )


# =====================================================================
# RENDER.COM HEALTH CHECK HTTP SERVER VA AUTO SELF-PING
# =====================================================================

async def health_check(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "LogoBot",
        "alive": True
    })

async def auto_self_ping(base_url: str):
    """
    Render.com-da bot uxlamasligi uchun har 5 daqiqada (300 soniya)
    o'z-o'ziga HTTP GET so'rovini yuborib turadi (Avtomatik 24/7 Self-Ping).
    """
    await asyncio.sleep(20)
    target_url = f"{base_url.rstrip('/')}/health"

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    pass
        except Exception:
            pass

        await asyncio.sleep(300)


# =====================================================================
# WEBHOOK STARTUP VA SHUTDOWN
# =====================================================================

async def on_startup(bot: Bot, base_url: Optional[str], dp: Dispatcher):
    global telethon_client
    init_db()

    # Telethon User Client (2 GB fayllarni yuklash/yuborish uchun)
    if SESSION_STRING and API_ID and API_HASH:
        try:
            telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
            await telethon_client.start()
        except Exception:
            telethon_client = None

    if base_url:
        webhook_url = f"{base_url.rstrip('/')}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types()
        )
        asyncio.create_task(auto_self_ping(base_url))
    else:
        await bot.delete_webhook(drop_pending_updates=True)


async def on_shutdown(bot: Bot):
    global telethon_client
    for d in chat_deliveries.values():
        if d.worker_task and not d.worker_task.done():
            d.worker_task.cancel()
    if telethon_client and telethon_client.is_connected():
        await telethon_client.disconnect()
    await bot.session.close()


# =====================================================================
# MAIN
# =====================================================================

def main():
    if not BOT_TOKEN:
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Render URL aniqlash
    base_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("APP_URL")
        or os.getenv("SELF_PING_URL")
    )
    if not base_url and os.getenv("RENDER_EXTERNAL_HOSTNAME"):
        base_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"

    # Aiohttp ilovasi
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    # Webhook handler
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    # Startup & Shutdown ro'yxatdan o'tkazish
    app.on_startup.append(lambda a: on_startup(bot, base_url, dp))
    app.on_shutdown.append(lambda a: on_shutdown(bot))

    # Aiohttp serverini ishga tushirish (barcha loglar o'chirilgan)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=lambda *args: None)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
