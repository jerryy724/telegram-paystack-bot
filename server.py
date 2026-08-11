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

GOLD_CHANNEL_ID = "-1004329655598"   # JAY GOLD MASTER VIP
FOREX_CHANNEL_ID = "-1004451754852"  # JAY FX PREMIUM SIGNALS

# SSL-ENCRYPTED MONGODB CONNECTION
mongo_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    tlsAllowInvalidCertificates=True
)
db = mongo_client["jay_empire_db"]
users_col = db["vip_users"]

telegram_app = Application.builder().token(BOT_TOKEN).build()

# GENERATE DYNAMIC 24-HOUR INVITE LINK VIA TELEGRAM API
async def generate_dynamic_link(bot: Bot, channel_type: str):
    target_channel = GOLD_CHANNEL_ID if channel_type == "gold" else FOREX_CHANNEL_ID
    expire_timestamp = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
    
    created_invite = await bot.create_chat_invite_link(
        chat_id=target_channel,
        member_limit=1,
        expire_date=expire_timestamp
    )
    return created_invite.invite_link

# DIRECT MINI APP LAUNCH ON /start
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

# DAILY EXPIRATION & REMINDER LOOP
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

    # Deactivate Expired Users
    expired_users = users_col.find({
        "is_active": True,
        "expires_at": {"$lte": now}
    })
    for user in expired_users:
        users_col.update_one({"_id": user["_id"]}, {"$set": {"is_active": False}})
        try:
            await bot.send_message(
                chat_id=user["telegram_id"],
                text="⚠️ <b>Jay Empire VIP Notice:</b> Your VIP access period has expired. Please renew inside the VIP Terminal."
            )
        except Exception as e:
            print(f"Deactivation Notice Error ({user['telegram_id']}): {e}")

async def scheduler_loop():
    while True:
        await check_expirations()
        await asyncio.sleep(86400) # Runs once every 24 hours

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

# PAYSTACK WEBHOOK RECEIVER
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
            
            # Save to Database
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
            
            # GENERATE FRESH INVITE LINK & SEND TO CHAT
            bot = Bot(token=BOT_TOKEN)
            try:
                fresh_invite = await generate_dynamic_link(bot, channel_type)
                channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
                
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Join {channel_name}", url=fresh_invite)]])
                await bot.send_message(
                    chat_id=tg_id,
                    text=f"🎉 <b>PAYMENT VERIFIED SUCCESSFULLY!</b>\n\nWelcome to <b>{channel_name}</b>. Tap below to join:\n\n🔗 <b>Your Invite Link:</b> {fresh_invite}\n\n<i>(Link valid for 24 hours)</i>",
                    parse_mode="HTML",
                    reply_markup=btn
                )
            except Exception as e:
                print(f"Error sending invite link to chat: {e}")

    return {"status": "success"}
