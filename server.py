"""
server.py — Jay Empire VIP Backend
With enhanced logging for debugging missing records
"""

import os
import asyncio
import logging
import ssl
import certifi
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo import MongoClient, ASCENDING
from pymongo.server_api import ServerApi
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from telegram.ext import Application, CommandHandler

# ==============================================================================
# LOGGING — Enhanced for debugging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==============================================================================
# ENVIRONMENT CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
MONGO_URI = os.getenv("MONGO_URI", "").strip().strip('"').strip("'")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-paystack-bot-415x.onrender.com")

GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID", "-1004329655598")
FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID", "-1004451754852")

GOLD_PRIMARY_LINK = "https://t.me/+env-Zrui2ykwYjg8"
FOREX_PRIMARY_LINK = "https://t.me/+njii3OAHlqI3MjQ8"

# ==============================================================================
# MONGODB CONNECTION
# ==============================================================================
def init_mongodb():
    if not MONGO_URI:
        logger.error("❌ MONGO_URI is not set!")
        return None, None, None, None

    logger.info(f"Connecting with URI type: {'srv' if 'mongodb+srv' in MONGO_URI else 'direct'}")

    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

        client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsCAFile=certifi.where(),
            server_api=ServerApi('1'),
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=20000,
            socketTimeoutMS=45000,
            retryWrites=True,
            maxPoolSize=50,
        )

        client.admin.command('ping')
        logger.info("✅ MongoDB Atlas connected successfully!")

        db = client.get_default_database()
        
        # Create indexes
        db.vip_users.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
        db.vip_users.create_index([("expires_at", ASCENDING)])
        db.vip_users.create_index([("is_active", ASCENDING)])
        db.leads.create_index([("telegram_id", ASCENDING)], unique=True)
        
        return client, db, db["vip_users"], db["leads"]

    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        return None, None, None, None

mongo_client, db, users_col, leads_col = init_mongodb()

# ==============================================================================
# TELEGRAM BOT SETUP
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()

async def start_cmd(update: Update, context):
    """Handle /start command."""
    user = update.effective_user
    if not user:
        return

    logger.info(f"🤖 /start received from user: {user.id} (@{user.username or 'no_username'})")

    if leads_col is not None:
        try:
            result = leads_col.update_one(
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
            if result.upserted_id:
                logger.info(f"✅ New lead logged: {user.id}")
            else:
                logger.info(f"ℹ️ Lead already exists: {user.id}")
        except Exception as e:
            logger.error(f"❌ Lead logging error: {e}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Launch VIP Terminal App", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text(
        "<b>Welcome to Jay Empire VIP Terminal 👑</b>\n\n"
        "Tap below to launch the VIP Mini App:",
        parse_mode="HTML",
        reply_markup=kb
    )

telegram_app.add_handler(CommandHandler("start", start_cmd))

# ==============================================================================
# SUBSCRIPTION MANAGEMENT
# ==============================================================================
async def kick_from_channel(user_id: int, channel_id: str, channel_type: str):
    """Ban then unban user — removes from channel but allows rejoin."""
    bot = Bot(token=BOT_TOKEN)
    channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    
    try:
        await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
        
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ <b>Your {channel_name} access has expired.</b>\n\n"
                f"You've been removed from the channel. "
                f"Renew via the VIP Terminal to regain access."
            ),
            parse_mode="HTML"
        )
        logger.info(f"✅ Kicked user {user_id} from {channel_type}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to kick user {user_id}: {e}")
        return False

async def send_reminder(user_id: int, channel_type: str, days_left: int):
    """Send renewal reminder."""
    bot = Bot(token=BOT_TOKEN)
    channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    
    try:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Renew Now", web_app=WebAppInfo(url=MINI_APP_URL))]
        ])
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⏰ <b>{channel_name} Renewal Reminder</b>\n\n"
                f"Your access expires in <b>{days_left} day(s)</b>.\n"
                f"Renew now to avoid automatic removal."
            ),
            parse_mode="HTML",
            reply_markup=kb
        )
        logger.info(f"✅ Reminder sent to {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Reminder failed for {user_id}: {e}")
        return False

# ==============================================================================
# DAILY CHECKS
# ==============================================================================
async def run_daily_checks():
    """Run all daily subscription checks."""
    if users_col is None or leads_col is None:
        logger.error("Database not available, skipping daily checks")
        return {"error": "Database not connected"}

    now = datetime.utcnow()
    results = {"reminders_sent": 0, "users_kicked": 0, "leads_followed": 0, "errors": []}

    # 1. Follow up unconverted leads (48h+)
    try:
        lead_cutoff = now - timedelta(hours=48)
        unconverted = leads_col.find({
            "converted": False,
            "followup_sent": False,
            "started_at": {"$lte": lead_cutoff}
        })
        
        for lead in unconverted:
            try:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👑 Enter VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]
                ])
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=lead["telegram_id"],
                    text=(
                        "👑 <b>Jay Empire VIP Market Alert</b>\n\n"
                        "High-precision trade setups are active now. "
                        "Don't miss the next wave.\n\n"
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
                logger.info(f"✅ Follow-up sent to lead: {lead['telegram_id']}")
            except Exception as e:
                logger.error(f"❌ Lead follow-up error: {e}")
                results["errors"].append(f"lead_{lead['telegram_id']}: {str(e)}")
    except Exception as e:
        logger.error(f"❌ DB error (leads): {e}")
        results["errors"].append(f"leads_query: {str(e)}")

    # 2. Send 3-day expiry reminders
    try:
        reminder_target = now + timedelta(days=3)
        expiring = users_col.find({
            "is_active": True,
            "reminder_sent": False,
            "expires_at": {"$lte": reminder_target, "$gt": now}
        })
        
        for user in expiring:
            days_left = max((user["expires_at"] - now).days, 1)
            if await send_reminder(user["telegram_id"], user["channel_type"], days_left):
                users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"reminder_sent": True}}
                )
                results["reminders_sent"] += 1
    except Exception as e:
        logger.error(f"❌ DB error (reminders): {e}")
        results["errors"].append(f"reminders: {str(e)}")

    # 3. Kick expired users
    try:
        expired = users_col.find({
            "is_active": True,
            "expires_at": {"$lte": now}
        })
        
        for user in expired:
            channel_id = GOLD_CHANNEL_ID if user["channel_type"] == "gold" else FOREX_CHANNEL_ID
            
            if await kick_from_channel(user["telegram_id"], channel_id, user["channel_type"]):
                users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"is_active": False, "kicked_at": now}}
                )
                results["users_kicked"] += 1
    except Exception as e:
        logger.error(f"❌ DB error (expired): {e}")
        results["errors"].append(f"expired: {str(e)}")

    logger.info(f"📊 Daily check complete: {results}")
    return results

async def scheduler_loop():
    """Background scheduler fallback."""
    while True:
        await asyncio.sleep(86400)
        await run_daily_checks()

# ==============================================================================
# FASTAPI LIFESPAN
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    
    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(url=webhook_target)
    logger.info(f"✅ Webhook set: {webhook_target}")
    
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
    db_status = "connected" if db is not None else "disconnected"
    return {
        "status": "active",
        "mongodb": db_status,
        "service": "Jay Empire VIP Backend",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health/db")
async def health_db():
    """Deep health check."""
    if db is None:
        return JSONResponse(
            {"status": "unhealthy", "mongodb": "not_initialized"},
            status_code=503
        )
    try:
        db.command("ping")
        return {
            "status": "healthy",
            "mongodb": "connected",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Health check failed: {e}")
        return JSONResponse(
            {"status": "unhealthy", "mongodb": str(e)},
            status_code=503
        )

@app.post("/cron/daily-check")
async def cron_daily_check():
    """External cron endpoint."""
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
    ENHANCED LOGGING for debugging missing records.
    """
    try:
        payload = await request.json()
        logger.info(f"💰 Paystack webhook received: {payload.get('event')}")
        
        if payload.get("event") == "charge.success":
            data = payload["data"]
            metadata = data.get("metadata", {})
            
            tg_id = metadata.get("telegram_id")
            channel_type = metadata.get("channel_type", "gold")
            days = int(metadata.get("days", 30))
            reference = data.get("reference", "unknown")
            
            logger.info(f"💳 Payment success: ref={reference}, user={tg_id}, channel={channel_type}, days={days}")
            
            if not tg_id or tg_id == 0:
                logger.warning("⚠️ Paystack webhook missing telegram_id")
                return {"status": "ignored"}
            
            now = datetime.utcnow()
            expires_at = now + timedelta(days=days)
            
            if users_col is not None:
                try:
                    # Upsert VIP user
                    result = users_col.update_one(
                        {"telegram_id": tg_id, "channel_type": channel_type},
                        {
                            "$set": {
                                "telegram_id": tg_id,
                                "channel_type": channel_type,
                                "purchased_at": now,
                                "expires_at": expires_at,
                                "is_active": True,
                                "reminder_sent": False,
                                "last_reference": reference,
                                "amount_paid": data.get("amount"),
                                "currency": data.get("currency"),
                                "customer_email": data.get("customer", {}).get("email"),
                                "paystack_reference": reference
                            }
                        },
                        upsert=True
                    )
                    
                    logger.info(f"✅ VIP user upserted: user={tg_id}, channel={channel_type}, matched={result.matched_count}, modified={result.modified_count}")
                    
                    # Mark lead as converted
                    lead_result = leads_col.update_one(
                        {"telegram_id": tg_id},
                        {"$set": {"converted": True, "converted_at": now, "converted_channel": channel_type}}
                    )
                    logger.info(f"✅ Lead marked converted: user={tg_id}, matched={lead_result.matched_count}")
                    
                except Exception as e:
                    logger.error(f"❌ Database update failed: {e}")
                    return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
            else:
                logger.error("❌ Database not available, cannot process payment")
                return JSONResponse({"status": "error", "detail": "Database offline"}, status_code=503)
            
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
                        f"Tap below to join:"
                    ),
                    parse_mode="HTML",
                    reply_markup=btn
                )
                logger.info(f"✅ Access message sent to {tg_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send access message to {tg_id}: {e}")

        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"❌ Paystack webhook error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ==============================================================================
# ADMIN ENDPOINTS — View Data Easily
# ==============================================================================

@app.get("/admin/users")
async def get_all_users():
    """View all VIP subscribers."""
    if users_col is None:
        return JSONResponse({"error": "Database not connected"}, status_code=503)
    
    users = list(users_col.find({}, {"_id": 0}))
    return {
        "total_subscribers": len(users),
        "active": sum(1 for u in users if u.get("is_active")),
        "expired": sum(1 for u in users if not u.get("is_active")),
        "users": users
    }

@app.get("/admin/leads")
async def get_all_leads():
    """View all leads."""
    if leads_col is None:
        return JSONResponse({"error": "Database not connected"}, status_code=503)
    
    leads = list(leads_col.find({}, {"_id": 0}))
    return {
        "total_leads": len(leads),
        "converted": sum(1 for l in leads if l.get("converted")),
        "unconverted": sum(1 for l in leads if not l.get("converted")),
        "leads": leads
    }

@app.get("/admin/dashboard")
async def admin_dashboard():
    """Quick stats overview."""
    if users_col is None or leads_col is None:
        return JSONResponse({"error": "Database not connected"}, status_code=503)
    
    now = datetime.utcnow()
    
    total_users = users_col.count_documents({})
    active_users = users_col.count_documents({"is_active": True})
    expired_users = users_col.count_documents({"is_active": False})
    expiring_soon = users_col.count_documents({
        "is_active": True,
        "expires_at": {"$lte": now + timedelta(days=3), "$gt": now}
    })
    
    total_leads = leads_col.count_documents({})
    converted_leads = leads_col.count_documents({"converted": True})
    
    return {
        "subscribers": {
            "total": total_users,
            "active": active_users,
            "expired": expired_users,
            "expiring_in_3_days": expiring_soon
        },
        "leads": {
            "total": total_leads,
            "converted": converted_leads,
            "conversion_rate": f"{(converted_leads/total_leads*100):.1f}%" if total_leads > 0 else "0%"
        },
        "timestamp": now.isoformat()
    }

# ==============================================================================
# RUN
# ==============================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
