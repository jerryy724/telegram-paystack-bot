import os
import json
import httpx
import asyncio
import certifi
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from pymongo import MongoClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")

# SSL-ENCRYPTED MONGODB CONNECTION
mongo_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    tlsAllowInvalidCertificates=True
)
db = mongo_client["jay_empire_db"]
users_col = db["vip_users"]

# Telegram Bot Setup
telegram_app = Application.builder().token(BOT_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Launch VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text(
        "<b>Welcome to Jay Empire VIP Terminal 👑</b>\n\nTap below to launch the VIP Terminal Mini App:",
        parse_mode="HTML",
        reply_markup=kb
    )

telegram_app.add_handler(CommandHandler("start", start_cmd))

# Daily Expiration & Reminder Checker
async def check_expirations():
    bot = Bot(token=BOT_TOKEN)
    now = datetime.utcnow()
    
    # 1. Send 3-day renewal reminder
    reminder_target = now + timedelta(days=3)
    expiring_soon = users_col.find({
        "is_active": True,
        "reminder_sent": False,
        "expires_at": {"$lte": reminder_target}
    })
    
    for user in expiring_soon:
        try:
            await bot.send_message(
                chat_id=user["telegram_id"],
                text=f"👑 <b>Jay Empire VIP Alert:</b>\nYour access to <b>{user['channel_type'].upper()} VIP</b> expires in 3 days. Tap below to launch the terminal and extend your subscription.",
                parse_mode="HTML"
            )
            users_col.update_one({"_id": user["_id"]}, {"$set": {"reminder_sent": True}})
        except Exception as e:
            print(f"Error sending reminder to {user['telegram_id']}: {e}")

    # 2. Deactivate expired users
    expired_users = users_col.find({
        "is_active": True,
        "expires_at": {"$lte": now}
    })
    for user in expired_users:
        users_col.update_one({"_id": user["_id"]}, {"$set": {"is_active": False}})
        try:
            await bot.send_message(
                chat_id=user["telegram_id"],
                text="⚠️ <b>Jay Empire VIP Notice:</b> Your VIP access period has ended. Please renew inside the VIP Terminal to regain access."
            )
        except Exception as e:
            print(f"Error notifying expired user {user['telegram_id']}: {e}")

async def scheduler_loop():
    while True:
        await check_expirations()
        await asyncio.sleep(86400) # Run once every 24 hours

@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    asyncio.create_task(scheduler_loop())
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# Paystack Webhook Receiver Endpoint
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    payload = await request.json()
    if payload.get("event") == "charge.success":
        data = payload["data"]
        metadata = data.get("metadata", {})
        
        tg_id = metadata.get("telegram_id")
        channel_type = metadata.get("channel_type", "gold")
        days = int(metadata.get("days", 30))
        
        if tg_id and tg_id != 0:
            now = datetime.utcnow()
            expires_at = now + timedelta(days=days)
            
            users_col.update_one(
                {"telegram_id": tg_id, "channel_type": channel_type},
                {
                    "$set": {
                        "telegram_id": tg_id,
                        "channel_type": channel_type,
                        "purchased_at": now,
                        "expires_at": expires_at,
                        "is_active": True,
                        "reminder_sent": False,
                        "last_reference": data.get("reference")
                    }
                },
                upsert=True
            )
            print(f"✅ Logged payment for user {tg_id}: {days} days added.")
    return {"status": "success"}
