import os
import asyncio
import nest_asyncio
nest_asyncio.apply()

import logging
import random
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

from dotenv import load_dotenv
import os

# Load .env file
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID"))
CHANNEL_TO_JOIN = int(os.getenv("CHANNEL_TO_JOIN"))

print("DEBUG BOT_TOKEN:", BOT_TOKEN) 


SYSTEM_DBS = {"admin", "local", "config", "_quiz_meta_"}

# =========================
# 🔌 Logging
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quiz-bot")

# =========================
# 🌐 MongoDB
# =========================
client = MongoClient(MONGO_URI)
meta_db = client["_quiz_meta_"]
user_progress_col = meta_db["user_progress"]
user_results_col = meta_db["user_results"]

# =========================
# 🧠 Helpers
# =========================
def list_user_dbs() -> List[str]:
    return [dbname for dbname in client.list_database_names() if dbname not in SYSTEM_DBS]

def list_collections(dbname: str) -> List[str]:
    return client[dbname].list_collection_names()

def clean_question_text(text: str) -> str:
    return re.sub(r"^\s*\d+\.\s*", "", text or "").strip()

def fetch_nonrepeating_questions(dbname: str, colname: Optional[str], user_id: int, n: int = 10) -> List[Dict[str, Any]]:
    prog_key = {"user_id": user_id, "db": dbname, "collection": colname or "_RANDOM_"}
    doc = user_progress_col.find_one(prog_key) or {}
    served = set(doc.get("served_qids", []))
    results = []

    if colname:
        pool = list(client[dbname][colname].aggregate([
            {"$match": {"question_id": {"$nin": list(served)}}},
            {"$sample": {"size": n * 5}}
        ]))
    else:
        cols = list_collections(dbname)
        pool = []
        for cname in cols:
            pool += list(client[dbname][cname].aggregate([
                {"$match": {"question_id": {"$nin": list(served)}}},
                {"$sample": {"size": max(3, n)}}
            ]))

    random.shuffle(pool)
    for q in pool:
        if q.get("question_id") not in served:
            results.append(q)
            served.add(q.get("question_id"))
        if len(results) >= n:
            break

    user_progress_col.update_one(prog_key, {"$set": {"served_qids": list(served)}}, upsert=True)
    return results[:n]

def format_question_card(q: Dict[str, Any]) -> str:
    qtext = clean_question_text(q.get("question", ""))
    opts = [
        f"(A) {q.get('option_a','')}",
        f"(B) {q.get('option_b','')}",
        f"(C) {q.get('option_c','')}",
        f"(D) {q.get('option_d','')}",
    ]
    return f"{qtext}\n\n" + "\n".join(opts)

def build_option_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(l, callback_data=f"ans:{l.lower()}")] for l in ["A","B","C","D"]]
    return InlineKeyboardMarkup(buttons)

def motivational_message() -> str:
    msgs = [
        "Great job! Keep going 💪",
        "Nice! Every attempt makes you sharper 🚀",
        "Well done! 🔥",
        "Progress over perfection ✅",
    ]
    return random.choice(msgs)

# =========================
# 🔑 Channel Membership Helper
# =========================
async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_TO_JOIN, user_id)
        return member.status in ["member", "creator", "administrator"]
    except:
        return False

async def show_main_menu(chat_id, context):
    user_id = chat_id
    is_member = await check_membership(user_id, context)
    if not is_member:
        keyboard = [
            [InlineKeyboardButton("🔗 Join Now", url="https://t.me/usersforstudy")],
            [InlineKeyboardButton("✅ Joined, Try Again", callback_data="check_join")]
        ]
        await context.bot.send_message(
            chat_id,
            "🔒 You must join our channel to access the quizzes.\n\nJoin the channel and then click below:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    db_names = [db for db in list_user_dbs() if db != "_quiz_meta_"]
    if not db_names:
        await context.bot.send_message(chat_id, "⚠ No quizzes available right now.")
        return

    welcome = "👋 Welcome!\n\nSelect a subject to start your quiz:"
    keyboard = [[InlineKeyboardButton(db, callback_data=f"db:{db}")] for db in db_names]
    await context.bot.send_message(chat_id, welcome, reply_markup=InlineKeyboardMarkup(keyboard))

# =========================
# 🧩 Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update.message.chat_id, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("db:"):
        dbname = data.split(":", 1)[1]
        cols = list_collections(dbname)
        buttons = [[InlineKeyboardButton("🎲 Random", callback_data=f"rnd:{dbname}")]]
        for cname in cols:
            buttons.append([InlineKeyboardButton(cname, callback_data=f"col:{dbname}:{cname}")])
        await query.edit_message_text(f"📚 {dbname} selected. Choose a topic:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("col:"):
        _, dbname, col = data.split(":")
        context.user_data["pending"] = {"db": dbname, "col": col}
        await query.edit_message_text(f"▶ {col} in {dbname}\n\nPress Start!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Start", callback_data=f"go:{dbname}:{col}")]]))

    elif data.startswith("rnd:"):
        _, dbname = data.split(":")
        context.user_data["pending"] = {"db": dbname, "col": None}
        await query.edit_message_text(f"🎲 Random from {dbname}\n\nPress Start!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Start", callback_data=f"go:{dbname}:_RANDOM_")]]))

    elif data.startswith("go:"):
        _, dbname, col = data.split(":")
        col = None if col == "_RANDOM_" else col
        questions = fetch_nonrepeating_questions(dbname, col, user_id, n=10)
        context.user_data["session"] = {"db": dbname, "col": col, "questions": questions, "i": 0, "score": 0}
        await send_current_question(update, context)

    elif data.startswith("ans:"):
        ans = data.split(":")[1]
        session = context.user_data.get("session")
        q = session["questions"][session["i"]]
        correct = (q.get("answer") or "").lower()
        correct_text = q.get(f"option_{correct}", "")

        if ans == correct:
            session["score"] += 1
            feedback = f"✅ Correct! ({correct.upper()}) {correct_text}"
        else:
            feedback = f"❌ Wrong. Correct is ({correct.upper()}) {correct_text}"

        await query.edit_message_text(feedback)
        session["i"] += 1
        if session["i"] >= len(session["questions"]):
            await end_quiz(update, context)
        else:
            await send_current_question(update, context)

    elif data == "restart":
        await show_main_menu(query.message.chat_id, context)

    elif data == "report":
        context.user_data["awaiting_report"] = True
        await query.edit_message_text("📷 Please send a screenshot or description of the issue.")

    elif data == "check_join":
        await show_main_menu(query.message.chat_id, context)

async def send_current_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = context.user_data["session"]
    q = session["questions"][session["i"]]
    await context.bot.send_message(chat_id, format_question_card(q), reply_markup=build_option_keyboard())

async def end_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = context.user_data["session"]
    score, total = session["score"], len(session["questions"])

    user_results_col.insert_one({
        "user_id": update.effective_user.id,
        "db": session["db"],
        "col": session["col"] or "_RANDOM_",
        "score": score, "total": total, "date": datetime.now(timezone.utc)
    })

    msg = f"🎉 Quiz finished!\n\n✅ Correct: {score}\n❌ Wrong: {total-score}\n\n{motivational_message()}"
    buttons = [
        [InlineKeyboardButton("Start Again", callback_data="restart")],
        [InlineKeyboardButton("Report Issue", callback_data="report")]
    ]
    await context.bot.send_message(chat_id, msg, reply_markup=InlineKeyboardMarkup(buttons))
    context.user_data.pop("session", None)

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_report"):
        context.user_data["awaiting_report"] = False
        try:
            if update.message.photo:
                photo = update.message.photo[-1].file_id
                await context.bot.send_photo(REPORT_CHANNEL_ID, photo, caption=f"Report from @{update.effective_user.username or update.effective_user.id}")
            elif update.message.text:
                await context.bot.send_message(REPORT_CHANNEL_ID, f"Report from @{update.effective_user.username or update.effective_user.id}:\n{update.message.text}")
            elif update.message.document:
                await context.bot.send_document(REPORT_CHANNEL_ID, update.message.document.file_id, caption=f"Report from @{update.effective_user.username or update.effective_user.id}")
            await update.message.reply_text("✅ Thanks! Your report has been forwarded.")
        except Exception as e:
            await update.message.reply_text("⚠ Failed to forward report. Please check bot permissions.")
            logger.error("Report forwarding error: %s", e)

# =========================
# 🌟 Alive Reporter
# =========================
async def alive_reporter(app):
    while True:
        try:
            await app.bot.send_message(REPORT_CHANNEL_ID, "🤖 I am alive")
        except Exception as e:
            logger.error("Failed to send alive message: %s", e)
        await asyncio.sleep(300)

# =========================
# 🚀 Run
# =========================
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^(db:|col:|rnd:|go:|ans:|restart|report|check_join)"))
    app.add_handler(MessageHandler(filters.ALL, handle_report))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    asyncio.create_task(alive_reporter(app))

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
