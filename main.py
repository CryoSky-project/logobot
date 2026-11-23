# -*- coding: utf-8 -*-
"""
LogoBot - Telegram orqali fayllarga (PDF, ZIP, CBZ, rasm va boshqa hujjatlarga) logotip qo'yish.
- Barcha fayllar QAT'IY KETMA-KETLIKDA BITTADAN (1-by-1) ishlanadi.
- Albomlar (media group) va yakkalik fayllar to'liq qo'llab-quvvatlanadi.
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
import html
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

BOT_TOKEN = os.getenv("BOT_TOKEN") or "8964013713:AAG8V-PHzHwl4T1n4-1W0VCkJtBnFVsqrbw"
PORT = int(os.getenv("PORT", "8000"))
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "")  # e.g. "http://localhost:8081"
DB_PATH = BASE_DIR / "logobot.db"

SAVED_LOGOS_DIR = BASE_DIR / "saved_logos"
SAVED_LOGOS_DIR.mkdir(parents=True, exist_ok=True)

# Qat'iy ketma-ket (1-by-1) ishlash uchun Global Navbat (Queue)
task_queue = None
health_server_started = False
worker_task = None

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
    waiting_for_remove_target_image = State()
    waiting_for_remove_files = State()


# --- KEYBOARDS ---
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 File logo qo'yish")],
            [KeyboardButton(text="🗑️ Keraksiz sahifani o'chirish")],
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
# FAYLLARNI QAYTA ISHLASH FUNKSIYALARI (PDF, ZIP, CBZ, RASMLAR)

# =====================================================================
# RASM TAQQOSLASH VA MANGA SAHIFALARINI O'CHIRISH (dHash)
# =====================================================================
def get_dhash(img: Image.Image, hash_size: int = 8) -> str:
    """Rasmdan 64-bitli dHash (Difference Hash) generatsiya qiladi."""
    try:
        img_l = img.convert('L').resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
        pixels = list(img_l.getdata())
        diff = []
        for row in range(hash_size):
            for col in range(hash_size):
                pixel_left = pixels[row * (hash_size + 1) + col]
                pixel_right = pixels[row * (hash_size + 1) + col + 1]
                diff.append(pixel_left > pixel_right)
        decimal_value = 0
        hex_string = []
        for index, value in enumerate(diff):
            if value:
                decimal_value += 2**(index % 8)
            if (index % 8) == 7:
                hex_string.append(hex(decimal_value)[2:].rjust(2, '0'))
                decimal_value = 0
        return ''.join(hex_string)
    except Exception:
        return ""

def hamming_dist(s1: str, s2: str) -> int:
    try:
        return bin(int(s1, 16) ^ int(s2, 16)).count('1')
    except Exception:
        return 999

def images_match(img1: Image.Image, target_hash: str, target_ratio: float, threshold: int = 14) -> bool:
    """Ikkita rasm bir xil yoki juda o'xshashligini tekshiradi (dHash + aspekt nisbati)."""
    try:
        r1 = img1.width / max(1, img1.height)
        # Aspekt nisbati o'xshash bo'lmasa darhol rad etamiz
        if abs(r1 - target_ratio) > 0.40:
            return False
        h1 = get_dhash(img1)
        if not h1 or not target_hash:
            return False
        dist = hamming_dist(h1, target_hash)
        return dist <= threshold
    except Exception:
        return False

def remove_pages_from_pdf(input_path: str, output_path: str, target_img_path: str, threshold: int = 14) -> int:
    """PDF ichidan ko'rsatilgan rasmga (reklama/muqova) mos sahifalarni topib o'chiradi."""
    try:
        with Image.open(target_img_path) as timg:
            target_hash = get_dhash(timg)
            target_ratio = timg.width / max(1, timg.height)

        if not target_hash:
            shutil.copyfile(input_path, output_path)
            return 0

        doc = fitz.open(input_path)
        total_p = len(doc)
        if total_p == 0:
            doc.close()
            shutil.copyfile(input_path, output_path)
            return 0

        removed_indices = []
        for page_num in range(total_p):
            page = doc[page_num]
            # Sahifani kichikroq o'lchamda render qilish (tezkor tekshiruv)
            pix = page.get_pixmap(dpi=72)
            pimg = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
            if images_match(pimg, target_hash, target_ratio, threshold):
                removed_indices.append(page_num)

        if removed_indices:
            # Барлық беттер mos келсе де, кем дегенде 1 бет қалу керек
            if len(removed_indices) < total_p:
                doc.delete_pages(removed_indices)
                doc.save(output_path, garbage=1, deflate=True)
            else:
                # Барлық беттер o'chirilsa - bo'sh PDF o'rniga aslini ko'chirib o'tkazamiz
                doc.close()
                shutil.copyfile(input_path, output_path)
                return 0
            doc.close()
            return len(removed_indices)
        else:
            doc.close()
            shutil.copyfile(input_path, output_path)
            return 0
    except Exception as err:
        logging.warning(f"remove_pages_from_pdf xatolik: {err}")
        shutil.copyfile(input_path, output_path)
        return 0

def remove_pages_from_archive(input_path: str, output_path: str, target_img_path: str, threshold: int = 14) -> int:
    """ZIP/CBZ arxiv ichidagi barcha sahifalarni tekshirib, o'xshashlarini o'chiradi."""
    try:
        with Image.open(target_img_path) as timg:
            target_hash = get_dhash(timg)
            target_ratio = timg.width / max(1, timg.height)

        if not target_hash:
            shutil.copyfile(input_path, output_path)
            return 0

        removed_count = 0
        with zipfile.ZipFile(input_path, 'r') as z_in:
            with zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_STORED) as z_out:
                for item in z_in.infolist():
                    if item.filename.endswith('/'):
                        z_out.writestr(item, z_in.read(item.filename))
                        continue
                    ext = Path(item.filename).suffix.lower()
                    data = z_in.read(item.filename)
                    if ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
                        try:
                            import io
                            with Image.open(io.BytesIO(data)) as simg:
                                if images_match(simg, target_hash, target_ratio, threshold):
                                    removed_count += 1
                                    continue
                        except Exception:
                            pass
                    z_out.writestr(item, data)
        return removed_count
    except Exception as err:
        logging.warning(f"remove_pages_from_archive xatolik: {err}")
        shutil.copyfile(input_path, output_path)
        return 0

# =====================================================================

def make_telegram_thumbnail(image_path: str, thumb_path: str) -> str:
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
    if not cover_path or not os.path.exists(cover_path):
        shutil.copyfile(input_path, output_path)
        return output_path

    try:
        doc = fitz.open(input_path)
        if doc.is_encrypted:
            doc.close()
            shutil.copyfile(input_path, output_path)
            return output_path

        total_pages = len(doc)
        if total_pages == 0:
            doc.close()
            shutil.copyfile(input_path, output_path)
            return output_path

        first_page_rect = doc[0].rect
        pw = first_page_rect.width if first_page_rect.width > 0 else 595.0

        try:
            with Image.open(cover_path) as cimg:
                cw, ch = cimg.size
        except Exception:
            cw, ch = 800, 800

        # Logotip rasmining asl nisbatiga (aspect ratio) mos balandlikni hisoblash
        # (Manhvaning 23,000px ga cho'zilib, fayl buzilishining oldini oladi)
        if cw > 0:
            cover_page_h = pw * (ch / cw)
        else:
            cover_page_h = pw * 1.414

        # Asl sahifalarni HECH QACHON O'CHIRMAYDI!
        # Logotip/muqovani eng boshiga (0-sahifaga) sifatli va to'liq joylashtiradi
        front_page = doc.new_page(0, width=pw, height=cover_page_h)
        front_page.insert_image(front_page.rect, filename=cover_path, keep_proportion=True)

        doc.save(output_path, garbage=1, deflate=True)
        doc.close()
    except Exception as err:
        logging.warning(f"PDF processing fallback to original: {err}")
        shutil.copyfile(input_path, output_path)
    return output_path


def process_archive_file(input_path: str, output_path: str, cover_path: Optional[str] = None) -> str:
    if not cover_path or not os.path.exists(cover_path):
        shutil.copyfile(input_path, output_path)
        return output_path

    try:
        with open(cover_path, "rb") as f:
            cover_bytes = f.read()

        cover_ext = Path(cover_path).suffix.lower()
        if not cover_ext:
            cover_ext = ".jpg"

        with zipfile.ZipFile(input_path, "r") as z_in:
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED) as z_out:
                # Logotipni arxiv boshiga muqova qilib qo'yish (asl rasmlarni o'chirmasdan)
                z_out.writestr(f"00000_cover{cover_ext}", cover_bytes)
                for item in z_in.infolist():
                    if not item.filename.endswith('/'):
                        z_out.writestr(item, z_in.read(item.filename))
    except Exception as err:
        logging.warning(f"Archive processing fallback to original: {err}")
        shutil.copyfile(input_path, output_path)
    return output_path


def process_image_file(input_path: str, output_path: str, cover_path: Optional[str] = None) -> str:
    if not cover_path or not os.path.exists(cover_path):
        shutil.copyfile(input_path, output_path)
        return output_path

    try:
        with Image.open(input_path) as base_img:
            base_img = base_img.convert("RGBA")
            with Image.open(cover_path) as logo_img:
                logo_img = logo_img.convert("RGBA")

                # Asl rasmning 20% gacha bo'lgan qismiga moslab logotipni kichraytirish
                bw, bh = base_img.size
                lw, lh = logo_img.size
                scale = min((bw * 0.20) / lw, (bh * 0.20) / lh, 1.0)
                new_size = (max(1, int(lw * scale)), max(1, int(lh * scale)))
                logo_resized = logo_img.resize(new_size, Image.Resampling.LANCZOS)

                # Pastki o'ng burchakka chiroyli joylashtirish
                margin = max(5, int(bw * 0.02))
                pos = (max(0, bw - new_size[0] - margin), max(0, bh - new_size[1] - margin))
                base_img.paste(logo_resized, pos, logo_resized)

                ext = Path(output_path).suffix.lower()
                if ext in ('.jpg', '.jpeg'):
                    base_img.convert("RGB").save(output_path, "JPEG", quality=95)
                else:
                    base_img.save(output_path)
    except Exception as err:
        logging.warning(f"Image processing fallback to original: {err}")
        shutil.copyfile(input_path, output_path)
    return output_path


def is_image_message(message: Message) -> bool:
    if message.photo:
        return True
    if message.document:
        if message.document.mime_type and message.document.mime_type.startswith("image/"):
            return True
        if message.document.file_name:
            ext = Path(message.document.file_name).suffix.lower()
            if ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
                return True
    return False


# =====================================================================
# QAT'IY 1-BY-1 KETMA-KETLIK YUKLASH TIZIMI (WORKER)
# =====================================================================

async def edit_status(bot: Bot, chat_id: int, message_id: int, text: str):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
    except Exception:
        try:
            clean = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", "")
            await bot.edit_message_text(clean, chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


USER_BATCHES: Dict[int, Dict[str, Any]] = {}

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

async def handle_user_batch_timer(chat_id: int, bot: Bot):
    """Oxirgi fayl kelganidan so'ng 2 sekund kutadi va Telegram message_id bo'yicha qat'iy tartiblab navbatga qo'shadi."""
    await asyncio.sleep(0.3)
    batch = USER_BATCHES.get(chat_id)
    if not batch or not batch.get("messages"):
        return

    if not batch.get("status_msg"):
        try:
            cnt = len(batch["messages"])
            batch["status_msg"] = await bot.send_message(
                chat_id,
                f"⏳ <b>Fayllar qabul qilinmoqda ({cnt} ta)...</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning(f"Status xabar yuborishda xatolik: {e}")

    last_count = len(batch["messages"])
    while True:
        await asyncio.sleep(0.5)
        batch = USER_BATCHES.get(chat_id)
        if not batch:
            return

        elapsed = time.time() - batch["last_received"]
        current_count = len(batch["messages"])

        if current_count != last_count and batch.get("status_msg"):
            last_count = current_count
            try:
                await bot.edit_message_text(
                    f"⏳ <b>Fayllar qabul qilinmoqda ({current_count} ta)...</b>",
                    chat_id=chat_id,
                    message_id=batch["status_msg"].message_id,
                    parse_mode="HTML"
                )
            except Exception:
                pass

        if elapsed >= 2.0:
            break

    batch = USER_BATCHES.pop(chat_id, None)
    if not batch:
        return

    messages: List[Message] = batch["messages"]
    active_logo = batch["active_logo"]
    status_msg = batch.get("status_msg")

    # Qat'iy ravishda Telegram message_id bo'yicha tartiblash (Telegram bo'yicha eng birinchi yuborilgani birinchi bo'ladi)
    messages.sort(key=lambda m: m.message_id)

    files_data = []
    seen_ids = set()
    for m in messages:
        if m.message_id not in seen_ids:
            seen_ids.add(m.message_id)
            fdata = extract_file_data(m)
            if fdata:
                files_data.append(fdata)

    if not files_data:
        if status_msg:
            try:
                await bot.delete_message(chat_id, status_msg.message_id)
            except Exception:
                pass
        return

    queue_size = task_queue.qsize()
    if status_msg:
        try:
            await bot.edit_message_text(
                f"⏳ <b>Barcha fayllar qabul qilindi ({len(files_data)} ta fayl tartib bilan).</b>\nNavbatga qo'shildi (Oldinda {queue_size} ta vazifa bor)...",
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode="HTML"
            )
        except Exception:
            pass

    await task_queue.put({
        "type": "album",
        "chat_id": chat_id,
        "active_logo": active_logo,
        "task_mode": batch.get("task_mode", "logo"),
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
            status_msg = task.get("status_msg")
            
            job_id = uuid.uuid4().hex[:8]
            temp_dir = Path(f"/tmp/job_{job_id}")
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            try:
                total_files = len(files)
                for idx, (f_type, filename, file_id, file_ext, msg_id) in enumerate(files, 1):
                    item_dir = temp_dir / f"item_{idx}"
                    item_dir.mkdir(parents=True, exist_ok=True)
                    safe_fn = html.escape(filename)
                    try:
                        # 1. Holat xabari
                        if status_msg:
                            status_text = (
                                f"⚙️ <b>Qayta ishlanmoqda ({idx}/{total_files}):</b>\n<code>{safe_fn}</code>"
                                if total_files > 1
                                else f"⚙️ <b>Qayta ishlanmoqda:</b>\n<code>{safe_fn}</code>"
                            )
                            await edit_status(bot, chat_id, status_msg.message_id, status_text)

                        # 2. Faylni yuklab olish
                        input_path = str(item_dir / f"in_{filename}")
                        tg_file = await bot.get_file(file_id)
                        await bot.download_file(tg_file.file_path, input_path, timeout=3600, chunk_size=1024 * 1024)

                        # 3. Logotip qo'yish
                        output_path = str(item_dir / f"out_{filename}")
                        thumb_path = str(item_dir / f"thumb_{idx}.jpg")
                        has_thumb = False

                        if active_logo and os.path.exists(active_logo):
                            make_telegram_thumbnail(active_logo, thumb_path)
                            has_thumb = True

                        task_mode = task.get("task_mode", "logo")
                        removed_info = ""
                        if task_mode == "remove":
                            if file_ext == ".pdf":
                                del_count = remove_pages_from_pdf(input_path, output_path, active_logo)
                                removed_info = f"\n🗑️ O'chirilgan sahifalar: <b>{del_count} ta</b>"
                            elif file_ext in (".zip", ".cbz"):
                                del_count = remove_pages_from_archive(input_path, output_path, active_logo)
                                removed_info = f"\n🗑️ O'chirilgan sahifalar: <b>{del_count} ta</b>"
                            else:
                                shutil.copyfile(input_path, output_path)
                        else:
                            if file_ext == ".pdf":
                                process_pdf_file(input_path, output_path, active_logo)
                            elif file_ext in (".zip", ".cbz"):
                                process_archive_file(input_path, output_path, active_logo)
                            elif file_ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                                process_image_file(input_path, output_path, active_logo)
                            else:
                                shutil.copyfile(input_path, output_path)

                        log_processed_file(chat_id, filename, file_ext)

                        # 4. Foydalanuvchiga bittadan (1-by-1) ketma-ket yuborish
                        fs_input = FSInputFile(output_path, filename=filename)
                        thumb_input = FSInputFile(thumb_path) if has_thumb and os.path.exists(thumb_path) else None
                        caption_text = (
                            f"✅ <b>Tayyor ({idx}/{total_files}):</b> <code>{safe_fn}</code>{removed_info}"
                            if total_files > 1
                            else f"✅ <b>Tayyor:</b> <code>{safe_fn}</code>{removed_info}"
                        )

                        if f_type == "photo":
                            try:
                                await bot.send_photo(
                                    chat_id,
                                    photo=fs_input,
                                    caption=caption_text,
                                    parse_mode="HTML",
                                    request_timeout=3600
                                )
                            except Exception as send_err:
                                logging.warning(f"send_photo HTML parse fallback: {send_err}")
                                await bot.send_photo(
                                    chat_id,
                                    photo=fs_input,
                                    caption=f"✅ Tayyor ({idx}/{total_files}): {filename}",
                                    request_timeout=3600
                                )
                        else:
                            try:
                                await bot.send_document(
                                    chat_id,
                                    document=fs_input,
                                    thumbnail=thumb_input,
                                    caption=caption_text,
                                    parse_mode="HTML",
                                    request_timeout=3600
                                )
                            except Exception as send_err:
                                logging.warning(f"send_document HTML parse fallback: {send_err}")
                                await bot.send_document(
                                    chat_id,
                                    document=fs_input,
                                    thumbnail=thumb_input,
                                    caption=f"✅ Tayyor ({idx}/{total_files}): {filename}",
                                    request_timeout=3600
                                )

                        await asyncio.sleep(0.5)

                    except Exception as item_err:
                        logging.exception(f"Error processing item {filename}: {item_err}")
                        err_text = str(item_err) if str(item_err).strip() else type(item_err).__name__
                        try:
                            await bot.send_message(
                                chat_id,
                                f"❌ <b>Xatolik ({idx}/{total_files}):</b> <code>{safe_fn}</code>\n<i>{html.escape(err_text)}</i>",
                                parse_mode="HTML"
                            )
                        except Exception:
                            try:
                                await bot.send_message(
                                    chat_id,
                                    f"❌ Xatolik ({idx}/{total_files}): {filename}\n{err_text}"
                                )
                            except Exception:
                                pass
                    finally:
                        if item_dir.exists():
                            shutil.rmtree(item_dir, ignore_errors=True)

                if status_msg:
                    try:
                        await bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        try:
                            await bot.edit_message_text(
                                f"✅ Barcha fayllar ({total_files} ta) muvaffaqiyatli tayyorlandi!",
                                chat_id=chat_id,
                                message_id=status_msg.message_id
                            )
                        except Exception:
                            pass
            except Exception as e:
                logging.exception(f"Error processing task: {e}")
                err_msg = str(e) if str(e).strip() else type(e).__name__
                if status_msg:
                    await edit_status(bot, chat_id, status_msg.message_id, f"❌ <b>Umumiy xatolik:</b> <code>{html.escape(err_msg)}</code>")
            finally:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                task_queue.task_done()
                await asyncio.sleep(0.5)


# =====================================================================
# ASOSIY MENYU VA START HANDLERS
# =====================================================================
@router.message(CommandStart())
@router.message(F.text.in_(["🔙 Asosiy menyu", "🔙 Bekor qilish (Asosiy menyuga qaytish)"]))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    USER_BATCHES.pop(message.chat.id, None)
    user = message.from_user
    saved_logo = get_user_saved_logo(user.id)
    logo_status = "✅ O'rnatilgan" if saved_logo else "❌ O'rnatilmagan"
    
    limit_status = "🚀 2000 MB (Local Bot API)" if LOCAL_API_URL else "📦 50 MB"

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


# =====================================================================
# FILE LOGO QO'YISH BOSQICHI
# =====================================================================
@router.message(F.text == "📁 File logo qo'yish")
async def cmd_file_logo_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    saved_logo = get_user_saved_logo(user_id)
    has_saved = bool(saved_logo and os.path.exists(saved_logo))

    text = "🖌️ <b>Fayllarga qaysi logotipni qo'ymoqchisiz?</b>\n\nTanlang:"
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
        "<i>(Fayllarni cheksiz miqdorda yuboraverishingiz mumkin. Bot ularni navbatma-navbat ishlashda davom etadi.)</i>\n\n"
        "Tugatish uchun pastdagi <b>«🔙 Bekor qilish»</b> tugmasini bosing."
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

    text = "📸 <b>Fayllarga qo'yiladigan yangi LOGOTIP rasmini (PNG / JPG) yuboring:</b>"
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cancel_to_main_kb())
    await callback.answer()


@router.message(BotStates.waiting_for_new_logo)
async def handle_new_logo_uploaded(message: Message, state: FSMContext, bot: Bot):
    if not is_image_message(message):
        await message.answer("⚠️ Iltimos, faqat rasm (PNG / JPG) formatida logotip yuboring!")
        return
    try:
        user_id = message.from_user.id
        timestamp = int(time.time() * 1000)
        temp_logo_path = SAVED_LOGOS_DIR / f"temp_{user_id}_{timestamp}.png"

        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, temp_logo_path, timeout=120)

        saved_logo = set_user_saved_logo(user_id, str(temp_logo_path))
        if temp_logo_path.exists():
            try:
                os.remove(temp_logo_path)
            except Exception:
                pass
        await state.update_data(active_logo=saved_logo)
        await state.set_state(BotStates.waiting_for_files)

        text = (
            "✅ <b>Yangi logotip qabul qilindi va saqlandi!</b>\n\n"
            "📤 <b>Endi fayllarni yuboring:</b>\n"
            "<i>(Fayllarni cheksiz miqdorda yuboraverishingiz mumkin. Bot ularni navbatma-navbat ishlashda davom etadi.)</i>\n\n"
            "Tugatish uchun pastdagi <b>«🔙 Bekor qilish»</b> tugmasini bosing."
        )
        await message.answer(text, parse_mode="HTML", reply_markup=files_receiving_kb())
    except Exception as e:
        await message.answer(f"❌ Logotipni yuklab olishda xatolik: {e}")


# =====================================================================
# DOIMIY LOGOTIP VA ADMINLAR
# =====================================================================
@router.message(F.text == "🖼️ Doimiy logotip")
async def cmd_saved_logo_menu(message: Message):
    user_id = message.from_user.id
    saved_logo = get_user_saved_logo(user_id)

    if saved_logo and os.path.exists(saved_logo):
        await message.answer_photo(
            photo=FSInputFile(saved_logo),
            caption="✅ <b>Sizning saqlangan doimiy logotipingiz.</b>\n\nFaylga logo qo'yishda har safar rasm yubormasdan, shu logotipdan foydalanishingiz mumkin.",
            parse_mode="HTML",
            reply_markup=saved_logo_menu_kb(has_saved_logo=True)
        )
    else:
        await message.answer(
            "ℹ️ <b>Sizda hali doimiy logotip saqlanmagan.</b>\nLogotip saqlash uchun quyidagi tugmani bosing:",
            parse_mode="HTML",
            reply_markup=saved_logo_menu_kb(has_saved_logo=False)
        )

@router.callback_query(F.data == "upload_permanent_logo")
async def cb_upload_permanent_logo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_permanent_logo)
    await callback.message.answer("📸 <b>Doimiy logotip sifatida saqlamoqchi bo'lgan RASMNI yuboring:</b>", parse_mode="HTML", reply_markup=cancel_to_main_kb())
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

@router.message(BotStates.waiting_for_permanent_logo)
async def handle_save_permanent_logo(message: Message, state: FSMContext, bot: Bot):
    if not is_image_message(message):
        await message.answer("⚠️ Iltimos, faqat rasm (PNG / JPG) formatida logotip yuboring!")
        return
    try:
        user_id = message.from_user.id
        temp_path = SAVED_LOGOS_DIR / f"temp_upload_{user_id}.png"
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, temp_path, timeout=120)
        set_user_saved_logo(user_id, str(temp_path))
        if temp_path.exists():
            os.remove(temp_path)
        await state.clear()
        await message.answer("✅ <b>Doimiy logotip muvaffaqiyatli saqlandi!</b>", parse_mode="HTML", reply_markup=main_menu_kb())
    except Exception as e:
        await message.answer(f"❌ Doimiy logotipni saqlashda xatolik: {e}")


@router.callback_query(F.data.startswith("save_as_perm:"))
async def cb_save_as_perm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    file_id = callback.data.split(":", 1)[1]
    try:
        user_id = callback.from_user.id
        temp_path = SAVED_LOGOS_DIR / f"temp_upload_{user_id}.png"
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, temp_path, timeout=120)
        set_user_saved_logo(user_id, str(temp_path))
        if temp_path.exists():
            os.remove(temp_path)
        await state.clear()
        await callback.message.edit_text("✅ <b>Doimiy logotip muvaffaqiyatli saqlandi!</b>", parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"❌ Xatolik: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("save_as_curr:"))
async def cb_save_as_curr(callback: CallbackQuery, state: FSMContext, bot: Bot):
    file_id = callback.data.split(":", 1)[1]
    try:
        user_id = callback.from_user.id
        timestamp = int(time.time() * 1000)
        temp_logo_path = SAVED_LOGOS_DIR / f"temp_{user_id}_{timestamp}.png"
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, temp_logo_path, timeout=120)
        saved_logo = set_user_saved_logo(user_id, str(temp_logo_path))
        if temp_logo_path.exists():
            try:
                os.remove(temp_logo_path)
            except Exception:
                pass
        await state.update_data(active_logo=saved_logo)
        await state.set_state(BotStates.waiting_for_files)
        await callback.message.edit_text(
            "✅ <b>Logotip tanlandi!</b>\n\n📤 <b>Endi fayllarni yuboring:</b>",
            parse_mode="HTML"
        )
        await callback.message.answer("Fayllarni yuborishingiz mumkin:", reply_markup=files_receiving_kb())
    except Exception as e:
        await callback.message.edit_text(f"❌ Xatolik: {e}")
    await callback.answer()

@router.message(F.text == "👥 Adminlar")
async def cmd_admins_menu(message: Message):
    admins = get_admins()
    admin_list_text = ""
    for idx, adm in enumerate(admins, 1):
        uname = f"@{adm['username']}" if adm.get('username') else "username yo'q"
        name = adm.get('full_name') or "Admin"
        admin_list_text += f"{idx}. <b>{name}</b> — <code>{adm['user_id']}</code> ({uname})\n"
    text = f"👥 <b>Barcha adminlar ro'yxati ({len(admins)} ta):</b>\n\n{admin_list_text}\n👇 <i>Admin qo'shish yoki o'chirish uchun quyidagi tugmalardan foydalaning:</i>"
    await message.answer(text, parse_mode="HTML", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_add_prompt")
async def cb_admin_add_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_admin_id)
    await callback.message.answer("✍️ <b>Qo'shmoqchi bo'lgan yangi adminning Telegram ID raqamini yuboring:</b>\n\n<i>Masalan: <code>1234567890</code></i>", parse_mode="HTML", reply_markup=cancel_to_main_kb())
    await callback.answer()

@router.message(BotStates.waiting_for_admin_id)
async def handle_admin_id_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqamlardan iborat to'g'ri Telegram ID yuboring!")
        return
    new_admin_id = int(text)
    success = add_admin(user_id=new_admin_id, username=None, full_name="Yangi Admin", added_by=message.from_user.id)
    await state.clear()
    if success:
        await message.answer(f"✅ <b>Yangi admin qo'shildi:</b> <code>{new_admin_id}</code>", parse_mode="HTML", reply_markup=main_menu_kb())
    else:
        await message.answer("❌ Adminni qo'shishda xatolik yuz berdi.", reply_markup=main_menu_kb())

@router.callback_query(F.data == "admin_remove_list")
async def cb_admin_remove_list(callback: CallbackQuery):
    admins = get_admins()
    buttons = [[InlineKeyboardButton(text=f"❌ O'chirish: {adm['user_id']}", callback_data=f"del_adm:{adm['user_id']}")] for adm in admins]
    buttons.append([InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="cancel_action")])
    await callback.message.edit_text("➖ <b>O'chirmoqchi bo'lgan adminni tanlang:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@router.callback_query(F.data.startswith("del_adm:"))
async def cb_delete_admin(callback: CallbackQuery):
    target_id = int(callback.data.split(":")[1])
    admins = get_admins()
    if len(admins) <= 1:
        await callback.answer("⚠️ Botda kamida 1 ta admin qolishi shart!", show_alert=True)
        return
    remove_admin(target_id)
    await callback.message.edit_text(f"🗑️ <b>Admin o'chirildi:</b> <code>{target_id}</code>", parse_mode="HTML")
    await callback.answer("O'chirildi")

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
# QAT'IY KETMA-KET NAVBATGA QO'SHISH (1-BY-1)
# =====================================================================
@router.message(BotStates.waiting_for_files, F.document | F.photo)
async def handle_incoming_files_in_queue(message: Message, state: FSMContext, bot: Bot):
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

    chat_id = message.chat.id
    now = time.time()

    # Race conditiondan himoyalangan sinxron navbatga yig'ish
    if chat_id not in USER_BATCHES:
        USER_BATCHES[chat_id] = {
            "messages": [],
            "active_logo": active_logo,
            "status_msg": None,
            "last_received": now,
            "timer_task": None
        }

    batch = USER_BATCHES[chat_id]
    if not any(m.message_id == message.message_id for m in batch["messages"]):
        batch["messages"].append(message)
    batch["last_received"] = now
    batch["active_logo"] = active_logo

    if batch.get("timer_task") is None or batch["timer_task"].done():
        batch["timer_task"] = asyncio.create_task(handle_user_batch_timer(chat_id, bot))


@router.message(F.document)
async def handle_direct_document_upload(message: Message, state: FSMContext, bot: Bot):
    current_state = await state.get_state()
    if current_state in (BotStates.waiting_for_new_logo, BotStates.waiting_for_permanent_logo, BotStates.waiting_for_admin_id, BotStates.waiting_for_remove_target_image, BotStates.waiting_for_remove_files):
        return

    user_id = message.from_user.id
    saved_logo = get_user_saved_logo(user_id)
    if saved_logo and os.path.exists(saved_logo):
        await state.update_data(active_logo=saved_logo)
        await state.set_state(BotStates.waiting_for_files)
        await handle_incoming_files_in_queue(message, state, bot)
    else:
        await message.answer(
            "⚠️ <b>Logotip tanlanmagan!</b>\n\n"
            "Fayllarga logotip qo'yish uchun quyidagi menyudan <b>«📁 File logo qo'yish»</b> tugmasini bosing yoki avval doimiy logotip yuklang.",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )


@router.message()
async def handle_direct_photo_upload(message: Message, state: FSMContext, bot: Bot):
    current_state = await state.get_state()
    if current_state in (BotStates.waiting_for_files, BotStates.waiting_for_new_logo, BotStates.waiting_for_permanent_logo, BotStates.waiting_for_admin_id, BotStates.waiting_for_remove_target_image, BotStates.waiting_for_remove_files):
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
    global task_queue, health_server_started, worker_task
    if task_queue is None:
        task_queue = asyncio.Queue()
    init_db()

    if not health_server_started:
        asyncio.create_task(run_health_server())
        health_server_started = True

    if worker_task is not None:
        worker_task.cancel()
    worker_task = asyncio.create_task(queue_worker_loop(bot))

    await bot.delete_webhook(drop_pending_updates=True)


async def on_shutdown(bot: Bot):
    await bot.session.close()


# =====================================================================
# KERAKSIZ SAHIFANI O'CHIRISH (MANGA CLEANER)
# =====================================================================
@router.message(F.text == "🗑️ Keraksiz sahifani o'chirish")
async def cmd_remove_page_start(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_remove_target_image)
    text = (
        "🗑️ <b>Keraksiz sahifani o'chirish bo'limi</b>\n\n"
        "📸 <b>Manga ichidan o'chirib tashlamoqchi bo'lgan sahifa yoki reklama rasmini (PNG / JPG) yuboring:</b>\n\n"
        "<i>Bot ushbu rasmga 100% yoki juda o'xshash barcha sahifalarni manga fayllari ichidan avtomatik qidirib topadi va butunlay o'chirib tashlaydi!</i>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=cancel_to_main_kb())

@router.message(BotStates.waiting_for_remove_target_image)
async def handle_remove_target_image_received(message: Message, state: FSMContext, bot: Bot):
    if not is_image_message(message):
        await message.answer("⚠️ Iltimos, o'chirilishi kerak bo'lgan sahifaning rasmini (PNG / JPG) yuboring!")
        return
    try:
        user_id = message.from_user.id
        timestamp = int(time.time() * 1000)
        target_path = SAVED_LOGOS_DIR / f"remove_target_{user_id}_{timestamp}.png"
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, target_path, timeout=120)

        await state.update_data(remove_target=str(target_path))
        await state.set_state(BotStates.waiting_for_remove_files)

        text = (
            "✅ <b>O'chirilishi kerak bo'lgan namuna rasm qabul qilindi!</b>\n\n"
            "📤 <b>Endi tozalanishi kerak bo'lgan Manga fayllarini (PDF, ZIP yoki CBZ) yuboring:</b>\n"
            "<i>(Bir nechta fayllarni birdaniga yuborishingiz mumkin. Bot ularning ichidagi barcha mos keluvchi sahifalarni bittadan o'chirib, toza holda qaytaradi.)</i>\n\n"
            "Tugatish uchun pastdagi <b>«🔙 Bekor qilish»</b> tugmasini bosing."
        )
        await message.answer(text, parse_mode="HTML", reply_markup=files_receiving_kb())
    except Exception as e:
        await message.answer(f"❌ Rasm yuklab olishda xatolik: {e}")

@router.message(BotStates.waiting_for_remove_files, F.document | F.photo)
async def handle_incoming_remove_files(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_img = data.get("remove_target")
    if not target_img or not os.path.exists(target_img):
        await message.answer("⚠️ O'chiriladigan rasm namunasi topilmadi! Iltimos, boshidan boshlang.")
        return

    chat_id = message.chat.id
    now = time.time()

    if chat_id not in USER_BATCHES:
        USER_BATCHES[chat_id] = {
            "messages": [],
            "active_logo": target_img,
            "task_mode": "remove",
            "status_msg": None,
            "last_received": now,
            "timer_task": None
        }

    batch = USER_BATCHES[chat_id]
    if not any(m.message_id == message.message_id for m in batch["messages"]):
        batch["messages"].append(message)
    batch["last_received"] = now
    batch["active_logo"] = target_img
    batch["task_mode"] = "remove"

    if batch["timer_task"] is None or batch["timer_task"].done():
        batch["timer_task"] = asyncio.create_task(handle_user_batch_timer(chat_id, bot))


# =====================================================================
# ASOSIY ISHGA TUSHIRISH (MAIN)
# =====================================================================
def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN topilmadi!")
        return

    api_url = LOCAL_API_URL
    if os.getenv("RENDER"):
        if "localhost" in api_url or "127.0.0.1" in api_url:
            api_url = api_url.replace("localhost", "157.173.110.5").replace("127.0.0.1", "157.173.110.5")
        if not api_url:
            api_url = "http://157.173.110.5:8081"

    async def run_polling():
        nonlocal api_url

        # Render port check har doim birinchi soniyadanoq muvaffaqiyatli bo'lishi uchun:
        await run_health_server()

        _bot = None
        use_local = False
        if api_url:
            try:
                is_local = os.getenv("IS_LOCAL", "").lower() in ("true", "1", "yes")
                test_session = AiohttpSession(
                    api=TelegramAPIServer.from_base(api_url, is_local=is_local),
                    timeout=5
                )
                test_bot = Bot(token=BOT_TOKEN, session=test_session)
                await asyncio.wait_for(test_bot.get_me(), timeout=4.0)
                await test_session.close()
                use_local = True
                print(f"Local Bot API muvaffaqiyatli ulandi: {api_url}")
            except Exception as e:
                print(f"Local Bot API ulanishda xatolik ({e}). Standart Telegram API-ga o'tilmoqda...")
                use_local = False

        if use_local:
            is_local = os.getenv("IS_LOCAL", "").lower() in ("true", "1", "yes")
            _session = AiohttpSession(
                api=TelegramAPIServer.from_base(api_url, is_local=is_local),
                timeout=3600
            )
            _bot = Bot(token=BOT_TOKEN, session=_session)
        else:
            _session = AiohttpSession(timeout=3600)
            _bot = Bot(token=BOT_TOKEN, session=_session)
            print("Standart Telegram API ishlatilmoqda.")

        _dp = Dispatcher(storage=MemoryStorage())
        _dp.include_router(router)

        try:
            await on_startup(_bot, _dp)
            await _dp.start_polling(_bot)
        finally:
            if _bot:
                await on_shutdown(_bot)

    asyncio.run(run_polling())


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
