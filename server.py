import os
import certifi
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from pymongo import MongoClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from telegram.ext import Application, CommandHandler

# ==============================================================================
# ENVIRONMENT CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-paystack-bot-415x.onrender.com")

# OFFICIAL PERMANENT SINGLE CHANNEL INVITE LINKS
GOLD_PRIMARY_LINK = "https://t.me/+env-Zrui2ykwYjg8"
FOREX_PRIMARY_LINK = "https://t.me/+njii3OAHlqI3MjQ8"

# ==============================================================================
# MONGODB CONNECTION (Optimized with Timeout)
# ==============================================================================
mongo_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    tlsAllowInvalidCertificates=True,
    serverSelectionTimeoutMS=5000  # Prevents bot from freezing if DB is slow
)
db = mongo_client["jay_empire_db"]
users_col = db["vip_users"]
leads_col = db["leads"]

# Initialize Telegram Application
telegram_app = Application.builder().token(BOT_TOKEN).build()

# ==============================================================================
# BOT COMMAND HANDLERS
# ==============================================================================
async def start_cmd(update: Update, context):
    user = update.effective_user
    if user:
        # Wrap in try/except so a database hang DOES NOT stop the bot from replying
        try:
            leads_col.update_one(
                {"telegram_id": user.id},
                {
                    "$setOnInsert": {
                        "telegram_id": user.id,
                        "first_name": user.first_name,
                        "started_at": datetime.utcnow(),
                        "converted": False,
                        "followup_sent": False
                    }
                },
                upsert=True
            )
        except Exception as e:
            print(f"Lead Logging Error: {e}")

    # The bot will instantly send this regardless of DB status
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
# AUTOMATED REMINDERS & SCHEDULER
# ==============================================================================
async def check_expirations_and_leads():
    bot = Bot(token=BOT_TOKEN)
    now = datetime.utcnow()
    
    # 1. Prospective Lead Follow-up Reminder (48 Hours After /start)
    lead_cutoff = now - timedelta(hours=48)
    try:
        unconverted_leads = leads_col.find({
            "converted": False,
            "followup_sent": False,
            "started_at": {"$lte": lead_cutoff}
        })
        for lead in unconverted_leads:
            try:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👑 Enter VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]
                ])
                await bot.send_message(
                    chat_id=lead["telegram_id"],
                    text=(
                        "👑 <b>Jay Empire VIP Market Alert</b>\n\n"
                        "High-precision trade setups and institutional insights are active right now. "
                        "Don't miss the next execution wave.\n\n"
                        "Tap below to launch your VIP Terminal and lock in your membership:"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb
                )
                leads_col.update_one({"_id": lead["_id"]}, {"$set": {"followup_sent": True}})
            except Exception as e:
                print(f"Lead Follow-up Error ({lead['telegram_id']}): {e}")
    except Exception as e:
        print(f"DB Error checking leads: {e}")

    # 2. VIP 3-Day Renewal Reminder
    reminder_target = now + timedelta(days=3)
    try:
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
                print(f"Renewal Reminder Error ({user['telegram_id']}): {e}")
    except Exception as e:
        print(f"DB Error checking renewals: {e}")

    # 3. Deactivate Expired VIP Subscriptions
    try:
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
                print(f"Deactivation Error ({user['telegram_id']}): {e}")
    except Exception as e:
        print(f"DB Error checking expirations: {e}")

async def scheduler_loop():
    while True:
        await check_expirations_and_leads()
        await asyncio.sleep(86400) # Runs daily check

# ==============================================================================
# FASTAPI LIFESPAN MANAGER
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    
    # Configure Webhook automatically on startup (Removed drop_pending_updates)
    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(url=webhook_target)
    print(f"Telegram Webhook configured to: {webhook_target}")
    
    asyncio.create_task(scheduler_loop())
    
    yield
    
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# CRON-JOB KEEP-ALIVE ROUTE
# ==============================================================================
@app.get("/")
async def health_check():
    return {"status": "active", "service": "Jay Empire VIP Backend"}

# ==============================================================================
# TELEGRAM WEBHOOK ENDPOINT
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

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
            
            try:
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
                
                leads_col.update_one(
                    {"telegram_id": tg_id},
                    {"$set": {"converted": True}}
                )
            except Exception as e:
                print(f"Database update failed on webhook: {e}")
            
            bot = Bot(token=BOT_TOKEN)
            try:
                target_link = GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK
                channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
                
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Enter {channel_name}", url=target_link)]])
                await bot.send_message(
                    chat_id=tg_id,
                    text=f"🎉 <b>PAYMENT VERIFIED SUCCESSFULLY!</b>\n\nWelcome to <b>{channel_name}</b>. Tap below to enter immediately:\n\n🔗 <b>Your Access Link:</b> {target_link}",
                    parse_mode="HTML",
                    reply_markup=btn
                )
            except Exception as e:
                print(f"Error sending chat notification to {tg_id}: {e}")

    return {"status": "success"}
