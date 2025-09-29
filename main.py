# Updated main bot script — more robust handling for Mongo documents & option formats
import asyncio
import os
import random
import re
import certifi
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pymongo import MongoClient
from bson.objectid import ObjectId
from aiohttp import web

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID')) if os.getenv('REPORT_CHANNEL_ID') else None
CHANNEL_TO_JOIN = int(os.getenv('CHANNEL_TO_JOIN')) if os.getenv('CHANNEL_TO_JOIN') else None

# Initialize bot and dispatcher with memory storage for FSM
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# MongoDB client with working SSL connection
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
SYSTEM_DBS = {"admin", "local", "config", "_quiz_meta_"}
meta_db = client["_quiz_meta_"]
user_progress_col = meta_db["user_progress"]
user_results_col = meta_db["user_results"]

# States for FSM
class QuizStates(StatesGroup):
    waiting_for_ready = State()
    selecting_subject = State()
    selecting_topic = State()
    answering_quiz = State()
    post_quiz = State()
    reporting_issue = State()

# -----------------------
# Helper utilities
# -----------------------
def chunked(lst: List[Any], n: int):
    return [lst[i:i + n] for i in range(0, len(lst), n)]

def sanitize_question_doc(q: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = {}
    for k, v in q.items():
        sanitized[k] = str(v) if isinstance(v, ObjectId) else v
    return sanitized

# =========================
# MongoDB Helpers
# =========================
def list_user_dbs() -> List[str]:
    try:
        return [dbname for dbname in client.list_database_names() if dbname not in SYSTEM_DBS]
    except:
        return []

def list_collections(dbname: str) -> List[str]:
    try:
        return client[dbname].list_collection_names()
    except:
        return []

def clean_question_text(text: str) -> str:
    return re.sub(r"^\s*\d+\.\s*", "", (text or "")).strip()

def fetch_nonrepeating_questions(dbname: str, colname: Optional[str], user_id: int, n: int = 10) -> List[Dict[str, Any]]:
    try:
        prog_key = {"user_id": user_id, "db": dbname, "collection": colname or "_RANDOM_"}
        doc = user_progress_col.find_one(prog_key) or {}
        served = set(doc.get("served_qids", []))
        results, pool = [], []

        if colname:
            cursor = client[dbname][colname].find({})
            for d in cursor:
                qid = d.get("question_id") or str(d.get("_id"))
                if qid not in served:
                    pool.append(d)
        else:
            for cname in list_collections(dbname):
                cursor = client[dbname][cname].find({})
                for d in cursor:
                    qid = d.get("question_id") or str(d.get("_id"))
                    if qid not in served:
                        pool.append(d)

        if not pool:
            return []

        random.shuffle(pool)
        for q in pool:
            qid = q.get("question_id") or str(q.get("_id"))
            if qid in served:
                continue
            served.add(qid)
            results.append(sanitize_question_doc(q))
            if len(results) >= n:
                break

        user_progress_col.update_one(prog_key, {"$set": {"served_qids": list(served)}}, upsert=True)
        return results[:n]
    except:
        return []

# =========================
# Question formatting + answer helpers
# =========================
def format_question_card(q: Dict[str, Any]) -> str:
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

def get_correct_answer(q: Dict[str, Any]) -> str:
    raw = (q.get('answer') or q.get('correct') or "").strip().lower()
    if raw in ['a','b','c','d']:
        return raw
    if raw.isdigit():
        return {'1':'a','2':'b','3':'c','4':'d'}.get(raw, 'a')
    m = re.search(r'([abcd])', raw)
    return m.group(1) if m else 'a'

def get_correct_option_text(q: Dict[str, Any], correct_letter: str) -> str:
    if f"option_{correct_letter}" in q:
        return q[f"option_{correct_letter}"]
    if isinstance(q.get("options"), dict) and q["options"].get(correct_letter):
        return q["options"][correct_letter]
    if isinstance(q.get("options"), list):
        idx = ord(correct_letter) - 97
        if idx < len(q["options"]):
            return q["options"][idx]
    return ""

def motivational_message() -> str:
    return random.choice([
        "Great job! Keep going 💪",
        "Nice! Every attempt makes you sharper 🚀",
        "Well done! 🔥",
        "Progress over perfection ✅",
    ])

# =========================
# Channel Membership Check
# =========================
async def is_channel_member(user_id: int) -> bool:
    if CHANNEL_TO_JOIN is None:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_TO_JOIN, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

# =========================
# Keyboard Helpers
# =========================
def create_inline_keyboard(button_texts, prefix, row_width=2):
    buttons = [InlineKeyboardButton(text=text, callback_data=f"{prefix}:{text}") for text in button_texts]
    rows = chunked(buttons, row_width)
    return InlineKeyboardMarkup(inline_keyboard=[[b for b in row] for row in rows])

def build_option_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("A", "answer:A"), InlineKeyboardButton("B", "answer:B")],
        [InlineKeyboardButton("C", "answer:C"), InlineKeyboardButton("D", "answer:D")]
    ])

# =========================
# Web server for alive check
# =========================
async def handle_ping(request):
    print(f"✅ Ping received at {datetime.now(timezone.utc).isoformat()}")
    return web.Response(text="Bot is alive!")

app = web.Application()
app.router.add_get("/", handle_ping)

async def alive_checker():
    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🚀 Web server running on port {port}")
    while True:
        if REPORT_CHANNEL_ID:
            try:
                await bot.send_message(REPORT_CHANNEL_ID, f"🤖 I am alive! Time: {datetime.now(timezone.utc).isoformat()}")
            except:
                pass
        await asyncio.sleep(300)  # every 5 minutes

# =========================
# Main function
# =========================
async def main():
    asyncio.create_task(alive_checker())
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
