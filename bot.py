# bot.py   —   Python ≥3.10         Версия: 2025‑05‑22
import asyncio, os, json, logging, aiohttp, aiosqlite
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.bot import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
# from pydantic import BaseSettings, Field
from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict
# from aiograph import Telegraph
from telegraph import Telegraph
from dotenv import load_dotenv

load_dotenv()


# ---------- 1. Конфигурация ----------
class Settings(BaseSettings):
    telegram_token: str = Field(..., validation_alias="TELEGRAM_BOT_TOKEN")
    deepseek_api_key: str = Field(..., validation_alias="DEEPSEEK_API_KEY")
    local_tz: str = "Etc/GMT-3"

    model_config = ConfigDict(
        env_file=".env",       # Явно указываем путь к .env
        env_file_encoding="utf-8",
        extra="ignore"         # Игнорировать лишние переменные
    )


# Создаем экземпляр настроек
settings = Settings()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
DB_PATH = "topics.db"
LOCAL_TZ = ZoneInfo(settings.local_tz)

# bot = Bot(settings.telegram_token, parse_mode="HTML")
bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
telegraph = Telegraph()                      # анонимный аккаунт
telegraph.create_account(short_name="deepstudy")


PROMPT_TEMPLATE = (
    'Привет! Помоги глубоко разобраться в теме: "{topic}".\n'
    "Сформируй структурированный материал объёмом 2500‑3000 слов (~15‑20 мин чтения).\n\n"
    "Требования к содержанию (Markdown):\n"
    "1. **Executive summary** – 3‑4 предложения.\n"
    "2. **Оглавление** (h2).\n"
    "3. Для каждого пункта: введение; ключевые технические детали; примеры;\n"
    "   сравнение альтернатив; подводные камни.\n"
    "4. Блок **«Ключевые выводы»** – bullet‑list.\n"
    "5. Три практических задания с критериями проверки.\n"
    "Не превышай 8000 токенов, избегай «воды», пиши на русском."
)

# ---------- 2. FSM для ввода темы ----------
class AddTopic(StatesGroup):
    waiting_topic = State()
class SetTime(StatesGroup):
    waiting_time = State()

# ---------- 3. Базовые хелперы ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                topic   TEXT
            );
            CREATE TABLE IF NOT EXISTS all_topics (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER,
                topic         TEXT,
                added_at      TIMESTAMP,
                telegraph_url TEXT,
                answered_at   TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                hour    INTEGER,
                minute  INTEGER
            );
        """)
        await db.commit()

async def get_user_time(db, user_id: int) -> Tuple[int, int]:
    cur = await db.execute("SELECT hour, minute FROM user_settings WHERE user_id=?",
                           (user_id,))
    row = await cur.fetchone()
    return row if row else (10, 0)          # дефолт 10:00 GMT+3

async def set_user_time(db, user_id: int, hour: int, minute: int):
    await db.execute("""
        INSERT INTO user_settings(user_id, hour, minute)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET hour=excluded.hour, minute=excluded.minute
    """, (user_id, hour, minute))
    await db.commit()

# ---------- 4. /start ----------
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton(text="✨ Сгенерировать сейчас", callback_data="gen_now")],
        [InlineKeyboardButton(text="🕒 Изменить время",      callback_data="chg_time")]
    ])

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "Привет! Записывай темы, а я буду генерировать подробные разборы.\n"
        "Кнопки внизу помогут управлять процессом.",
        reply_markup=main_keyboard()
    )

# ---------- 5. Добавление темы ----------
@dp.callback_query(F.data == "add_topic")
async def ask_topic(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите тему, которую хотите изучить:")
    await state.set_state(AddTopic.waiting_topic)
    await cb.answer()

@dp.message(AddTopic.waiting_topic)
async def save_topic(msg: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO topics(user_id, topic) VALUES(?,?)",
                         (msg.from_user.id, msg.text.strip()))
        await db.execute("""
            INSERT INTO all_topics(user_id, topic, added_at)
            VALUES(?,?,CURRENT_TIMESTAMP)
        """, (msg.from_user.id, msg.text.strip()))
        await db.commit()
    await msg.reply("Тема сохранена ✅", reply_markup=main_keyboard())
    await state.clear()

# ---------- 6. /list ----------
@dp.message(Command("list"))
async def list_queue(msg: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT topic FROM topics WHERE user_id=?",
                               (msg.from_user.id,))
        rows = await cur.fetchall()
    if not rows:
        await msg.answer("🚀 Очередь пуста.")
    else:
        await msg.answer("В очереди:\n" + "\n".join(f"• {r[0]}" for r in rows))

# ---------- 7. /history ----------
@dp.message(Command("history"))
async def history(msg: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT topic, telegraph_url, answered_at
            FROM all_topics
            WHERE user_id=? AND telegraph_url IS NOT NULL
            ORDER BY answered_at DESC
            LIMIT 30
        """, (msg.from_user.id,))
        rows = await cur.fetchall()
    if not rows:
        await msg.answer("Пока нет готовых статей.")
        return
    text = "\n".join(
        f"• <b>{t}</b> — <a href=\"{u}\">читать</a> ({a[:10]})"
        for t, u, a in rows
    )
    await msg.answer("<b>История:</b>\n" + text,
                     disable_web_page_preview=True)

# ---------- 8. Изменение времени ----------
@dp.callback_query(F.data == "chg_time")
async def ask_new_time(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите новое время в формате HH:MM (GMT+3):")
    await state.set_state(SetTime.waiting_time)
    await cb.answer()

@dp.message(SetTime.waiting_time)
async def set_time(msg: Message, state: FSMContext):
    try:
        hh, mm = map(int, msg.text.strip().split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
    except ValueError:
        await msg.reply("Неверный формат. Попробуйте ещё раз (HH:MM).")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await set_user_time(db, msg.from_user.id, hh, mm)
    await msg.reply(f"✅ Время изменено на {hh:02d}:{mm:02d} GMT+3",
                    reply_markup=main_keyboard())
    await state.clear()

# ---------- 9. Генерация «сейчас» ----------
@dp.callback_query(F.data == "gen_now")
async def generate_now(cb: CallbackQuery):
    await cb.answer()            # убрать «часики»
    await process_one_topic(cb.from_user.id, immediate=True)

@dp.message(Command("generate"))
async def cmd_generate(msg: Message):
    await process_one_topic(msg.from_user.id, immediate=True)

# ---------- 10. Логика DeepSeek + Telegraph ----------
async def deepseek_request(topic: str) -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}",
               "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user",
                      "content": PROMPT_TEMPLATE.format(topic=topic)}],
        "temperature": 0.7,
        "max_tokens": 8000
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(300)) as s:
        async with s.post(url, headers=headers, data=json.dumps(payload)) as r:
            r.raise_for_status()
            data = await r.json()
            return data["choices"][0]["message"]["content"]


async def publish_to_telegraph(title: str, md_content: str) -> str:
    page = telegraph.create_page(title = title, author_name = "DeepStudy Bot",html_content = md_content)
    return page["url"]


async def process_one_topic(user_id: int, immediate=False):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT rowid, topic FROM topics
            WHERE user_id=?
            ORDER BY rowid LIMIT 1
        """, (user_id,))
        row = await cur.fetchone()
        if not row:
            if immediate:
                await bot.send_message(user_id, "Очередь пуста — нечего генерировать.")
            return
        rowid, topic = row
        try:
            article = await deepseek_request(topic)
            url = await publish_to_telegraph(topic, article)
            await bot.send_message(user_id,
                f"📚 <b>{topic}</b>\nГотово! 👉 {url}")
            # архивируем
            await db.execute("""
                UPDATE all_topics
                SET telegraph_url=?, answered_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND topic=? AND telegraph_url IS NULL
            """, (url, user_id, topic))
            # удаляем из очереди
            await db.execute("DELETE FROM topics WHERE rowid=?", (rowid,))
            await db.commit()
        except Exception as e:
            logging.exception("Ошибка генерации %s для %s", topic, user_id)
            await bot.send_message(user_id,
                                   f"❌ Не удалось обработать «{topic}»:\n{e}")

# ---------- 11. Суточная задача ----------
async def scheduler():
    await asyncio.sleep(3)
    while True:
        now = datetime.now(LOCAL_TZ)
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT user_id, hour, minute FROM user_settings
            """)
            settings_rows = await cur.fetchall()
            # дефолты
            users_def = await db.execute("SELECT DISTINCT user_id FROM topics")
            for (uid,) in await users_def.fetchall():
                if uid not in [r[0] for r in settings_rows]:
                    settings_rows.append((uid, 10, 0))

        for uid, hh, mm in settings_rows:
            if now.hour == hh and now.minute == mm:
                asyncio.create_task(process_one_topic(uid))
        await asyncio.sleep(60)  # проверяем каждую минуту

# ---------- 12. Точка входа ----------
async def main():
    await init_db()

    # Telegraph: создаём аккаунт один раз
    telegraph.create_account(short_name="deepstudy")

    # планировщик + поллинг
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
