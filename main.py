# Telegram_bot_QUIZimport asyncio
import nest_asyncio
nest_asyncio.apply()

from fastapi import FastAPI
import uvicorn
import threading

from bot import main as bot_main  # your existing bot code in bot.py

# --- Web server (needed for Render free plan) ---
app = FastAPI()

@app.get("/")
def home():
    return {"status": "Bot is running 🚀"}

# --- Run bot in background ---
def run_bot():
    asyncio.run(bot_main())

threading.Thread(target=run_bot).start()

# --- Run web server ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
