# main.py
import asyncio
import logging
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
from pymongo.errors import ConnectionFailure
from bson.objectid import ObjectId

# ✅ NEW: aiohttp for minimal web server
from aiohttp import web

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID')) if os.getenv('REPORT_CHANNEL_ID') else None
CHANNEL_TO_JOIN = int(os.getenv('CHANNEL_TO_JOIN')) if os.getenv('CHANNEL_TO_JOIN') else None

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

try:
    client.admin.command('ping')
    logger.info("✅ MongoDB connection successful!")
except Exception as e:
    logger.exception(f"❌ MongoDB connection failed: {e}")
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
    """Small chunk helper (replacement for itertools.batched for portability)."""
    return [lst[i:i + n] for i in range(0, len(lst), n)]

def sanitize_question_doc(q: Dict[str, Any]) -> Dict[str, Any]:
    """Convert BSON types (ObjectId) to strings and ensure plain python types for FSM storage."""
    sanitized = {}
    for k, v in q.items():
        if isinstance(v, ObjectId):
            sanitized[k] = str(v)
        else:
            sanitized[k] = v
    return sanitized

# =========================
# MongoDB Helpers (more robust)
# =========================
def list_user_dbs() -> List[str]:
    """Get all user databases excluding system DBs"""
    try:
        return [dbname for dbname in client.list_database_names() if dbname not in SYSTEM_DBS]
    except ConnectionFailure:
        logger.error("MongoDB connection failed")
        return []

def list_collections(dbname: str) -> List[str]:
    """Get all collections in a database"""
    try:
        return client[dbname].list_collection_names()
    except Exception as e:
        logger.exception(f"Error getting collections for {dbname}: {e}")
        return []

def clean_question_text(text: str) -> str:
    """Clean question text by removing numbering"""
    return re.sub(r"^\s*\d+\.\s*", "", (text or "")).strip()

def fetch_nonrepeating_questions(dbname: str, colname: Optional[str], user_id: int, n: int = 10) -> List[Dict[str, Any]]:
    """
    Fetch questions that user hasn't seen before.
    This version avoids `$sample` issues and sanitizes documents.
    """
    try:
        prog_key = {"user_id": user_id, "db": dbname, "collection": colname or "_RANDOM_"}
        doc = user_progress_col.find_one(prog_key) or {}
        served = set(doc.get("served_qids", []))
        results: List[Dict[str, Any]] = []
        pool: List[Dict[str, Any]] = []

        if colname:
            # iterate over collection documents and add those not served
            cursor = client[dbname][colname].find({})
            for d in cursor:
                qid = d.get("question_id") or str(d.get("_id"))
                if qid not in served:
                    pool.append(d)
        else:
            # collect from all collections
            cols = list_collections(dbname)
            for cname in cols:
                cursor = client[dbname][cname].find({})
                for d in cursor:
                    qid = d.get("question_id") or str(d.get("_id"))
                    if qid not in served:
                        pool.append(d)

        if not pool:
            return []

        # randomize and pick up to n
        random.shuffle(pool)
        for q in pool:
            qid = q.get("question_id") or str(q.get("_id"))
            if qid in served:
                continue
            served.add(qid)
            results.append(sanitize_question_doc(q))
            if len(results) >= n:
                break

        # persist served list
        user_progress_col.update_one(prog_key, {"$set": {"served_qids": list(served)}}, upsert=True)
        logger.info(f"Fetched {len(results)} questions for user {user_id} (db={dbname}, col={colname})")
        return results[:n]
    except Exception as e:
        logger.exception(f"Error fetching questions: {e}")
        return []

# =========================
# Question formatting + answer helpers
# =========================
def format_question_card(q: Dict[str, Any]) -> str:
    """Format question with options (handles multiple DB shapes)."""
    try:
        qtext = clean_question_text(q.get("question") or q.get("text") or "")
        # Build options by trying common field names and fallback to 'options' array/dict
        opts = {}
        for letter in ['a', 'b', 'c', 'd']:
            # Try common variants
            candidate = (
                q.get(f"option_{letter}") or q.get(letter) or q.get(letter.upper()) or q.get(f"opt_{letter}")
            )
            if candidate:
                opts[letter] = candidate
                continue
            # Try options dict
            if isinstance(q.get("options"), dict) and q["options"].get(letter):
                opts[letter] = q["options"][letter]
                continue
            # Try options list (ordered)
            if isinstance(q.get("options"), list):
                idx = ord(letter) - 97
                if idx < len(q["options"]):
                    opts[letter] = q["options"][idx]
                    continue
            # fallback blank
            opts[letter] = ""

        parts = [qtext, ""]
        parts += [f"A: {opts['a']}", f"B: {opts['b']}", f"C: {opts['c']}", f"D: {opts['d']}"]
        return "\n".join(parts).strip()
    except Exception as e:
        logger.exception(f"Error formatting question: {e}")
        return "Error loading question"

def get_correct_answer(q: Dict[str, Any]) -> str:
    """Get correct answer letter robustly."""
    try:
        raw = (q.get('answer') or q.get('correct') or "").strip()
        if not raw:
            # try answer stored as numeric or in option_x form
            for key in ['answer_index', 'correct_index']:
                if key in q:
                    idx = int(q[key])
                    return {1: 'a', 2: 'b', 3: 'c', 4: 'd'}.get(idx, 'a')
            return 'a'

        raw_lower = raw.lower()
        # If 'c' or 'C' etc.
        if raw_lower in ('a', 'b', 'c', 'd'):
            return raw_lower
        # If numeric "3" -> c
        if raw_lower.isdigit():
            mapping = {'1': 'a', '2': 'b', '3': 'c', '4': 'd'}
            return mapping.get(raw_lower, 'a')
        # If 'option_c' style
        m = re.search(r'([abcd])', raw_lower)
        if m:
            return m.group(1)
        # fallback
        return 'a'
    except Exception as e:
        logger.exception(f"Error getting correct answer: {e}")
        return 'a'

def get_correct_option_text(q: Dict[str, Any], correct_letter: str) -> str:
    """Get the text of the correct option robustly."""
    try:
        field = f"option_{correct_letter}"
        if field in q and q[field]:
            return q[field]
        # try other variants
        for variant in [correct_letter, correct_letter.upper(), f"opt_{correct_letter}"]:
            if variant in q and q[variant]:
                return q[variant]
        if isinstance(q.get("options"), dict) and q["options"].get(correct_letter):
            return q["options"][correct_letter]
        if isinstance(q.get("options"), list):
            idx = ord(correct_letter) - 97
            if idx < len(q["options"]):
                return q["options"][idx]
        return ""
    except Exception as e:
        logger.exception(f"Error getting correct option text: {e}")
        return ""

def motivational_message() -> str:
    """Get random motivational message"""
    msgs = [
        "Great job! Keep going 💪",
        "Nice! Every attempt makes you sharper 🚀",
        "Well done! 🔥",
        "Progress over perfection ✅",
    ]
    return random.choice(msgs)

# =========================
# Channel Membership Check
# =========================
async def is_channel_member(user_id: int) -> bool:
    """Check if user is member of the required channel"""
    try:
        if CHANNEL_TO_JOIN is None:
            # If not configured, allow by default but log a warning
            logger.warning("CHANNEL_TO_JOIN is not set — allowing users by default")
            return True
        member = await bot.get_chat_member(chat_id=CHANNEL_TO_JOIN, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.exception(f"Error checking membership: {e}")
        return False

# =========================
# Keyboard Helpers
# =========================
def create_inline_keyboard(button_texts, prefix, row_width=2):
    """Create inline keyboard with callback data"""
    buttons = [InlineKeyboardButton(text=text, callback_data=f"{prefix}:{text}") for text in button_texts]
    rows = chunked(buttons, row_width)
    # convert to list-of-lists
    inline_keyboard = [[b for b in row] for row in rows]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

def build_option_keyboard() -> InlineKeyboardMarkup:
    """Build A/B/C/D options keyboard (2x2 layout)"""
    buttons = [
        [
            InlineKeyboardButton(text="A", callback_data="answer:A"),
            InlineKeyboardButton(text="B", callback_data="answer:B")
        ],
        [
            InlineKeyboardButton(text="C", callback_data="answer:C"),
            InlineKeyboardButton(text="D", callback_data="answer:D")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# =========================
# Handlers (unchanged flows, but extra logging)
# =========================
@dp.message(Command(commands=['start']))
async def start_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"/start from {user_id}")
    if await is_channel_member(user_id):
        ready_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I am ready", callback_data="ready")]
        ])
        await message.reply("🎉 Welcome! You're a member. Press 'I am ready' to start.", reply_markup=ready_keyboard)
        await state.set_state(QuizStates.waiting_for_ready)
    else:
        join_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Join Now", url="https://t.me/usersforstudy")],
            [InlineKeyboardButton(text="✅ Try Again", callback_data="try_again")]
        ])
        await message.reply("🔒 You must join our channel first to access quizzes.", reply_markup=join_keyboard)

@dp.callback_query(F.data == "try_again")
async def try_again_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    logger.info(f"try_again from {user_id}")
    if await is_channel_member(user_id):
        ready_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I am ready", callback_data="ready")]
        ])
        await callback.message.edit_text("🎉 Welcome! You're now a member. Press 'I am ready' to start.", reply_markup=ready_keyboard)
        await state.set_state(QuizStates.waiting_for_ready)
    else:
        await callback.answer("You haven't joined yet. Please join and try again.", show_alert=True)

@dp.callback_query(QuizStates.waiting_for_ready, F.data == "ready")
async def ready_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    subjects = list_user_dbs()
    if not subjects:
        await callback.message.reply("❌ No subjects available. Please try later.")
        await state.clear()
        return
    subject_keyboard = create_inline_keyboard(subjects, "subject", 2)
    await callback.message.reply("📚 Select a subject:", reply_markup=subject_keyboard)
    await state.set_state(QuizStates.selecting_subject)

@dp.callback_query(QuizStates.selecting_subject, F.data.startswith("subject:"))
async def subject_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    subject = callback.data.split(":", 1)[1]
    if subject not in list_user_dbs():
        await callback.message.reply("❌ Invalid subject. Please select from the buttons.")
        return
    await state.update_data(subject=subject)
    topics = list_collections(subject)
    if not topics:
        await callback.message.reply("❌ No topics available in this subject.")
        await state.clear()
        return
    topic_buttons = ["🎲 Random"] + topics
    topic_keyboard = create_inline_keyboard(topic_buttons, "topic", 2)
    await callback.message.reply("📖 Select a topic:", reply_markup=topic_keyboard)
    await state.set_state(QuizStates.selecting_topic)

@dp.callback_query(QuizStates.selecting_topic, F.data.startswith("topic:"))
async def topic_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    topic = callback.data.split(":", 1)[1]
    data = await state.get_data()
    subject = data.get('subject')
    topics = list_collections(subject)
    valid_topics = ["🎲 Random"] + topics
    if topic not in valid_topics:
        await callback.message.reply("❌ Invalid topic. Please select from the buttons.")
        return
    is_random = (topic == "🎲 Random")
    actual_topic = None if is_random else topic
    user_id = callback.from_user.id
    questions = fetch_nonrepeating_questions(subject, actual_topic, user_id, n=10)
    if len(questions) < 1:
        await callback.message.reply("❌ Not enough questions in this topic. Please select another.")
        await state.clear()
        return
    # store sanitized questions
    await state.update_data(
        topic=actual_topic,
        topic_display=topic,
        questions=questions,
        current_question=0,
        score=0
    )
    await callback.message.reply(f"🚀 Starting quiz: {subject} - {'Random' if is_random else topic}")
    await send_next_question(callback.message, state)

async def send_next_question(message: Message, state: FSMContext):
    """Send the next question or finish quiz (more robust and verbose logging)."""
    try:
        data = await state.get_data()
        questions = data.get('questions', [])
        current = int(data.get('current_question', 0))
        if current >= len(questions):
            # finish
            score = int(data.get('score', 0))
            total = len(questions)
            user_id = message.chat.id
            user_results_col.insert_one({
                "user_id": user_id,
                "db": data.get('subject'),
                "col": data.get('topic') or "_RANDOM_",
                "score": score,
                "total": total,
                "date": datetime.now(timezone.utc)
            })
            post_quiz_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Report an issue", callback_data="report_issue")],
                [InlineKeyboardButton(text="Start again", callback_data="start_again")]
            ])
            message_text = f"""🎉 Quiz finished!

✅ Correct: {score}
❌ Wrong: {total - score}

{motivational_message()}"""
            await message.reply(message_text, reply_markup=post_quiz_keyboard)
            await state.set_state(QuizStates.post_quiz)
            return

        q = questions[current]
        # defensive checks
        if not isinstance(q, dict):
            logger.error("Question entry is not a dict: %s", repr(q))
            await message.reply("❌ Error loading question (bad format). Please try again later.")
            await state.clear()
            return

        question_text = format_question_card(q)
        options_keyboard = build_option_keyboard()

        logger.info(f"Sending question #{current+1} to chat {message.chat.id}")
        await bot.send_message(message.chat.id,f"Question {current + 1}:\n\n{question_text}", reply_markup=options_keyboard)
        await state.set_state(QuizStates.answering_quiz)

    except Exception as e:
        logger.exception("Error in send_next_question")
        # show the real error in logs; send friendly message to user
        await message.reply("❌ Error loading question. Please try again later.")
        # keep state so user can try again or clear depending on severity
        # await state.clear()  # optional: clear to avoid stuck sessions

@dp.callback_query(QuizStates.answering_quiz, F.data.startswith("answer:"))
async def answer_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_answer = callback.data.split(":", 1)[1]  # A/B/C/D
    user_answer_lower = user_answer.lower()
    data = await state.get_data()
    questions = data.get('questions', [])
    current = int(data.get('current_question', 0))
    if current >= len(questions):
        await callback.message.reply("No active question. Start again.")
        await state.clear()
        return
    q = questions[current]
    correct_answer = get_correct_answer(q)
    correct_answer_upper = correct_answer.upper()
    correct_option_text = get_correct_option_text(q, correct_answer)
    score = int(data.get('score', 0))
    if user_answer_lower == correct_answer:
        response = f"✅ Correct! ({correct_answer_upper}) {correct_option_text}"
        score += 1
    else:
        response = f"❌ Wrong Answer! /n/nCorrect answer is ({correct_answer_upper}) {correct_option_text}"
    await callback.message.reply(response)
    await state.update_data(current_question=current + 1, score=score)
    await asyncio.sleep(1)
    await send_next_question(callback.message, state)

@dp.callback_query(QuizStates.post_quiz, F.data.startswith("report_issue"))
async def report_issue_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.reply("📷 Please send a screenshot or describe the issue.")
    await state.set_state(QuizStates.reporting_issue)

@dp.callback_query(QuizStates.post_quiz, F.data.startswith("start_again"))
async def start_again_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # reuse existing "ready" flow
    await ready_callback(callback, state)

@dp.message(QuizStates.reporting_issue)
async def report_issue_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    try:
        if message.text:
            report_text = f"🚨 Issue from @{username} (ID: {user_id}):\n{message.text}"
            if REPORT_CHANNEL_ID:
                await bot.send_message(REPORT_CHANNEL_ID, report_text)
        elif message.photo:
            caption = f"🚨 Issue from @{username} (ID: {user_id})"
            if REPORT_CHANNEL_ID:
                await bot.send_photo(REPORT_CHANNEL_ID, message.photo[-1].file_id, caption=caption)
        elif message.document:
            caption = f"🚨 Issue from @{username} (ID: {user_id})"
            if REPORT_CHANNEL_ID:
                await bot.send_document(REPORT_CHANNEL_ID, message.document.file_id, caption=caption)
        await message.reply("✅ Issue reported. Thank you!")
    except Exception as e:
        logger.exception("Error reporting issue")
        await message.reply("❌ Failed to send report. Please try again.")
    post_quiz_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Report an issue", callback_data="report_issue")],
        [InlineKeyboardButton(text="Start again", callback_data="start_again")]
    ])
    await message.reply("What would you like to do next?", reply_markup=post_quiz_keyboard)
    await state.set_state(QuizStates.post_quiz)
# Background task to send alive messages (keeps logs clear; safe-guarded)
async def alive_checker():
    while True:
        try:
            if REPORT_CHANNEL_ID:
                await bot.send_message(REPORT_CHANNEL_ID, f"🤖 I am alive! Time: {datetime.now(timezone.utc).isoformat()}")
        except Exception as e:
            logger.exception("Error sending alive message")
        await asyncio.sleep(300)  # 5 minutes

# ✅ NEW: aiohttp minimal web app
app = web.Application()

async def handle(request):
    return web.Response(text="Bot is alive")

app.router.add_get("/", handle)

# Main function to run bot + web server
async def main():
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting web server on port {port}")

    # Start aiohttp web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("🌍 Web server started successfully")

    # Clear webhook (important if deploying after webhook setup)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🧹 Webhook cleared")

    # Start background alive checker
    asyncio.create_task(alive_checker())

    # Start bot polling in parallel
    asyncio.create_task(dp.start_polling(bot))
    logger.info("🤖 Bot polling started")

    # Keep running forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
