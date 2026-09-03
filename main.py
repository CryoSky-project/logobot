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
import fitz  # PyMuPDF
from PIL import Image
from aiohttp import web

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
    TelegramObject
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

logging.basicConfig(level=logging.INFO)

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


async def queue_worker_loop(bot: Bot):
    """Fayllarni bittadan (navbat bilan) yuklab oluvchi markaziy tsikl."""
    while True:
        task = await task_queue.get()
        if task is None:
            break
            
        user_chat_id, user_msg_id, active_logo, file_name, status_msg = task

        job_id = uuid.uuid4().hex[:8]
        temp_dir = Path(f"/tmp/job_{job_id}")
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # 1. Yuklab olish
            await edit_status(bot, user_chat_id, status_msg.message_id, "📥 <b>Navbat keldi:</b> Fayl yuklab olinmoqda...")
            
            # Asl fayl nomi bilan emas, input sifatida saqlaymiz (xatolik bo'lmasligi uchun)
            input_path = str(temp_dir / f"input_{file_name}")
            tg_file = await bot.get_file(status_msg.reply_to_message.document.file_id if status_msg.reply_to_message.document else status_msg.reply_to_message.photo[-1].file_id)
            await bot.download_file(tg_file.file_path, input_path)

            # 2. Qayta ishlash (Logotip qo'yish)
            await edit_status(bot, user_chat_id, status_msg.message_id, "⚙️ Fayl qayta ishlanmoqda (Logotip qo'yilmoqda)...")
            
            file_ext = Path(file_name).suffix.lower()
            # Fayl aniq o'zining nomi bilan chiqishi kerak
            output_path = str(temp_dir / file_name)
            thumb_path = str(temp_dir / "thumb.jpg")
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
                
            # 3. Tayyor faylni yuborish
            await edit_status(bot, user_chat_id, status_msg.message_id, "📤 Yuklanmoqda...")
            
            doc_input = FSInputFile(output_path, filename=file_name)
            thumb_input = FSInputFile(thumb_path) if has_thumb and os.path.exists(thumb_path) else None
            
            await bot.send_document(
                chat_id=user_chat_id,
                document=doc_input,
                thumbnail=thumb_input,
                caption=f"✅ <b>Tayyor:</b> <code>{file_name}</code>",
                parse_mode="HTML",
                reply_to_message_id=user_msg_id
            )

            log_processed_file(user_chat_id, file_name, file_ext)

            if status_msg:
                try:
                    await bot.delete_message(user_chat_id, status_msg.message_id)
                except Exception:
                    pass

        except Exception as e:
            await edit_status(bot, user_chat_id, status_msg.message_id, f"❌ <b>Xatolik ({file_name}):</b> <code>{e}</code>")
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            task_queue.task_done()
            await asyncio.sleep(1) # Bitta fayl tugagach, keyingisiga o'tishdan oldin qisqa tanaffus


# =====================================================================
# FAYLLARNI QAYTA ISHLASH FUNKSIYALARI (PDF, ZIP, CBZ, RASMLAR)
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
    if cover_path and os.path.exists(cover_path):
        shutil.copyfile(cover_path, output_path)
    else:
        shutil.copyfile(input_path, output_path)
    return output_path


# =====================================================================
# ASOSIY MENYU VA START HANDLERS
# =====================================================================
@router.message(CommandStart())
@router.message(F.text.in_(["🔙 Asosiy menyu", "🔙 Bekor qilish (Asosiy menyuga qaytish)"]))
async def cmd_start(message: Message, state: FSMContext):

    await state.clear()
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


@router.message(BotStates.waiting_for_new_logo, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_new_logo_uploaded(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    timestamp = int(time.time() * 1000)
    temp_logo_path = SAVED_LOGOS_DIR / f"temp_{user_id}_{timestamp}.png"

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, temp_logo_path)

    set_user_saved_logo(user_id, str(temp_logo_path))
    await state.update_data(active_logo=str(temp_logo_path))
    await state.set_state(BotStates.waiting_for_files)

    text = (
        "✅ <b>Yangi logotip qabul qilindi va saqlandi!</b>\n\n"
        "📤 <b>Endi fayllarni yuboring:</b>\n"
        "<i>(Fayllarni cheksiz miqdorda yuboraverishingiz mumkin. Bot ularni navbatma-navbat ishlashda davom etadi.)</i>\n\n"
        "Tugatish uchun pastdagi <b>«🔙 Bekor qilish»</b> tugmasini bosing."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=files_receiving_kb())


# =====================================================================
# DOIMIY LOGOTIP VA ADMINLAR (Standart)
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

@router.message(BotStates.waiting_for_permanent_logo, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_save_permanent_logo(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    temp_path = SAVED_LOGOS_DIR / f"temp_upload_{user_id}.png"
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, temp_path)
    set_user_saved_logo(user_id, str(temp_path))
    if temp_path.exists():
        os.remove(temp_path)
    await state.clear()
    await message.answer("✅ <b>Doimiy logotip muvaffaqiyatli saqlandi!</b>", parse_mode="HTML", reply_markup=main_menu_kb())

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
@router.message(BotStates.waiting_for_files, F.document)
async def handle_incoming_documents_in_queue(message: Message, state: FSMContext, bot: Bot):

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

    try:
        queue_size = task_queue.qsize()
        status_msg = await message.reply(f"⏳ <b>'{filename}'</b> navbatga qo'shildi (Oldinda {queue_size} ta fayl bor)...")
        await task_queue.put((message.chat.id, message.message_id, active_logo, filename, status_msg))
    except Exception:
        pass


@router.message(BotStates.waiting_for_files, F.photo)
async def handle_incoming_photos_in_queue(message: Message, state: FSMContext, bot: Bot):

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

    filename = f"photo_{message.message_id}.jpg"

    try:
        queue_size = task_queue.qsize()
        status_msg = await message.reply(f"⏳ <b>'{filename}'</b> navbatga qo'shildi (Oldinda {queue_size} ta fayl bor)...")
        await task_queue.put((message.chat.id, message.message_id, active_logo, filename, status_msg))
    except Exception:
        pass


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

    # LOCAL BOT API ULANISH
    if LOCAL_API_URL:
        is_local = os.getenv("IS_LOCAL", "").lower() in ("true", "1", "yes")
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(LOCAL_API_URL, is_local=is_local)
        )
        bot = Bot(token=BOT_TOKEN, session=session)
        print(f"Local Bot API ishlatilmoqda: {LOCAL_API_URL} (is_local={is_local})")
    else:
        bot = Bot(token=BOT_TOKEN)
        print("Oddiy Telegram Bot API ishlatilmoqda (Max 50MB).")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    async def run_polling():
        await on_startup(bot, dp)
        try:
            await dp.start_polling(bot)
        finally:
            await on_shutdown(bot)

    asyncio.run(run_polling())


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
