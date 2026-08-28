import os
import asyncio
import logging
import ssl
import certifi
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pymongo import MongoClient, ASCENDING
from pymongo.server_api import ServerApi
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from telegram.ext import Application, CommandHandler

# ==============================================================================
# LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==============================================================================
# ENVIRONMENT CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-paystack-bot-415x.onrender.com")

# Channel/Group IDs (MUST be actual Telegram IDs, not invite links)
# Get these by adding @userinfobot to your channel/group
GOLD_GROUP_ID = os.getenv("GOLD_GROUP_ID", "-1001234567890")  # Replace with real ID
FOREX_GROUP_ID = os.getenv("FOREX_GROUP_ID", "-1001234567891")  # Replace with real ID

# Invite links for NEW members
GOLD_PRIMARY_LINK = "https://t.me/+env-Zrui2ykwYjg8"
FOREX_PRIMARY_LINK = "https://t.me/+njii3OAHlqI3MjQ8"

# ==============================================================================
# MONGODB CONNECTION — FIXED TLS FOR RENDER
# ==============================================================================
def init_mongodb():
    """
    Initialize MongoDB with explicit SSL context.
    This fixes TLSV1_ALERT_INTERNAL_ERROR on Render.
    """
    if not MONGO_URI:
        raise ValueError("MONGO_URI environment variable is not set!")

    # Build explicit SSL context using certifi's CA bundle
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl_context.check_hostname = True
    ssl_context.verify_mode = ssl.CERT_REQUIRED
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2  # Force TLS 1.2+

    client = MongoClient(
        MONGO_URI,
        tls=True,
        tlsCAFile=certifi.where(),
        server_api=ServerApi('1'),  # Required for Atlas compatibility
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=20000,
        socketTimeoutMS=45000,
        retryWrites=True,
        maxPoolSize=50,
    )

    # IMMEDIATE connection test — fail fast if broken
    try:
        client.admin.command('ping')
        logger.info("✅ MongoDB Atlas connected successfully!")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise

    db = client.get_default_database()
    
    # Ensure indexes for performance
    db.vip_users.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
    db.vip_users.create_index([("expires_at", ASCENDING)])
    db.vip_users.create_index([("is_active", ASCENDING)])
    db.leads.create_index([("telegram_id", ASCENDING)], unique=True)
    db.leads.create_index([("started_at", ASCENDING)])
    
    return db

# Initialize globally — app will crash on startup if DB is unreachable
db = init_mongodb()
users_col = db["vip_users"]
leads_col = db["leads"]

# ==============================================================================
# TELEGRAM BOT SETUP
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()

async def start_cmd(update: Update, context):
    """Handle /start command — log lead and show Mini App."""
    user = update.effective_user
    if not user:
        return

    # Log lead
    try:
        leads_col.update_one(
            {"telegram_id": user.id},
            {
                "$setOnInsert": {
                    "telegram_id": user.id,
                    "first_name": user.first_name,
                    "username": user.username,
                    "started_at": datetime.utcnow(),
                    "converted": False,
                    "followup_sent": False
                }
            },
            upsert=True
        )
        logger.info(f"Lead logged: {user.id}")
    except Exception as e:
        logger.error(f"Lead logging error: {e}")

    # Send Mini App button
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Launch VIP Terminal App", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text(
        "<b>Welcome to Jay Empire VIP Terminal 👑</b>\n\n"
        "Tap below to launch the VIP Mini App directly:",
        parse_mode="HTML",
        reply_markup=kb
    )

telegram_app.add_handler(CommandHandler("start", start_cmd))

# ==============================================================================
# SUBSCRIPTION MANAGEMENT FUNCTIONS
# ==============================================================================
async def kick_user_from_group(user_id: int, group_id: str, channel_type: str):
    """
    Ban then unban user — this removes them from group/channel
    but allows rejoining after payment.
    """
    bot = Bot(token=BOT_TOKEN)
    try:
        # Ban removes from group
        await bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        # Unban immediately so they can rejoin via invite link after paying
        await bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        
        channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ <b>Your {channel_name} access has expired.</b>\n\n"
                f"You've been removed from the group. Renew via the VIP Terminal to regain access."
            ),
            parse_mode="HTML"
        )
        logger.info(f"Kicked user {user_id} from {channel_type} group")
        return True
    except Exception as e:
        logger.error(f"Failed to kick user {user_id}: {e}")
        return False

async def send_renewal_reminder(user_id: int, channel_type: str, days_left: int):
    """Send 3-day expiry reminder."""
    bot = Bot(token=BOT_TOKEN)
    channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    try:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Renew VIP Access", web_app=WebAppInfo(url=MINI_APP_URL))]
        ])
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⏰ <b>{channel_name} Renewal Reminder</b>\n\n"
                f"Your access expires in <b>{days_left} day(s)</b>.\n\n"
                f"Renew now to avoid automatic removal from the group."
            ),
            parse_mode="HTML",
            reply_markup=kb
        )
        logger.info(f"Reminder sent to {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send reminder to {user_id}: {e}")
        return False

# ==============================================================================
# DAILY CRON JOB — CALL THIS FROM RENDER CRON OR EXTERNAL SCHEDULER
# ==============================================================================
async def run_daily_checks():
    """
    Run all daily subscription and lead checks.
    Call this via POST /cron/daily-check daily at 9 AM UTC.
    """
    now = datetime.utcnow()
    results = {"reminders_sent": 0, "users_kicked": 0, "leads_followed": 0, "errors": []}

    # ─── 1. Follow up unconverted leads (48h+) ────────────────────
    try:
        lead_cutoff = now - timedelta(hours=48)
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
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=lead["telegram_id"],
                    text=(
                        "👑 <b>Jay Empire VIP Market Alert</b>\n\n"
                        "High-precision trade setups are active right now. "
                        "Don't miss the next execution wave.\n\n"
                        "Tap below to lock in your membership:"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb
                )
                leads_col.update_one(
                    {"_id": lead["_id"]},
                    {"$set": {"followup_sent": True}}
                )
                results["leads_followed"] += 1
            except Exception as e:
                logger.error(f"Lead follow-up error {lead['telegram_id']}: {e}")
                results["errors"].append(f"lead_{lead['telegram_id']}: {str(e)}")
    except Exception as e:
        logger.error(f"DB error checking leads: {e}")
        results["errors"].append(f"leads_query: {str(e)}")

    # ─── 2. Send 3-day expiry reminders ───────────────────────────
    try:
        reminder_target = now + timedelta(days=3)
        expiring_soon = users_col.find({
            "is_active": True,
            "reminder_sent": False,
            "expires_at": {"$lte": reminder_target, "$gt": now}
        })
        
        for user in expiring_soon:
            days_left = (user["expires_at"] - now).days
            success = await send_renewal_reminder(
                user["telegram_id"], 
                user["channel_type"], 
                max(days_left, 1)
            )
            if success:
                users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"reminder_sent": True}}
                )
                results["reminders_sent"] += 1
    except Exception as e:
        logger.error(f"DB error checking renewals: {e}")
        results["errors"].append(f"renewals: {str(e)}")

    # ─── 3. Kick expired users from groups ────────────────────────
    try:
        expired_users = users_col.find({
            "is_active": True,
            "expires_at": {"$lte": now}
        })
        
        for user in expired_users:
            group_id = GOLD_GROUP_ID if user["channel_type"] == "gold" else FOREX_GROUP_ID
            
            # Kick from Telegram group
            await kick_user_from_group(user["telegram_id"], group_id, user["channel_type"])
            
            # Mark as inactive in DB
            users_col.update_one(
                {"_id": user["_id"]},
                {"$set": {"is_active": False, "kicked_at": now}}
            )
            results["users_kicked"] += 1
    except Exception as e:
        logger.error(f"DB error checking expirations: {e}")
        results["errors"].append(f"expirations: {str(e)}")

    logger.info(f"Daily check complete: {results}")
    return results

# ==============================================================================
# BACKGROUND SCHEDULER (Falls back if cron job fails)
# ==============================================================================
async def scheduler_loop():
    """Background loop — runs every 24 hours as backup."""
    while True:
        await asyncio.sleep(86400)  # 24 hours
        await run_daily_checks()

# ==============================================================================
# FASTAPI LIFESPAN
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    
    # Set webhook
    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(url=webhook_target)
    logger.info(f"Webhook set to: {webhook_target}")
    
    # Start background scheduler as backup
    asyncio.create_task(scheduler_loop())
    
    yield
    
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# API ENDPOINTS
# ==============================================================================

@app.get("/")
async def health_check():
    """Root health check."""
    return {
        "status": "active",
        "service": "Jay Empire VIP Backend",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health/db")
async def health_db():
    """Deep health check — verifies MongoDB connectivity."""
    try:
        db.command("ping")
        return {
            "status": "healthy",
            "mongodb": "connected",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail=f"Database unreachable: {str(e)}")

@app.post("/cron/daily-check")
async def cron_daily_check():
    """
    External cron endpoint.
    Call this daily via Render Cron Job or UptimeRobot.
    """
    results = await run_daily_checks()
    return JSONResponse({
        "status": "completed",
        "results": results,
        "timestamp": datetime.utcnow().isoformat()
    })

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates."""
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    """
    Receive Paystack payment confirmations.
    """
    payload = await request.json()
    
    if payload.get("event") == "charge.success":
        data = payload["data"]
        metadata = data.get("metadata", {})
        
        tg_id = metadata.get("telegram_id")
        channel_type = metadata.get("channel_type", "gold")
        days = int(metadata.get("days", 30))
        
        if not tg_id or tg_id == 0:
            logger.warning("Paystack webhook missing telegram_id")
            return {"status": "ignored"}
        
        now = datetime.utcnow()
        expires_at = now + timedelta(days=days)
        
        # Upsert VIP user
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
            
            # Mark lead as converted
            leads_col.update_one(
                {"telegram_id": tg_id},
                {"$set": {"converted": True, "converted_at": now}}
            )
            
            logger.info(f"VIP activated: user={tg_id}, plan={channel_type}, expires={expires_at}")
        except Exception as e:
            logger.error(f"Database update failed: {e}")
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
        
        # Send access link
        bot = Bot(token=BOT_TOKEN)
        try:
            target_link = GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK
            channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
            
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🚀 Enter {channel_name}", url=target_link)
            ]])
            await bot.send_message(
                chat_id=tg_id,
                text=(
                    f"🎉 <b>PAYMENT VERIFIED!</b>\n\n"
                    f"Plan: <b>{channel_type.upper()}</b>\n"
                    f"Duration: <b>{days} days</b>\n"
                    f"Expires: <b>{expires_at.strftime('%B %d, %Y')}</b>\n\n"
                    f"Tap below to join immediately:"
                ),
                parse_mode="HTML",
                reply_markup=btn
            )
        except Exception as e:
            logger.error(f"Failed to send access message to {tg_id}: {e}")

    return {"status": "success"}

# ==============================================================================
# RUN
# ==============================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
