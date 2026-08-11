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

# ==============================================================================
# ENVIRONMENT CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")

GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID")
FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID")

# YOUR PRIMARY CHANNEL LINKS (FALLBACKS)
GOLD_PRIMARY_LINK = "https://t.me/+env-Zrui2ykwYjg8"
FOREX_PRIMARY_LINK = "https://t.me/+njii3OAHlqI3MjQ8"

# ==============================================================================
# MONGODB CONNECTION
# ==============================================================================
mongo_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    tlsAllowInvalidCertificates=True
)
db = mongo_client["jay_empire_db"]
users_col = db["vip_users"]

telegram_app = Application.builder().token(BOT_TOKEN).build()

# ==============================================================================
# TELEGRAM DYNAMIC LINK GENERATOR (24-HOUR EXPIRATION)
# ==============================================================================
async def generate_dynamic_link(bot: Bot, channel_type: str):
    target_channel = GOLD_CHANNEL_ID if channel_type == "gold" else FOREX_CHANNEL_ID
    
    # If environment variables for channel IDs are configured, generate single-use 24h link
    if target_channel:
        try:
            expire_dt = datetime.utcnow() + timedelta(hours=24)
            created_invite = await bot.create_chat_invite_link(
                chat_id=target_channel,
                member_limit=1,
                expire_date=expire_dt
            )
            return created_invite.invite_link
        except Exception as e:
            print(f"⚠️ Dynamic link generation error: {e}. Falling back to primary link.")
    
    # Fallback to direct primary links
    return GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK

# ==============================================================================
# BOT COMMAND HANDLERS
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Launch VIP Terminal App", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text(
        "<b>Welcome to Jay Empire VIP Terminal 👑</b>\n\nTap below to launch the VIP Mini App directly:",
        parse_mode="HTML",
        reply_markup=kb
    )

telegram_app.add_handler(CommandHandler("start", start_cmd))

# ==============================================================================
# SUBSCRIPTION TRACKER & REMINDER SCHEDULER
# ==============================================================================
async def check_expirations():
    bot = Bot(token=BOT_TOKEN)
    now = datetime.utcnow()
    
    # 3-Day Renewal Reminder
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
                text=f"👑 <b>Jay Empire VIP Alert:</b>\nYour access to <b>{user['channel_type'].upper()} VIP</b> expires in 3 days. Launch the VIP Terminal to extend your access.",
                parse_mode="HTML"
            )
            users_col.update_one({"_id": user["_id"]}, {"$set": {"reminder_sent": True}})
        except Exception as e:
            print(f"Reminder Error ({user['telegram_id']}): {e}")

    # Deactivate Expired Memberships
    expired_users = users_col.find({
        "is_active": True,
        "expires_at": {"$lte": now}
    })
    for user in expired_users:
        users_col.update_one({"_id": user["_id"]}, {"$set": {"is_active": False}})
        try:
            await bot.send_message(
                chat_id=user["telegram_id"],
                text="⚠️ <b>Jay Empire VIP Notice:</b> Your VIP access period has ended. Please renew inside the VIP Terminal."
            )
        except Exception as e:
            print(f"Deactivation Notice Error ({user['telegram_id']}): {e}")

async def scheduler_loop():
    while True:
        await check_expirations()
        await asyncio.sleep(86400) # Runs daily

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

# ==============================================================================
# PAYSTACK WEBHOOK RECEIVER
# ==============================================================================
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
            
            # 1. Update/Insert into MongoDB
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
            
            # 2. Generate Invite Link & Send via Bot
            bot = Bot(token=BOT_TOKEN)
            try:
                invite_url = await generate_dynamic_link(bot, channel_type)
                channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
                
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Join {channel_name}", url=invite_url)]])
                await bot.send_message(
                    chat_id=tg_id,
                    text=f"🎉 <b>PAYMENT VERIFIED SUCCESSFULLY!</b>\n\nWelcome to <b>{channel_name}</b>. Tap below to enter immediately:\n\n🔗 <b>Your Personal Link:</b> {invite_url}\n\n<i>(Single-use link valid for 24 hours)</i>",
                    parse_mode="HTML",
                    reply_markup=btn
                )
            except Exception as e:
                print(f"Error issuing invite link to user {tg_id}: {e}")

    return {"status": "success"}
