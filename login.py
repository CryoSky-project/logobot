# -*- coding: utf-8 -*-
"""
Telegram StringSession Generator (Telethon)
Render.com-da 2 GB-gacha fayllarni erkin yuklash va yuborish uchun sessiya kalitini yaratadi.
"""

import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.getenv("API_ID", "39181210"))
API_HASH = os.getenv("API_HASH", "fb40947940c94722c5f0b7560df570fd")

async def main():
    print("=" * 50)
    print("  Telegram StringSession Generator (Telethon)  ")
    print("=" * 50)
    
    phone = input("\nTelefon raqamingizni kiriting (+7... / +998...): ").strip()
    if not phone:
        print("❌ Telefon raqami kiritilmadi!")
        return

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    print(f"[{phone}] raqamiga tasdiqlash kodi so'ralmoqda...")
    try:
        await client.send_code_request(phone)
    except Exception as e:
        print(f"❌ Kod so'rashda xatolik: {e}")
        await client.disconnect()
        return

    code = input("Telegram-ga kelgan 5 xonali tasdiqlash kodini kiriting: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("2FA Parolini (Two-Step Verification Password) kiriting: ")
        await client.sign_in(password=password)
    except Exception as e:
        print(f"❌ Kirishda xatolik: {e}")
        await client.disconnect()
        return

    me = await client.get_me()
    session_string = client.session.save()

    print("\n" + "=" * 50)
    print(f"✅ MUVAFFAQIYATLI ULINDI: {me.first_name} (@{me.username}) | ID: {me.id}")
    print("=" * 50)
    print("\n👇 Quyidagi SESSION_STRING ni Render.com Environment Variables-ga qo'shing:\n")
    print(f"SESSION_STRING={session_string}")
    print("\n" + "=" * 50)

    # Shuningdek faylga ham yozib qo'yamiz
    with open("session_string.txt", "w", encoding="utf-8") as f:
        f.write(session_string)
    print("ℹ️ Sessiya kaliti 'session_string.txt' fayliga ham saqlandi.")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
