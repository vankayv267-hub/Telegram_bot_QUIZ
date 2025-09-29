import os
import asyncio
import random
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pymongo import MongoClient
from bson.objectid import ObjectId
from aiohttp import web
import certifi

# -------------------------
# Load environment variables
# -------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID")) if os.getenv("REPORT_CHANNEL_ID") else None
CHANNEL_TO_JOIN = int(os.getenv("CHANNEL_TO_JOIN")) if os.getenv("CHANNEL_TO_JOIN") else None
PORT = int(os.getenv("PORT", 10000))

# -------------------------
# Initialize bot & dispatcher
# -------------------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# -------------------------
# MongoDB setup
# -------------------------
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
SYSTEM_DBS = {"admin", "local", "config", "_quiz_meta_"}
meta_db = client["_quiz_meta_"]
user_progress_col = meta_db["user_progress"]
user_results_col = meta_db["user_results"]

# -------------------------
# FSM States
# -------------------------
class QuizStates(StatesGroup):
    waiting_for_ready = State()
    selecting_subject = State()
    selecting_topic = State()
    answering_quiz = State()
    post_quiz = State()
    reporting_issue = State()

# -------------------------
# Helpers
# -------------------------
def chunked(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]

def sanitize_question_doc(q):
    sanitized = {}
    for k, v in q.items():
        sanitized[k] = str(v) if isinstance(v, ObjectId) else v
    return sanitized

def clean_question_text(text):
    return re.sub(r"^\s*\d+\.\s*", "", (text or "")).strip()

def format_question_card(q):
    qtext = clean_question_text(q.get("question") or q.get("text") or "")
    opts = {}
    for letter in ['a', 'b', 'c', 'd']:
        candidate = q.get(f"option_{letter}") or q.get(letter) or q.get(letter.upper()) or q.get(f"opt_{letter}")
        if candidate:
            opts[letter] = candidate
            continue
        if isinstance(q.get("options"), dict) and q["options"].get(letter):
            opts[letter] = q["options"][letter]
            continue
        if isinstance(q.get("options"), list):
            idx = ord(letter) - 97
            if idx < len(q["options"]):
                opts[letter] = q["options"][idx]
                continue
        opts[letter] = ""
    parts = [qtext, ""]
    parts += [f"A: {opts['a']}", f"B: {opts['b']}", f"C: {opts['c']}", f"D: {opts['d']}"]
    return "\n".join(parts).strip()

def get_correct_answer(q):
    raw = (q.get('answer') or q.get('correct') or "").strip().lower()
    if raw in ['a','b','c','d']:
        return raw
    if raw.isdigit():
        return {'1':'a','2':'b','3':'c','4':'d'}.get(raw, 'a')
    m = re.search(r'([abcd])', raw)
    return m.group(1) if m else 'a'

def motivational_message():
    return random.choice([
        "Great job! Keep going 💪",
        "Nice! Every attempt makes you sharper 🚀",
        "Well done! 🔥",
        "Progress over perfection ✅",
    ])

def build_option_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("A", "answer:A"), InlineKeyboardButton("B", "answer:B")],
        [InlineKeyboardButton("C", "answer:C"), InlineKeyboardButton("D", "answer:D")]
    ])

async def is_channel_member(user_id: int) -> bool:
    if CHANNEL_TO_JOIN is None:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_TO_JOIN, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

# -------------------------
# Telegram handlers
# -------------------------
@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    if not await is_channel_member(message.from_user.id):
        await message.answer(f"⚠️ Please join the channel first!")
        return
    await state.set_state(QuizStates.waiting_for_ready)
    await message.answer("👋 Welcome to the Quiz Bot! Send any message to start your first quiz.")

@dp.message()
async def handle_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == QuizStates.waiting_for_ready:
        await message.answer("✅ Let's start your quiz!", reply_markup=build_option_keyboard())
        await state.set_state(QuizStates.answering_quiz)
    elif current_state == QuizStates.answering_quiz:
        await message.answer(f"{motivational_message()} Keep going!")

@dp.callback_query(F.data.startswith("answer:"))
async def handle_answer(call: CallbackQuery, state: FSMContext):
    selected = call.data.split(":")[1]
    await call.message.answer(f"You selected: {selected}\n{motivational_message()}")

# -------------------------
# Web server for alive check
# -------------------------
async def handle_ping(request):
    print(f"✅ Ping received at {datetime.now(timezone.utc).isoformat()}")
    return web.Response(text="Bot is alive!")

async def alive_checker():
    while True:
        if REPORT_CHANNEL_ID:
            try:
                await bot.send_message(REPORT_CHANNEL_ID, f"🤖 I am alive! Time: {datetime.now(timezone.utc).isoformat()}")
            except:
                pass
        await asyncio.sleep(300)

app = web.Application()
app.router.add_get("/", handle_ping)

# -------------------------
# Main function
# -------------------------
async def main():
    asyncio.create_task(alive_checker())
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🚀 Web server running on port {PORT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
