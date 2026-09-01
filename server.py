"""
server.py — Jay Empire VIP Backend
With FULL Affiliate System + Paystack Split Payments
"""

import os
import asyncio
import logging
import ssl
import certifi
import hmac
import hashlib
import httpx
import secrets
import string
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Header, HTTPException
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
# ENVIRONMENT
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
MONGO_URI = os.getenv("MONGO_URI", "").strip().strip('"').strip("'")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-paystack-bot-415x.onrender.com")

GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID", "-1004329655598")
FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID", "-1004451754852")
ADMIN_USERNAME = "jay_empire247"
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

GOLD_PRIMARY_LINK = "https://t.me/+env-Zrui2ykwYjg8"
FOREX_PRIMARY_LINK = "https://t.me/+njii3OAHlqI3MjQ8"

# ==============================================================================
# COMMISSION RATES
# ==============================================================================
COMMISSION_FIRST_SALE = 50   # 50% on first payment
COMMISSION_RENEWAL = 35      # 35% on renewals
REFERRAL_MILESTONE = 10      # 10 referrals = lifetime VIP notification

# ==============================================================================
# MONGODB
# ==============================================================================
def init_mongodb():
    if not MONGO_URI:
        logger.error("❌ MONGO_URI not set!")
        return None, None, None, None, None, None

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
        logger.info("✅ MongoDB connected")

        db = client.get_default_database()
        
        # Collections
        users_col = db["vip_users"]
        leads_col = db["leads"]
        affiliates_col = db["affiliates"]
        referrals_col = db["referrals"]
        
        # Indexes
        users_col.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
        users_col.create_index([("expires_at", ASCENDING)])
        users_col.create_index([("is_active", ASCENDING)])
        users_col.create_index([("referred_by", ASCENDING)])
        
        leads_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("ref_code", ASCENDING)], unique=True)
        referrals_col.create_index([("affiliate_id", ASCENDING)])
        referrals_col.create_index([("customer_telegram_id", ASCENDING)])
        
        return client, db, users_col, leads_col, affiliates_col, referrals_col

    except Exception as e:
        logger.error(f"❌ MongoDB failed: {e}")
        return None, None, None, None, None, None

mongo_client, db, users_col, leads_col, affiliates_col, referrals_col = init_mongodb()

# ==============================================================================
# PAYSTACK HELPERS
# ==============================================================================
def get_paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}

async def create_paystack_subaccount(business_name, bank_code, account_number, percentage):
    """Create Paystack subaccount for affiliate auto-payouts"""
    payload = {
        "business_name": business_name,
        "settlement_bank": bank_code,
        "account_number": account_number,
        "percentage_charge": percentage,
        "description": f"Jay Empire Affiliate - {business_name}"
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/subaccount",
            json=payload,
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()
        if data.get("status"):
            return data["data"]["subaccount_code"]
        logger.error(f"Subaccount creation failed: {data}")
        return None

async def get_paystack_bank_list(country="ghana"):
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"https://api.paystack.co/bank?country={country}",
            headers=get_paystack_headers(),
            timeout=10.0
        )
        data = res.json()
        return data.get("data", []) if data.get("status") else []

async def verify_bank_account(account_number, bank_code):
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"https://api.paystack.co/bank/resolve?account_number={account_number}&bank_code={bank_code}",
            headers=get_paystack_headers(),
            timeout=10.0
        )
        data = res.json()
        return data["data"]["account_name"] if data.get("status") else None

def generate_ref_code():
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
    return f"JAY{suffix}"

# ==============================================================================
# TELEGRAM BOT
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()

# In-memory state for multi-step flows
user_states = {}

async def start_cmd(update: Update, context):
    """Handle /start with referral detection"""
    user = update.effective_user
    if not user:
        return

    chat_id = user.id
    username = user.username or ""
    text = update.message.text or ""
    
    logger.info(f"🤖 /start from {chat_id} (@{username}) — text: {text}")

    # Extract ref code from deep link: /start ref_XXXXX
    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            ref_code = payload.replace("ref_", "")
            user_states[chat_id] = {"referred_by": ref_code}
            logger.info(f"🔗 Referral detected: {ref_code} for user {chat_id}")

    # Log lead
    if leads_col is not None:
        try:
            leads_col.update_one(
                {"telegram_id": chat_id},
                {
                    "$setOnInsert": {
                        "telegram_id": chat_id,
                        "first_name": user.first_name,
                        "username": username,
                        "started_at": datetime.utcnow(),
                        "converted": False,
                        "followup_sent": False,
                        "referred_by": ref_code
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"Lead logging error: {e}")

    # Check if user is already an affiliate
    is_affiliate = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True}) if affiliates_col else None

    # Build keyboard
    kb = [
        [InlineKeyboardButton("👑 Launch VIP Terminal App", web_app=WebAppInfo(url=MINI_APP_URL))]
    ]
    
    if not is_affiliate:
        kb.append([InlineKeyboardButton("💰 Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
        # Check milestone
        active_refs = referrals_col.count_documents({
            "affiliate_id": is_affiliate["_id"],
            "is_active": True
        }) if referrals_col else 0
        if active_refs >= REFERRAL_MILESTONE and not is_affiliate.get("milestone_notified"):
            await notify_milestone(update, is_affiliate, active_refs)

    welcome_text = (
        "<b>Welcome to Jay Empire VIP Terminal 👑</b>\n\n"
        "Tap below to launch the VIP Mini App and subscribe."
    )
    if ref_code:
        welcome_text += f"\n\n<i>👥 You were referred by affiliate: <code>{ref_code}</code></i>"

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def notify_milestone(update, affiliate, count):
    """Notify affiliate they hit 10+ referrals"""
    try:
        await update.message.reply_text(
            f"🎉 <b>CONGRATULATIONS!</b> 🎉\n\n"
            f"You've successfully referred <b>{count}</b> active members to Jay Empire!\n\n"
            f"🏆 <b>You've unlocked Lifetime VIP Access!</b>\n\n"
            f"Please contact <b>@{ADMIN_USERNAME}</b> directly to claim your reward. "
            f"Show this message as proof.\n\n"
            f"<i>Your referral code: <code>{affiliate['ref_code']}</code></i>",
            parse_mode="HTML"
        )
        affiliates_col.update_one(
            {"_id": affiliate["_id"]},
            {"$set": {"milestone_notified": True, "milestone_reached_at": datetime.utcnow()}}
        )
        logger.info(f"🏆 Milestone notified for affiliate {affiliate['ref_code']}")
    except Exception as e:
        logger.error(f"Milestone notify error: {e}")

telegram_app.add_handler(CommandHandler("start", start_cmd))

# ==============================================================================
# AFFILIATE CALLBACK HANDLERS (via webhook)
# ==============================================================================
async def handle_affiliate_callback(chat_id, action, username=""):
    """Process affiliate-related callback queries"""
    bot = Bot(token=BOT_TOKEN)
    
    if action == "affiliate_start":
        kb = [
            [InlineKeyboardButton("✅ I Agree & Join", callback_data="affiliate_agree")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ]
        terms = (
            "<b>🤝 JAY EMPIRE AFFILIATE PROGRAM</b>\n\n"
            f"<b>Commission Structure:</b>\n"
            f"• <b>First Sale:</b> {COMMISSION_FIRST_SALE}% of every new subscriber\n"
            f"• <b>Renewals:</b> {COMMISSION_RENEWAL}% of every renewal payment\n"
            f"• <b>Lifetime tracking:</b> Earn as long as referrals stay subscribed\n\n"
            f"<b>Payout:</b> Automatic via Paystack Split. Money lands in your bank account in 1-2 days.\n\n"
            f"<b>🏆 Bonus:</b> Refer {REFERRAL_MILESTONE}+ active members and get <b>Lifetime VIP Access</b>!\n\n"
            f"<b>Rules:</b> No fake signups, no self-referrals, no spam. Violation = permanent ban.\n\n"
            f"<i>By clicking 'I Agree & Join', you accept these terms and authorize Paystack subaccount creation.</i>"
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    
    elif action == "affiliate_agree":
        user_states[chat_id] = {"step": "awaiting_full_name", "data": {}}
        await bot.send_message(
            chat_id=chat_id,
            text="📝 <b>Step 1/4:</b> Enter your <b>Full Name</b> (as it appears on your bank account):",
            parse_mode="HTML"
        )
    
    elif action == "affiliate_dashboard":
        aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
        if aff:
            ref_link = f"https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}"
            total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]}) if referrals_col else 0
            active_refs = referrals_col.count_documents({"affiliate_id": aff["_id"], "is_active": True}) if referrals_col else 0
            
            milestone_status = "🏆 UNLOCKED!" if active_refs >= REFERRAL_MILESTONE else f"({active_refs}/{REFERRAL_MILESTONE} to unlock)"
            
            dashboard = (
                f"<b>📊 Your Affiliate Dashboard</b>\n\n"
                f"🔗 <code>{ref_link}</code>\n\n"
                f"💰 Total Earnings: <b>${aff.get('total_earnings', 0):,.2f}</b>\n"
                f"👥 Total Referrals: <b>{total_refs}</b>\n"
                f"✅ Active Subscribers: <b>{active_refs}</b>\n"
                f"📈 First Sale: <b>{COMMISSION_FIRST_SALE}%</b> | 🔄 Renewal: <b>{COMMISSION_RENEWAL}%</b>\n"
                f"🏆 Lifetime VIP Bonus: <b>{milestone_status}</b>\n\n"
                f"<i>Share your link anywhere! Commissions are automatic.</i>"
            )
            kb = [
                [InlineKeyboardButton("📋 Copy My Link", callback_data=f"aff_copy:{aff['ref_code']}")],
                [InlineKeyboardButton("🏦 Bank Info", callback_data="affiliate_bank_info")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
            ]
            await bot.send_message(chat_id=chat_id, text=dashboard, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    
    elif action.startswith("aff_copy:"):
        ref_code = action.split(":")[1]
        link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
        await bot.send_message(
            chat_id=chat_id,
            text=f"<b>🔗 Your Referral Link:</b>\n\n<code>{link}</code>\n\n<i>Tap and hold to copy, then share!</i>",
            parse_mode="HTML"
        )
    
    elif action == "affiliate_bank_info":
        aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
        if aff:
            b = aff.get("bank_details", {})
            await bot.send_message(
                chat_id=chat_id,
                text=f"<b>🏦 Payout Details</b>\n\nBank: <b>{b.get('bank_name','N/A')}</b>\nAccount: <b>****{b.get('account_number','0000')[-4:]}</b>\nName: <b>{b.get('account_name','N/A')}</b>\n\n<i>Automatic payouts via Paystack. No action needed.</i>",
                parse_mode="HTML"
            )
    
    elif action == "back_main":
        is_aff = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True}) if affiliates_col else None
        kb = [[InlineKeyboardButton("👑 Launch VIP Terminal App", web_app=WebAppInfo(url=MINI_APP_URL))]]
        if not is_aff:
            kb.append([InlineKeyboardButton("💰 Become an Affiliate", callback_data="affiliate_start")])
        else:
            kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
        await bot.send_message(chat_id=chat_id, text="<b>Jay Empire Main Menu:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ==============================================================================
# SUBSCRIPTION MANAGEMENT
# ==============================================================================
async def kick_from_channel(user_id: int, channel_id: str, channel_type: str):
    bot = Bot(token=BOT_TOKEN)
    channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    
    try:
        await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.send_message(
            chat_id=user_id,
            text=f"⚠️ <b>Your {channel_name} access has expired.</b>\n\nRenew via the VIP Terminal to regain access.",
            parse_mode="HTML"
        )
        logger.info(f"✅ Kicked user {user_id} from {channel_type}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to kick {user_id}: {e}")
        return False

async def send_reminder(user_id: int, channel_type: str, days_left: int):
    bot = Bot(token=BOT_TOKEN)
    channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    
    try:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Renew Now", web_app=WebAppInfo(url=MINI_APP_URL))]])
        await bot.send_message(
            chat_id=user_id,
            text=f"⏰ <b>{channel_name} Renewal Reminder</b>\n\nYour access expires in <b>{days_left} day(s)</b>.\nRenew now to avoid automatic removal.",
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
    if users_col is None or leads_col is None:
        logger.error("Database not available")
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
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Enter VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]])
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=lead["telegram_id"],
                    text="👑 <b>Jay Empire VIP Market Alert</b>\n\nHigh-precision trade setups are active now. Don't miss the next wave.\n\nTap below to lock in your membership:",
                    parse_mode="HTML",
                    reply_markup=kb
                )
                leads_col.update_one({"_id": lead["_id"]}, {"$set": {"followup_sent": True}})
                results["leads_followed"] += 1
            except Exception as e:
                logger.error(f"Lead follow-up error: {e}")
                results["errors"].append(f"lead_{lead['telegram_id']}: {str(e)}")
    except Exception as e:
        logger.error(f"DB error (leads): {e}")

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
                users_col.update_one({"_id": user["_id"]}, {"$set": {"reminder_sent": True}})
                results["reminders_sent"] += 1
    except Exception as e:
        logger.error(f"DB error (reminders): {e}")

    # 3. Kick expired users
    try:
        expired = users_col.find({"is_active": True, "expires_at": {"$lte": now}})
        
        for user in expired:
            channel_id = GOLD_CHANNEL_ID if user["channel_type"] == "gold" else FOREX_CHANNEL_ID
            if await kick_from_channel(user["telegram_id"], channel_id, user["channel_type"]):
                users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"is_active": False, "kicked_at": now}}
                )
                results["users_kicked"] += 1
    except Exception as e:
        logger.error(f"DB error (expired): {e}")

    logger.info(f"📊 Daily check: {results}")
    return results

async def scheduler_loop():
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
    db_status = "connected" if db is not None else "disconnected"
    return {
        "status": "active",
        "mongodb": db_status,
        "service": "Jay Empire VIP + Affiliate System",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health/db")
async def health_db():
    if db is None:
        return JSONResponse({"status": "unhealthy", "mongodb": "not_initialized"}, status_code=503)
    try:
        db.command("ping")
        return {"status": "healthy", "mongodb": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "mongodb": str(e)}, status_code=503)

@app.post("/cron/daily-check")
async def cron_daily_check():
    results = await run_daily_checks()
    return JSONResponse({"status": "completed", "results": results, "timestamp": datetime.utcnow().isoformat()})

# ==============================================================================
# TELEGRAM WEBHOOK
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    # Handle callback queries (affiliate buttons, etc.)
    if "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        username = query["from"].get("username", "")
        
        try:
            await Bot(token=BOT_TOKEN).answer_callback_query(callback_query_id=query["id"])
        except:
            pass
        
        await handle_affiliate_callback(chat_id, action, username)
        return {"status": "ok"}
    
    # Handle text messages (affiliate registration flow)
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        username = data["message"]["from"].get("username", "")
        
        # Skip commands
        if text.startswith("/"):
            update = Update.de_json(data, telegram_app.bot)
            await telegram_app.process_update(update)
            return {"status": "ok"}
        
        # Handle affiliate registration steps
        if chat_id in user_states:
            state = user_states[chat_id]
            step = state.get("step")
            bot = Bot(token=BOT_TOKEN)
            
            if step == "awaiting_full_name":
                state["data"]["full_name"] = text
                state["step"] = "awaiting_bank_selection"
                banks = await get_paystack_bank_list()
                kb = [[InlineKeyboardButton(b["name"], callback_data=f"aff_bank:{b['code']}:{b['name']}")] for b in banks[:20]]
                kb.append([InlineKeyboardButton("⬅️ Cancel", callback_data="back_main")])
                await bot.send_message(chat_id=chat_id, text="🏦 <b>Step 2/4: Select Your Bank</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
                return {"status": "ok"}
            
            elif step == "awaiting_account_number":
                state["data"]["account_number"] = text
                state["step"] = "awaiting_confirmation"
                acc_name = await verify_bank_account(text, state["data"]["bank_code"])
                if acc_name:
                    state["data"]["account_name"] = acc_name
                    kb = [
                        [InlineKeyboardButton("✅ Confirm & Create Account", callback_data="affiliate_confirm")],
                        [InlineKeyboardButton("❌ Start Over", callback_data="affiliate_agree")]
                    ]
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"<b>Confirm Details:</b>\n\nName: {state['data']['full_name']}\nBank: {state['data']['bank_name']}\nAccount: {text}\nVerified: {acc_name}\n\nClick confirm to start earning!",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                else:
                    await bot.send_message(chat_id=chat_id, text="❌ Could not verify account. Please check and try again.")
                    state["step"] = "awaiting_account_number"
                return {"status": "ok"}
    
    # Process via python-telegram-bot for commands
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

# ==============================================================================
# PAYSTACK WEBHOOK (SECURED)
# ==============================================================================
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    # Verify signature
    if not PAYSTACK_SECRET:
        raise HTTPException(status_code=500, detail="Paystack secret not configured")
    
    body = await request.body()
    expected_sig = hmac.new(PAYSTACK_SECRET.encode('utf-8'), body, hashlib.sha512).hexdigest()
    
    if not x_paystack_signature or not hmac.compare_digest(expected_sig, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    try:
        payload = await request.json()
        logger.info(f"💰 Paystack webhook: {payload.get('event')}")
        
        if payload.get("event") == "charge.success":
            data = payload["data"]
            metadata = data.get("metadata", {})
            
            tg_id = metadata.get("telegram_id")
            channel_type = metadata.get("channel_type", "gold")
            days = int(metadata.get("days", 30))
            reference = data.get("reference", "unknown")
            ref_code = metadata.get("ref_code")
            is_renewal = metadata.get("is_renewal", False)
            
            logger.info(f"💳 Payment: ref={reference}, user={tg_id}, channel={channel_type}, days={days}, ref={ref_code}")
            
            if not tg_id or tg_id == 0:
                logger.warning("⚠️ Missing telegram_id")
                return {"status": "ignored"}
            
            now = datetime.utcnow()
            expires_at = now + timedelta(days=days)
            
            if users_col is not None:
                try:
                    # Upsert VIP user
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
                                "last_reference": reference,
                                "amount_paid": data.get("amount"),
                                "currency": data.get("currency"),
                                "customer_email": data.get("customer", {}).get("email"),
                                "paystack_reference": reference,
                                "referred_by": ref_code
                            }
                        },
                        upsert=True
                    )
                    
                    # Mark lead as converted
                    leads_col.update_one(
                        {"telegram_id": tg_id},
                        {"$set": {"converted": True, "converted_at": now, "converted_channel": channel_type}}
                    )
                    
                    # ── AFFILIATE TRACKING ─────────────────────────────────
                    if ref_code and affiliates_col and referrals_col:
                        affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
                        if affiliate:
                            commission_rate = COMMISSION_RENEWAL if is_renewal else COMMISSION_FIRST_SALE
                            amount = data.get("amount", 0)
                            commission = int(amount * commission_rate / 100)
                            
                            # Upsert referral record
                            referrals_col.update_one(
                                {"affiliate_id": affiliate["_id"], "customer_telegram_id": tg_id},
                                {
                                    "$setOnInsert": {
                                        "affiliate_id": affiliate["_id"],
                                        "ref_code": ref_code,
                                        "customer_telegram_id": tg_id,
                                        "customer_channel": channel_type,
                                        "plan_key": metadata.get("plan_key", "unknown"),
                                        "created_at": now,
                                        "is_active": True
                                    },
                                    "$set": {
                                        "last_payment": {
                                            "amount": amount,
                                            "currency": data.get("currency"),
                                            "commission_paid": commission,
                                            "commission_rate": commission_rate,
                                            "paystack_reference": reference,
                                            "paid_at": now,
                                            "is_renewal": is_renewal
                                        }
                                    },
                                    "$inc": {"total_payments": 1}
                                },
                                upsert=True
                            )
                            
                            # Update affiliate earnings
                            affiliates_col.update_one(
                                {"_id": affiliate["_id"]},
                                {
                                    "$inc": {"total_earnings": commission / 100, "total_referrals": 1 if not is_renewal else 0},
                                    "$set": {"last_earning_at": now}
                                }
                            )
                            
                            logger.info(f"💰 Affiliate {ref_code} earned {commission_rate}% = {commission} from user {tg_id}")
                    
                except Exception as e:
                    logger.error(f"❌ DB update failed: {e}")
                    return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
            else:
                return JSONResponse({"status": "error", "detail": "Database offline"}, status_code=503)
            
            # Send access link
            bot = Bot(token=BOT_TOKEN)
            try:
                target_link = GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK
                channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
                
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Enter {channel_name}", url=target_link)]])
                await bot.send_message(
                    chat_id=tg_id,
                    text=f"🎉 <b>PAYMENT VERIFIED!</b>\n\nPlan: <b>{channel_type.upper()}</b>\nDuration: <b>{days} days</b>\nExpires: <b>{expires_at.strftime('%B %d, %Y')}</b>\n\nTap below to join:",
                    parse_mode="HTML",
                    reply_markup=btn
                )
                logger.info(f"✅ Access message sent to {tg_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send access to {tg_id}: {e}")

        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"❌ Paystack webhook error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ==============================================================================
# ADMIN ENDPOINTS
# ==============================================================================

@app.get("/admin/users")
async def get_all_users():
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
    if leads_col is None:
        return JSONResponse({"error": "Database not connected"}, status_code=503)
    leads = list(leads_col.find({}, {"_id": 0}))
    return {
        "total_leads": len(leads),
        "converted": sum(1 for l in leads if l.get("converted")),
        "unconverted": sum(1 for l in leads if not l.get("converted")),
        "leads": leads
    }

@app.get("/admin/affiliates")
async def get_all_affiliates():
    if affiliates_col is None:
        return JSONResponse({"error": "Database not connected"}, status_code=503)
    affs = list(affiliates_col.find({}, {"_id": 0, "bank_details": 0}))
    return {
        "total_affiliates": len(affs),
        "active": sum(1 for a in affs if a.get("is_active")),
        "milestone_reached": sum(1 for a in affs if a.get("milestone_notified")),
        "affiliates": affs
    }

@app.get("/admin/dashboard")
async def admin_dashboard():
    if users_col is None or leads_col is None or affiliates_col is None:
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
    total_affs = affiliates_col.count_documents({"is_active": True})
    milestone_affs = affiliates_col.count_documents({"milestone_notified": True})
    total_earnings = sum(a.get("total_earnings", 0) for a in affiliates_col.find())
    
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
        "affiliates": {
            "total": total_affs,
            "milestone_reached": milestone_affs,
            "total_payouts": round(total_earnings, 2)
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
