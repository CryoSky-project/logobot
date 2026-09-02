# 🤖 LogoBot - Render.com Ready Telegram Bot

Telegram orqali fayllarga (PDF, ZIP, CBZ, rasm va boshqa hujjatlarga) avtomatik logotip / muqova qo'yish va Telegram prevyusi (thumbnail) o'rnatish boti.

## 🚀 Imkoniyatlar
- **Faqat adminlar uchun:** Bot faqat tasdiqlangan adminlar uchun ishlaydi. Adminlarni to'g'ridan-to'g'ri Telegram orqali qo'shish va o'chirish mumkin.
- **Doimiy logotip:** Logotipni bir marta saqlab, keyinchalik barcha fayllarga tezkor qo'llash.
- **Batch Processing:** 10+ faylni ketma-ket yuborish va `/done` buyrug'i bilan yakunlash.
- **Xotirani avtomatik tozalash:** Har bir fayl qayta ishlanib Telegram'ga yuborilishi bilan server xotirasidan darhol o'chiriladi.
- **Avtomatik Webhook:** Render.com bergan domenga (`RENDER_EXTERNAL_URL`) avtomatik Webhook ulanadi.
- **24/7 Self-Ping:** Render Free planda uxlab qolmasligi uchun har 5 daqiqada o'z-o'ziga ping yuborib turadi.
- **Silent Mode:** Hech qanday keraksiz loglarsiz toza ishlaydi.

## 🛠️ O'rnatish va Ishga tushirish (Render.com)

1. [Render.com](https://dashboard.render.com) saytida **New Web Service** oching.
2. Ushbu GitHub repozitoriyani ulang.
3. Sozlamalar:
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python3 main.py`
4. **Environment Variables:**
   - `BOT_TOKEN` = `sizning_bot_tokeningiz`
   - `ADMIN_IDS` = `7052955513`
