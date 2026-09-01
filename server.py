"""
server.py -- Jay Empire VIP Backend + Affiliate System
With Paystack Split Payments, Auto-Payouts, and Milestone Rewards
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
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

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
# COMMISSION CONFIG
# ==============================================================================
COMMISSION_FIRST_SALE = 50   # 50% on first payment
COMMISSION_RENEWAL = 35      # 35% on renewals
REFERRAL_MILESTONE = 10      # 10 active referrals = lifetime VIP notification

# ==============================================================================
# MONGODB
# ==============================================================================
def init_mongodb():
    if not MONGO_URI:
        logger.error("MONGO_URI is not set!")
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
        logger.info("MongoDB connected successfully")

        db = client.get_default_database()

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
        logger.error(f"MongoDB connection failed: {e}")
        return None, None, None, None, None, None

mongo_client, db, users_col, leads_col, affiliates_col, referrals_col = init_mongodb()

# ==============================================================================
# PAYSTACK HELPERS
# ==============================================================================
def get_paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}

async def create_paystack_subaccount(business_name, bank_code, account_number, percentage):
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
user_states = {}  # {telegram_id: {"step": "...", "data": {...}}}

async def start_cmd(update: Update, context):
    user = update.effective_user
    if not user:
        return

    chat_id = user.id
    username = user.username or ""
    text = update.message.text or ""

    logger.info(f"/start from {chat_id} (@{username})")

    # Extract ref code from deep link
    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            ref_code = payload.replace("ref_", "")
            user_states[chat_id] = {"referred_by": ref_code}
            logger.info(f"Referral detected: {ref_code} for user {chat_id}")

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

    # Check if already affiliate
    # FIX: Use explicit None comparison instead of truthiness
    is_affiliate = None
    if affiliates_col is not None:
        is_affiliate = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True})

    kb = [[InlineKeyboardButton("Launch VIP Terminal App", web_app=WebAppInfo(url=MINI_APP_URL))]]

    if is_affiliate is None:
        kb.append([InlineKeyboardButton("Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("My Affiliate Dashboard", callback_data="affiliate_dashboard")])
        # Check milestone
        active_refs = 0
        if referrals_col is not None:
            active_refs = referrals_col.count_documents({
                "affiliate_id": is_affiliate["_id"],
                "is_active": True
            })
        if active_refs >= REFERRAL_MILESTONE and not is_affiliate.get("milestone_notified"):
            await notify_milestone(update, is_affiliate, active_refs)

    welcome_text = "<b>Welcome to Jay Empire VIP Terminal</b>\n\nTap below to launch the VIP Mini App:"
    if ref_code:
        welcome_text += f"\n\n<i>You were referred by affiliate: <code>{ref_code}</code></i>"

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def notify_milestone(update, affiliate, count):
    try:
        await update.message.reply_text(
            f"Congratulations! You have referred {count} active members!\n\n"
            f"You have unlocked Lifetime VIP Access!\n\n"
            f"Contact @{ADMIN_USERNAME} to claim your reward. "
            f"Show this message as proof.\n\n"
            f"Your code: <code>{affiliate['ref_code']}</code>",
            parse_mode="HTML"
        )
        if affiliates_col is not None:
            affiliates_col.update_one(
                {"_id": affiliate["_id"]},
                {"$set": {"milestone_notified": True, "milestone_reached_at": datetime.utcnow()}}
            )
        logger.info(f"Milestone notified for affiliate: {affiliate['ref_code']}")
    except Exception as e:
        logger.error(f"Milestone notify error: {e}")

# FIX: Added CallbackQueryHandler so inline buttons actually work
async def callback_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    action = query.data
    username = query.from_user.username or ""

    await handle_affiliate_callback(chat_id, action, username)

telegram_app.add_handler(CommandHandler("start", start_cmd))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))

# ==============================================================================
# AFFILIATE CALLBACKS
# ==============================================================================
async def handle_affiliate_callback(chat_id, action, username=""):
    bot = Bot(token=BOT_TOKEN)

    if action == "affiliate_start":
        kb = [
            [InlineKeyboardButton("I Agree and Join", callback_data="affiliate_agree")],
            [InlineKeyboardButton("Back", callback_data="back_main")]
        ]
        terms = (
            f"<b>Jay Empire Affiliate Program</b>\n\n"
            f"<b>Commissions:</b>\n"
            f"- First Sale: {COMMISSION_FIRST_SALE}%\n"
            f"- Renewals: {COMMISSION_RENEWAL}%\n"
            f"- Lifetime tracking\n\n"
            f"<b>Payout:</b> Automatic via Paystack Split. 1-2 days to bank.\n\n"
            f"<b>Bonus:</b> {REFERRAL_MILESTONE}+ active referrals = Lifetime VIP!\n\n"
            f"<b>Rules:</b> No fake signups, no self-referrals, no spam.\n\n"
            f"Click 'I Agree and Join' to accept terms and create your Paystack subaccount."
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "affiliate_agree":
        user_states[chat_id] = {"step": "awaiting_full_name", "data": {}}
        await bot.send_message(
            chat_id=chat_id,
            text="Step 1/4: Enter your Full Name (as on bank account):",
            parse_mode="HTML"
        )

    elif action == "affiliate_dashboard":
        # FIX: Explicit None comparison
        aff = None
        if affiliates_col is not None:
            aff = affiliates_col.find_one({"telegram_id": chat_id})
        if aff is not None:
            ref_link = f"https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}"
            total_refs = 0
            active_refs = 0
            if referrals_col is not None:
                total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]})
                active_refs = referrals_col.count_documents({"affiliate_id": aff["_id"], "is_active": True})

            milestone = "UNLOCKED!" if active_refs >= REFERRAL_MILESTONE else f"({active_refs}/{REFERRAL_MILESTONE})"

            dashboard = (
                f"<b>Your Affiliate Dashboard</b>\n\n"
                f"{ref_link}\n\n"
                f"Earnings: ${aff.get('total_earnings', 0):,.2f}\n"
                f"Total: {total_refs} | Active: {active_refs}\n"
                f"First: {COMMISSION_FIRST_SALE}% | Renewal: {COMMISSION_RENEWAL}%\n"
                f"Milestone: {milestone}\n\n"
                f"Share your link! Commissions are automatic."
            )
            kb = [
                [InlineKeyboardButton("Copy Link", callback_data=f"aff_copy:{aff['ref_code']}")],
                [InlineKeyboardButton("Bank Info", callback_data="affiliate_bank_info")],
                [InlineKeyboardButton("Back", callback_data="back_main")]
            ]
            await bot.send_message(chat_id=chat_id, text=dashboard, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action.startswith("aff_copy:"):
        ref_code = action.split(":")[1]
        link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
        await bot.send_message(
            chat_id=chat_id,
            text=f"Your Link:\n\n<code>{link}</code>\n\nTap and hold to copy!",
            parse_mode="HTML"
        )

    elif action == "affiliate_bank_info":
        # FIX: Explicit None comparison
        aff = None
        if affiliates_col is not None:
            aff = affiliates_col.find_one({"telegram_id": chat_id})
        if aff is not None:
            b = aff.get("bank_details", {})
            await bot.send_message(
                chat_id=chat_id,
                text=f"Payout Details\n\nBank: {b.get('bank_name','N/A')}\nAccount: ****{b.get('account_number','0000')[-4:]}\nName: {b.get('account_name','N/A')}\n\nAutomatic via Paystack.",
                parse_mode="HTML"
            )

    elif action == "back_main":
        # FIX: Explicit None comparison
        is_aff = None
        if affiliates_col is not None:
            is_aff = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True})
        kb = [[InlineKeyboardButton("Launch VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]]
        if is_aff is None:
            kb.append([InlineKeyboardButton("Become an Affiliate", callback_data="affiliate_start")])
        else:
            kb.append([InlineKeyboardButton("My Affiliate Dashboard", callback_data="affiliate_dashboard")])
        await bot.send_message(chat_id=chat_id, text="<b>Jay Empire Main Menu:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ==============================================================================
# SUBSCRIPTION MANAGEMENT
# ==============================================================================
async def kick_from_channel(user_id: int, channel_id: str, channel_type: str):
    bot = Bot(token=BOT_TOKEN)
    name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    try:
        await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.send_message(
            chat_id=user_id,
            text=f"Your {name} access has expired.\n\nRenew via the VIP Terminal.",
            parse_mode="HTML"
        )
        logger.info(f"Kicked {user_id} from {channel_type}")
        return True
    except Exception as e:
        logger.error(f"Kick failed {user_id}: {e}")
        return False

async def send_reminder(user_id: int, channel_type: str, days_left: int):
    bot = Bot(token=BOT_TOKEN)
    name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    try:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Renew Now", web_app=WebAppInfo(url=MINI_APP_URL))]])
        await bot.send_message(
            chat_id=user_id,
            text=f"{name} expires in {days_left} day(s).\nRenew now to avoid removal.",
            parse_mode="HTML",
            reply_markup=kb
        )
        logger.info(f"Reminder sent to {user_id}")
        return True
    except Exception as e:
        logger.error(f"Reminder failed {user_id}: {e}")
        return False

# ==============================================================================
# DAILY CHECKS
# ==============================================================================
async def run_daily_checks():
    # FIX: Explicit None comparison instead of truthiness
    if users_col is None or leads_col is None:
        logger.error("DB not available")
        return {"error": "DB not connected"}

    now = datetime.utcnow()
    results = {"reminders_sent": 0, "users_kicked": 0, "leads_followed": 0, "errors": []}

    # 1. Follow up unconverted leads (48h+)
    try:
        cutoff = now - timedelta(hours=48)
        unconverted = leads_col.find({
            "converted": False,
            "followup_sent": False,
            "started_at": {"$lte": cutoff}
        })
        for lead in unconverted:
            try:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("Enter VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]])
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=lead["telegram_id"],
                    text="Jay Empire VIP Market Alert\n\nHigh-precision trade setups are active now. Tap below:",
                    parse_mode="HTML",
                    reply_markup=kb
                )
                leads_col.update_one({"_id": lead["_id"]}, {"$set": {"followup_sent": True}})
                results["leads_followed"] += 1
            except Exception as e:
                results["errors"].append(f"lead_{lead['telegram_id']}: {str(e)}")
    except Exception as e:
        results["errors"].append(f"leads: {str(e)}")

    # 2. Reminders
    try:
        target = now + timedelta(days=3)
        expiring = users_col.find({
            "is_active": True,
            "reminder_sent": False,
            "expires_at": {"$lte": target, "$gt": now}
        })
        for user in expiring:
            days_left = max((user["expires_at"] - now).days, 1)
            if await send_reminder(user["telegram_id"], user["channel_type"], days_left):
                users_col.update_one({"_id": user["_id"]}, {"$set": {"reminder_sent": True}})
                results["reminders_sent"] += 1
    except Exception as e:
        results["errors"].append(f"reminders: {str(e)}")

    # 3. Kick expired
    try:
        expired = users_col.find({"is_active": True, "expires_at": {"$lte": now}})
        for user in expired:
            cid = GOLD_CHANNEL_ID if user["channel_type"] == "gold" else FOREX_CHANNEL_ID
            if await kick_from_channel(user["telegram_id"], cid, user["channel_type"]):
                users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"is_active": False, "kicked_at": now}}
                )
                results["users_kicked"] += 1
    except Exception as e:
        results["errors"].append(f"expired: {str(e)}")

    logger.info(f"Daily check: {results}")
    return results

async def scheduler_loop():
    while True:
        await asyncio.sleep(86400)
        await run_daily_checks()

# ==============================================================================
# LIFESPAN
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(url=webhook_target)
    logger.info(f"Webhook set: {webhook_target}")

    asyncio.create_task(scheduler_loop())
    yield
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# ENDPOINTS
# ==============================================================================

@app.get("/")
async def health():
    return {
        "status": "active",
        # FIX: Explicit None comparison
        "mongodb": "connected" if db is not None else "disconnected",
        "service": "Jay Empire VIP + Affiliate",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health/db")
async def health_db():
    # FIX: Explicit None comparison
    if db is None:
        return JSONResponse({"status": "unhealthy"}, status_code=503)
    try:
        db.command("ping")
        return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=503)

@app.post("/cron/daily-check")
async def cron_daily():
    return JSONResponse({
        "status": "completed",
        "results": await run_daily_checks(),
        "timestamp": datetime.utcnow().isoformat()
    })

# ==============================================================================
# TELEGRAM WEBHOOK
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # Callback queries (affiliate buttons)
    if "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        username = query["from"].get("username", "")

        try:
            await Bot(token=BOT_TOKEN).answer_callback_query(callback_query_id=query["id"])
        except:
            pass

        # Handle bank selection during registration
        if action.startswith("aff_bank:"):
            parts = action.split(":", 2)
            if len(parts) == 3:
                _, bank_code, bank_name = parts
                if chat_id in user_states:
                    user_states[chat_id]["data"]["bank_code"] = bank_code
                    user_states[chat_id]["data"]["bank_name"] = bank_name
                    user_states[chat_id]["step"] = "awaiting_account_number"
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=chat_id,
                    text=f"Step 3/4: Enter your Account Number for {bank_name}:",
                    parse_mode="HTML"
                )
            return {"status": "ok"}

        # Handle confirmation
        if action == "affiliate_confirm":
            if chat_id not in user_states:
                return {"status": "ok"}
            state = user_states[chat_id]
            d = state["data"]

            subaccount = await create_paystack_subaccount(
                d["full_name"], d["bank_code"], d["account_number"], COMMISSION_FIRST_SALE
            )
            if subaccount is None:
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=chat_id,
                    text="Failed to create payout account. Contact @jay_empire247."
                )
                return {"status": "ok"}

            ref_code = generate_ref_code()
            aff_doc = {
                "telegram_id": chat_id,
                "username": username,
                "full_name": d["full_name"],
                "ref_code": ref_code,
                "paystack_subaccount": subaccount,
                "bank_details": {
                    "bank_code": d["bank_code"],
                    "bank_name": d["bank_name"],
                    "account_number": d["account_number"],
                    "account_name": d.get("account_name", d["full_name"])
                },
                "commission_rates": {"first_sale": COMMISSION_FIRST_SALE, "renewal": COMMISSION_RENEWAL},
                "total_earnings": 0,
                "total_referrals": 0,
                "is_active": True,
                "milestone_notified": False,
                "created_at": datetime.utcnow()
            }

            if affiliates_col is not None:
                affiliates_col.insert_one(aff_doc)

            ref_link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
            await Bot(token=BOT_TOKEN).send_message(
                chat_id=chat_id,
                text=(
                    f"Welcome to the Affiliate Program!\n\n"
                    f"Your Link:\n{ref_link}\n\n"
                    f"Commissions: {COMMISSION_FIRST_SALE}% first | {COMMISSION_RENEWAL}% renewal\n"
                    f"Bonus: {REFERRAL_MILESTONE}+ refs = Lifetime VIP\n"
                    f"Payouts: Auto to {d['bank_name']}\n\n"
                    f"Start sharing now!"
                ),
                parse_mode="HTML"
            )
            del user_states[chat_id]
            return {"status": "ok"}

        await handle_affiliate_callback(chat_id, action, username)
        return {"status": "ok"}

    # Text messages (affiliate registration flow)
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]

        if text.startswith("/"):
            # FIX: Process commands through the dispatcher properly
            update = Update.de_json(data, telegram_app.bot)
            await telegram_app.process_update(update)
            return {"status": "ok"}

        if chat_id in user_states:
            state = user_states[chat_id]
            step = state.get("step")
            bot = Bot(token=BOT_TOKEN)

            if step == "awaiting_full_name":
                state["data"]["full_name"] = text
                state["step"] = "awaiting_bank_selection"
                banks = await get_paystack_bank_list()
                kb = []
                for b in banks[:20]:
                    kb.append([InlineKeyboardButton(b["name"], callback_data=f"aff_bank:{b['code']}:{b['name']}")])
                kb.append([InlineKeyboardButton("Cancel", callback_data="back_main")])
                await bot.send_message(
                    chat_id=chat_id,
                    text="Step 2/4: Select Your Bank",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return {"status": "ok"}

            elif step == "awaiting_account_number":
                state["data"]["account_number"] = text
                state["step"] = "awaiting_confirmation"
                acc_name = await verify_bank_account(text, state["data"]["bank_code"])
                if acc_name is not None:
                    state["data"]["account_name"] = acc_name
                    kb = [
                        [InlineKeyboardButton("Confirm and Create", callback_data="affiliate_confirm")],
                        [InlineKeyboardButton("Start Over", callback_data="affiliate_agree")]
                    ]
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"Confirm:\n\nName: {state['data']['full_name']}\nBank: {state['data']['bank_name']}\nAccount: {text}\nVerified: {acc_name}\n\nClick confirm to start earning!",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                else:
                    await bot.send_message(chat_id=chat_id, text="Could not verify. Check and try again.")
                    state["step"] = "awaiting_account_number"
                return {"status": "ok"}

    # Fallback: process any other updates through python-telegram-bot dispatcher
    # FIX: This ensures all updates (including commands) are properly handled
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

# ==============================================================================
# PAYSTACK WEBHOOK (SECURED)
# ==============================================================================
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    if not PAYSTACK_SECRET:
        raise HTTPException(status_code=500, detail="Paystack secret not set")

    body = await request.body()
    expected = hmac.new(PAYSTACK_SECRET.encode(), body, hashlib.sha512).hexdigest()

    if x_paystack_signature is None or not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
        logger.info(f"Webhook: {payload.get('event')}")

        if payload.get("event") == "charge.success":
            data = payload["data"]
            meta = data.get("metadata", {})

            tg_id = meta.get("telegram_id")
            channel_type = meta.get("channel_type", "gold")
            days = int(meta.get("days", 30))
            reference = data.get("reference", "unknown")
            ref_code = meta.get("ref_code")
            is_renewal = meta.get("is_renewal", False)

            if not tg_id or tg_id == 0:
                return {"status": "ignored"}

            now = datetime.utcnow()
            expires = now + timedelta(days=days)

            if users_col is not None:
                # Upsert user
                users_col.update_one(
                    {"telegram_id": tg_id, "channel_type": channel_type},
                    {
                        "$set": {
                            "telegram_id": tg_id,
                            "channel_type": channel_type,
                            "purchased_at": now,
                            "expires_at": expires,
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

                # Mark lead converted
                if leads_col is not None:
                    leads_col.update_one(
                        {"telegram_id": tg_id},
                        {"$set": {"converted": True, "converted_at": now, "converted_channel": channel_type}}
                    )

                # AFFILIATE TRACKING
                if ref_code and affiliates_col is not None and referrals_col is not None:
                    affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
                    if affiliate is not None:
                        rate = COMMISSION_RENEWAL if is_renewal else COMMISSION_FIRST_SALE
                        amount = data.get("amount", 0)
                        commission = int(amount * rate / 100)

                        referrals_col.update_one(
                            {"affiliate_id": affiliate["_id"], "customer_telegram_id": tg_id},
                            {
                                "$setOnInsert": {
                                    "affiliate_id": affiliate["_id"],
                                    "ref_code": ref_code,
                                    "customer_telegram_id": tg_id,
                                    "customer_channel": channel_type,
                                    "plan_key": meta.get("plan_key", "unknown"),
                                    "created_at": now,
                                    "is_active": True
                                },
                                "$set": {
                                    "last_payment": {
                                        "amount": amount,
                                        "currency": data.get("currency"),
                                        "commission_paid": commission,
                                        "commission_rate": rate,
                                        "paystack_reference": reference,
                                        "paid_at": now,
                                        "is_renewal": is_renewal
                                    }
                                },
                                "$inc": {"total_payments": 1}
                            },
                            upsert=True
                        )

                        affiliates_col.update_one(
                            {"_id": affiliate["_id"]},
                            {
                                "$inc": {"total_earnings": commission / 100, "total_referrals": 0 if is_renewal else 1},
                                "$set": {"last_earning_at": now}
                            }
                        )

                        logger.info(f"Affiliate {ref_code} earned {rate}% = {commission} from {tg_id}")

            # Send access link
            bot = Bot(token=BOT_TOKEN)
            try:
                link = GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK
                name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"Enter {name}", url=link)]])
                await bot.send_message(
                    chat_id=tg_id,
                    text=f"PAYMENT VERIFIED!\n\nPlan: {channel_type.upper()}\nDuration: {days} days\nExpires: {expires.strftime('%B %d, %Y')}\n\nTap below:",
                    parse_mode="HTML",
                    reply_markup=btn
                )
            except Exception as e:
                logger.error(f"Access message failed: {e}")

        return {"status": "success"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ==============================================================================
# ADMIN ENDPOINTS
# ==============================================================================

@app.get("/admin/users")
async def admin_users():
    # FIX: Explicit None comparison
    if users_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)
    users = list(users_col.find({}, {"_id": 0}))
    return {
        "total": len(users),
        "active": sum(1 for u in users if u.get("is_active")),
        "expired": sum(1 for u in users if not u.get("is_active")),
        "users": users
    }

@app.get("/admin/leads")
async def admin_leads():
    # FIX: Explicit None comparison
    if leads_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)
    leads = list(leads_col.find({}, {"_id": 0}))
    return {
        "total": len(leads),
        "converted": sum(1 for l in leads if l.get("converted")),
        "unconverted": sum(1 for l in leads if not l.get("converted")),
        "leads": leads
    }

@app.get("/admin/affiliates")
async def admin_affiliates():
    # FIX: Explicit None comparison
    if affiliates_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)
    affs = list(affiliates_col.find({}, {"_id": 0, "bank_details": 0}))
    return {
        "total": len(affs),
        "active": sum(1 for a in affs if a.get("is_active")),
        "milestone_reached": sum(1 for a in affs if a.get("milestone_notified")),
        "affiliates": affs
    }

@app.get("/admin/dashboard")
async def admin_dashboard():
    # FIX: Explicit None comparison for all collections
    if users_col is None or leads_col is None or affiliates_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)

    now = datetime.utcnow()
    total_earnings = sum(a.get("total_earnings", 0) for a in affiliates_col.find())

    return {
        "subscribers": {
            "total": users_col.count_documents({}),
            "active": users_col.count_documents({"is_active": True}),
            "expired": users_col.count_documents({"is_active": False}),
            "expiring_3d": users_col.count_documents({
                "is_active": True,
                "expires_at": {"$lte": now + timedelta(days=3), "$gt": now}
            })
        },
        "leads": {
            "total": leads_col.count_documents({}),
            "converted": leads_col.count_documents({"converted": True})
        },
        "affiliates": {
            "total": affiliates_col.count_documents({"is_active": True}),
            "milestone_reached": affiliates_col.count_documents({"milestone_notified": True}),
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
