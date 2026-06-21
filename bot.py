import asyncio
import os
from datetime import datetime, time
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sqlite3
from aiohttp import web

# ---------- Настройки ----------
TOKEN = "8990565741:AAFIFdBtUhBVOyfzuxeRRXkEJWt7asRnLxM"   # ← замени на свой токен
PORT = int(os.environ.get("PORT", 8080))

MORNING_START = time(7, 0)
MORNING_END = time(12, 0)
EVENING_START = time(19, 0)
EVENING_END = time(23, 59)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- База данных (в памяти для простоты, Render перезаписывает диск) ---
# ВАЖНО: на бесплатном Render данные могут теряться при перезапуске.
# Для сохранности можно подключить внешнюю БД, но для старта сойдёт.
conn = sqlite3.connect("data.db")
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    goal TEXT,
    morning_done INTEGER DEFAULT 0,
    evening_done INTEGER DEFAULT 0)""")
conn.commit()
conn.close()

def get_db():
    return sqlite3.connect("data.db")

def get_user(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT goal, morning_done, evening_done FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def set_goal(user_id, goal):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO users (user_id, goal, morning_done, evening_done) VALUES (?, ?, 0, 0)",
                    (user_id, goal))
        conn.commit()

def mark_morning(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET morning_done=1 WHERE user_id=?", (user_id,))
        conn.commit()

def mark_evening(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET evening_done=1 WHERE user_id=?", (user_id,))
        conn.commit()

def get_pending_morning():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE goal IS NOT NULL AND morning_done=0")
        return [row[0] for row in cur.fetchall()]

def get_pending_evening():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE goal IS NOT NULL AND evening_done=0")
        return [row[0] for row in cur.fetchall()]

def reset_daily():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET morning_done=0, evening_done=0")
        conn.commit()

# --- FSM ---
class GoalSetup(StatesGroup):
    waiting_for_goal = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Установи цель: /set_goal")

@dp.message(Command("set_goal"))
async def cmd_set_goal(message: types.Message, state: FSMContext):
    await state.set_state(GoalSetup.waiting_for_goal)
    await message.answer("Напиши свою аффирмацию одним сообщением:")

@dp.message(GoalSetup.waiting_for_goal)
async def process_goal(message: types.Message, state: FSMContext):
    goal = message.text.strip()
    if len(goal) < 3:
        await message.answer("Слишком коротко. Введи ещё раз.")
        return
    set_goal(message.from_user.id, goal)
    await state.clear()
    await message.answer(f"✅ Цель сохранена:\n«{goal}»\n\nКаждое утро (07:00–12:00) и вечер (19:00–23:59) присылай её точный текст.")

def in_time_window(now, start, end):
    if start <= end:
        return start <= now <= end
    else:
        return now >= start or now <= end

@dp.message(F.text)
async def handle_affirmation(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user or not user[0]:
        await message.answer("Сначала установи цель: /set_goal")
        return
    goal = user[0]
    if message.text.strip() != goal:
        await message.answer("❌ Текст не совпадает с аффирмацией.")
        return
    now = datetime.now().time()
    morning_done = bool(user[1])
    evening_done = bool(user[2])
    if in_time_window(now, MORNING_START, MORNING_END):
        if morning_done:
            await message.answer("🌅 Утренняя уже засчитана.")
        else:
            mark_morning(user_id)
            await message.answer("🌞 Молодец! Хорошего дня!")
        return
    if in_time_window(now, EVENING_START, EVENING_END):
        if evening_done:
            await message.answer("🌙 Вечерняя уже принята.")
        else:
            mark_evening(user_id)
            await message.answer("🌛 Отлично! Ты завершил день с мыслями о цели.")
        return
    await message.answer("⏳ Верный текст, но сейчас не время для отчёта (утро 07-12, вечер 19-23:59).")

# --- Планировщик ---
async def check_morning():
    for uid in get_pending_morning():
        try:
            await bot.send_message(uid, "⏰ Ты забыл утреннюю аффирмацию. Не отступай!")
        except:
            pass

async def check_evening():
    for uid in get_pending_evening():
        try:
            await bot.send_message(uid, "🌙 Вечер почти прошёл, а ты не повторил цель. Сейчас последний шанс!")
        except:
            pass

async def midnight_reset():
    reset_daily()

# --- Веб-сервер для Render (чтобы не усыплял) ---
async def handle_health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def main():
    scheduler.add_job(check_morning, 'cron', hour=12, minute=1)
    scheduler.add_job(check_evening, 'cron', hour=23, minute=59)
    scheduler.add_job(midnight_reset, 'cron', hour=0, minute=0)
    scheduler.start()
    await run_web()            # запускаем веб-сервер
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
