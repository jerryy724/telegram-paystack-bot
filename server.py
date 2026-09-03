"""
server.py -- Jay Empire Backend & Semi-Automated Affiliate System
Paystack Payments + Telegram Semi-Automated MoMo Payouts
"""

import os
import asyncio
import logging
import certifi
import hmac
import hashlib
import httpx
import secrets
import string
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
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

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# ==============================================================================
# PRICING & COMMISSIONS
# ==============================================================================
PLANS = [
    {"key": "test", "name": "Test Phase", "usd": 0.10, "days": 1, "original": None, "is_test": True},
    {"key": "1m", "name": "1 Month Access", "usd": 15, "days": 30, "original": 25, "is_test": False},
    {"key": "3m", "name": "3 Months Access", "usd": 40, "days": 90, "original": 60, "is_test": False},
    {"key": "6m", "name": "6 Months Access", "usd": 80, "days": 180, "original": 120, "is_test": False},
    {"key": "1y", "name": "1 Year Access", "usd": 150, "days": 365, "original": 250, "is_test": False},
    {"key": "lifetime", "name": "Lifetime VIP", "usd": 700, "days": 36500, "original": 1500, "is_test": False},
]
PLANS_BY_KEY = {p["key"]: p for p in PLANS}

CURRENCY_RATES = {
    "GHS": 15.50, "NGN": 1600.0, "ZAR": 18.20, "KES": 130.0,
    "USD": 1.0, "GBP": 0.78, "EUR": 0.92, "XOF": 605.0,
}

COMMISSION_FIRST_SALE = 50
COMMISSION_RENEWAL = 35
REFERRAL_MILESTONE = 10

MOMO_NETWORKS = ["MTN Mobile Money", "Telecel Cash (Vodafone)", "AirtelTigo Money"]

# ==============================================================================
# MONGODB
# ==============================================================================
def init_mongodb():
    if not MONGO_URI:
        logger.error("MONGO_URI is not set!")
        return None, None, None, None, None, None, None, None

    try:
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
        withdrawals_col = db["withdrawals"]
        transactions_col = db["affiliate_transactions"]
        webhook_events_col = db["webhook_events"]

        users_col.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
        leads_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("ref_code", ASCENDING)], unique=True)
        referrals_col.create_index([("affiliate_id", ASCENDING)])
        withdrawals_col.create_index([("affiliate_id", ASCENDING), ("status", ASCENDING)])
        transactions_col.create_index([("affiliate_id", ASCENDING), ("created_at", DESCENDING)])
        webhook_events_col.create_index([("reference", ASCENDING)], unique=True)

        return client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col, webhook_events_col

    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        return None, None, None, None, None, None, None, None

_mongo_result = init_mongodb()
if len(_mongo_result) == 9:
    mongo_client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col, webhook_events_col = _mongo_result
else:
    mongo_client = db = users_col = leads_col = affiliates_col = referrals_col = withdrawals_col = transactions_col = webhook_events_col = None

# ==============================================================================
# HELPERS & AUTH
# ==============================================================================
def verify_admin(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API key unset")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

def get_paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}

async def verify_paystack_transaction(reference):
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()
        return data["data"] if data.get("status") else None

def generate_ref_code():
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
    return f"JAY{suffix}"

def log_affiliate_transaction(affiliate_id, transaction_type, amount, description, reference=None):
    if transactions_col is None:
        return
    transactions_col.insert_one({
        "affiliate_id": affiliate_id,
        "type": transaction_type,
        "amount": amount,
        "description": description,
        "reference": reference or f"TXN-{secrets.token_hex(6).upper()}",
        "created_at": datetime.utcnow()
    })

# ==============================================================================
# TELEGRAM BOT & HANDLERS
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()
user_states = {}

async def start_cmd(update: Update, context):
    user = update.effective_user
    if not user:
        return

    chat_id = user.id
    username = user.username or ""
    text = update.message.text or ""

    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            ref_code = payload.replace("ref_", "")
            user_states[chat_id] = {"referred_by": ref_code}

    if leads_col is not None:
        leads_col.update_one(
            {"telegram_id": chat_id},
            {"$setOnInsert": {
                "telegram_id": chat_id, "first_name": user.first_name, "username": username,
                "started_at": datetime.utcnow(), "converted": False, "referred_by": ref_code
            }},
            upsert=True
        )

    is_affiliate = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True}) if affiliates_col is not None else None

    kb = [[InlineKeyboardButton("📈 Subscribe", web_app=WebAppInfo(url=MINI_APP_URL))]]
    if is_affiliate is None:
        kb.append([InlineKeyboardButton("🤝 Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])

    welcome_text = (
        "👑 <b>JAY EMPIRE VIP TERMINAL</b>\n"
        "<i>Success Is Our Aim</i>\n\n"
        "📈 <b>Subscribe</b> — get institutional-grade Gold &amp; FX signals\n"
        "🤝 <b>Become an Affiliate</b> — earn commission sharing your link\n\n"
        "Choose an option below to get started:"
    )
    if ref_code:
        welcome_text += f"\n\n🔗 <i>Referred by:</i> <code>{ref_code}</code>"

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

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
# AFFILIATE FLOWS
# ==============================================================================
async def handle_affiliate_callback(chat_id, action, username=""):
    bot = Bot(token=BOT_TOKEN)

    if action == "affiliate_start":
        kb = [
            [InlineKeyboardButton("✅ I Agree and Join", callback_data="affiliate_agree")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        terms = (
            f"👑 <b>Jay Empire Affiliate Program</b>\n\n"
            f"<b>💰 Commissions:</b>\n"
            f"- First Sale: {COMMISSION_FIRST_SALE}%\n"
            f"- Renewals: {COMMISSION_RENEWAL}%\n\n"
            f"<b>💸 Payout Terms:</b>\n"
            f"- No minimum withdrawal requirement.\n"
            f"- Processed within 24–48 business hours.\n"
            f"- Weekend earnings settle and clear on working days (Paystack rolling settlement).\n\n"
            f"Tap 'I Agree and Join' to register your MoMo details."
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "affiliate_agree":
        user_states[chat_id] = {"step": "awaiting_full_name", "data": {}}
        await bot.send_message(
            chat_id=chat_id,
            text="Step 1/3: Enter your <b>Full Name</b> (as registered on your MoMo account):",
            parse_mode="HTML"
        )

    elif action.startswith("net:"):
        net_index = int(action.split(":")[1])
        selected_net = MOMO_NETWORKS[net_index]
        if chat_id in user_states:
            user_states[chat_id]["data"]["network"] = selected_net
            user_states[chat_id]["step"] = "awaiting_momo_number"

        await bot.send_message(
            chat_id=chat_id,
            text=f"Step 3/3: Enter your <b>{selected_net} Phone Number</b> (e.g., 0241234567):",
            parse_mode="HTML"
        )

    elif action == "affiliate_confirm_setup":
        if chat_id not in user_states:
            return
        d = user_states[chat_id]["data"]
        ref_code = generate_ref_code()

        aff_doc = {
            "telegram_id": chat_id,
            "username": username,
            "full_name": d["full_name"],
            "ref_code": ref_code,
            "momo_network": d["network"],
            "momo_number": d["momo_number"],
            "total_earnings": 0,
            "total_withdrawn": 0,
            "total_referrals": 0,
            "is_active": True,
            "created_at": datetime.utcnow()
        }

        if affiliates_col is not None:
            affiliates_col.insert_one(aff_doc)

        ref_link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 <b>Welcome to Jay Empire Affiliates!</b>\n\n"
                f"🔗 Your Referral Link:\n<code>{ref_link}</code>\n\n"
                f"📱 <b>Payout Account:</b> {d['network']} | <code>{d['momo_number']}</code>\n"
                f"💰 <b>Commissions:</b> {COMMISSION_FIRST_SALE}% First Sale | {COMMISSION_RENEWAL}% Renewals\n"
                f"✨ No minimum withdrawal limit!"
            ),
            parse_mode="HTML"
        )
        del user_states[chat_id]

    elif action == "affiliate_dashboard":
        await show_affiliate_dashboard(chat_id, bot)

    elif action == "request_withdrawal":
        await handle_withdrawal_request(chat_id, bot)

    elif action.startswith("withdraw_confirm:"):
        cents = int(action.split(":")[1])
        await process_withdrawal_confirmation(chat_id, cents, bot)

    elif action.startswith("admin_approve:"):
        w_id = action.split(":")[1]
        await admin_process_payout(w_id, approve=True, bot=bot)

    elif action.startswith("admin_reject:"):
        w_id = action.split(":")[1]
        await admin_process_payout(w_id, approve=False, bot=bot)

    elif action == "back_main":
        await show_main_menu(chat_id, bot)

async def show_main_menu(chat_id, bot):
    is_aff = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True}) if affiliates_col is not None else None
    kb = [[InlineKeyboardButton("📈 Subscribe", web_app=WebAppInfo(url=MINI_APP_URL))]]
    if is_aff is None:
        kb.append([InlineKeyboardButton("🤝 Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
    await bot.send_message(chat_id=chat_id, text="👑 <b>Jay Empire Main Menu</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_affiliate_dashboard(chat_id, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if aff is None:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate yet.")
        return

    ref_link = f"https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}"
    total_earnings = aff.get("total_earnings", 0)
    total_withdrawn = aff.get("total_withdrawn", 0)
    available_balance = total_earnings - total_withdrawn

    dashboard = (
        f"💎 <b>Jay Empire Affiliate Dashboard</b>\n\n"
        f"🔗 <b>Referral Link:</b>\n<code>{ref_link}</code>\n\n"
        f"💰 <b>Earnings Summary:</b>\n"
        f"  Total Earned: ${total_earnings:,.2f}\n"
        f"  Total Withdrawn: ${total_withdrawn:,.2f}\n"
        f"  <b>Available Balance: ${available_balance:,.2f}</b>\n\n"
        f"📱 <b>MoMo Details:</b>\n"
        f"  {aff.get('momo_network','N/A')} | <code>{aff.get('momo_number','N/A')}</code>\n\n"
        f"📊 <b>Ref Code:</b> <code>{aff['ref_code']}</code>"
    )

    kb = [
        [InlineKeyboardButton("💸 Request Withdrawal", callback_data="request_withdrawal")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]
    ]

    await bot.send_message(chat_id=chat_id, text=dashboard, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_withdrawal_request(chat_id, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if aff is None:
        return

    pending = withdrawals_col.count_documents({"affiliate_id": aff["_id"], "status": "pending"}) if withdrawals_col is not None else 0
    if pending > 0:
        await bot.send_message(chat_id=chat_id, text="⚠️ You already have a pending withdrawal request under review.")
        return

    total_earnings = aff.get("total_earnings", 0)
    total_withdrawn = aff.get("total_withdrawn", 0)
    available = total_earnings - total_withdrawn

    if available <= 0:
        await bot.send_message(chat_id=chat_id, text="❌ You have no available balance to withdraw right now.")
        return

    full_cents = int(round(available * 100))
    kb = [
        [InlineKeyboardButton(f"Withdraw Full Balance (${available:,.2f})", callback_data=f"withdraw_confirm:{full_cents}")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_dashboard")]
    ]

    # Weekend / settlement warning logic
    is_weekend = datetime.utcnow().weekday() in [5, 6]
    weekend_note = "\n\n<b>Note:</b> Requests submitted on weekends are processed on the next working day after Paystack settlements clear." if is_weekend else ""

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"💸 <b>Request Withdrawal</b>\n\n"
            f"Available Balance: <b>${available:,.2f}</b>\n"
            f"Payout Account: {aff.get('momo_network')} (<code>{aff.get('momo_number')}</code>)"
            f"{weekend_note}"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def process_withdrawal_confirmation(chat_id, amount_cents, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if aff is None:
        return

    amount_usd = amount_cents / 100
    rate = CURRENCY_RATES.get("GHS", 15.50)
    amount_ghs = amount_usd * rate

    doc = {
        "affiliate_id": aff["_id"],
        "telegram_id": chat_id,
        "username": aff.get("username", ""),
        "full_name": aff.get("full_name", ""),
        "ref_code": aff.get("ref_code", ""),
        "amount_usd": amount_usd,
        "amount_ghs": amount_ghs,
        "momo_network": aff.get("momo_network"),
        "momo_number": aff.get("momo_number"),
        "status": "pending",
        "created_at": datetime.utcnow()
    }

    res = withdrawals_col.insert_one(doc) if withdrawals_col is not None else None
    w_id = str(res.inserted_id) if res else ""

    log_affiliate_transaction(aff["_id"], "withdrawal_request", amount_usd, f"Requested payout ${amount_usd:,.2f}")

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ <b>Withdrawal Request Received!</b>\n\n"
            f"Amount: <b>${amount_usd:,.2f}</b> (Approx GH₵ {amount_ghs:,.2f})\n"
            f"Status: <i>Processing (24-48 business hours)</i>\n\n"
            f"You will receive a notification once the transfer is sent to your MoMo account."
        ),
        parse_mode="HTML"
    )

    # Direct 1-Click Alert to Admin Telegram
    if ADMIN_TELEGRAM_ID > 0:
        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Mark as Paid", callback_data=f"admin_approve:{w_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject:{w_id}")
            ]
        ])

        try:
            await bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=(
                    f"🚨 <b>NEW PAYOUT REQUEST</b>\n\n"
                    f"👤 <b>Affiliate:</b> {aff.get('full_name')} (@{aff.get('username','N/A')})\n"
                    f"📱 <b>Network:</b> {aff.get('momo_network')}\n"
                    f"📞 <b>MoMo Number:</b> <code>{aff.get('momo_number')}</code>\n"
                    f"💵 <b>Amount:</b> ${amount_usd:,.2f} (<b>GH₵ {amount_ghs:,.2f}</b>)\n"
                    f"🆔 <b>Code:</b> <code>{aff.get('ref_code')}</code>"
                ),
                parse_mode="HTML",
                reply_markup=admin_kb
            )
        except Exception as e:
            logger.error(f"Failed to alert admin: {e}")

async def admin_process_payout(w_id: str, approve: bool, bot: Bot):
    from bson.objectid import ObjectId

    w = withdrawals_col.find_one({"_id": ObjectId(w_id)}) if withdrawals_col is not None else None
    if w is None or w.get("status") != "pending":
        return

    aff = affiliates_col.find_one({"_id": w["affiliate_id"]}) if affiliates_col is not None else None
    if aff is None:
        return

    now = datetime.utcnow()

    if approve:
        withdrawals_col.update_one({"_id": ObjectId(w_id)}, {"$set": {"status": "approved", "processed_at": now}})
        affiliates_col.update_one({"_id": aff["_id"]}, {"$inc": {"total_withdrawn": w["amount_usd"]}})

        log_affiliate_transaction(aff["_id"], "withdrawal", w["amount_usd"], f"Payout approved: ${w['amount_usd']:,.2f}")

        try:
            await bot.send_message(
                chat_id=w["telegram_id"],
                text=f"🎉 <b>Payout Approved & Sent!</b>\n\nYour withdrawal of <b>${w['amount_usd']:,.2f}</b> (GH₵ {w['amount_ghs']:,.2f}) has been deposited to your {w['momo_network']} account.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user of payout: {e}")
    else:
        withdrawals_col.update_one({"_id": ObjectId(w_id)}, {"$set": {"status": "rejected", "processed_at": now}})
        try:
            await bot.send_message(
                chat_id=w["telegram_id"],
                text=f"❌ Your withdrawal request of ${w['amount_usd']:,.2f} was declined. Please contact support @{ADMIN_USERNAME}.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user of rejection: {e}")

# ==============================================================================
# TELEGRAM WEBHOOK INGESTION
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    if TELEGRAM_WEBHOOK_SECRET and (not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET)):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    data = await request.json()

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

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]

        if text.startswith("/"):
            update = Update.de_json(data, telegram_app.bot)
            await telegram_app.process_update(update)
            return {"status": "ok"}

        if chat_id in user_states:
            state = user_states[chat_id]
            step = state.get("step")
            bot = Bot(token=BOT_TOKEN)

            if step == "awaiting_full_name":
                state["data"]["full_name"] = text
                state["step"] = "awaiting_network"

                kb = [[InlineKeyboardButton(net, callback_data=f"net:{i}")] for i, net in enumerate(MOMO_NETWORKS)]
                await bot.send_message(
                    chat_id=chat_id,
                    text="Step 2/3: Select your <b>Mobile Money Network</b>:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return {"status": "ok"}

            elif step == "awaiting_momo_number":
                state["data"]["momo_number"] = text.strip()
                state["step"] = "awaiting_confirmation"

                kb = [
                    [InlineKeyboardButton("Confirm & Save", callback_data="affiliate_confirm_setup")],
                    [InlineKeyboardButton("Start Over", callback_data="affiliate_agree")]
                ]
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Confirm Details:\n\nName: <b>{state['data']['full_name']}</b>\nNetwork: <b>{state['data']['network']}</b>\nNumber: <b>{text}</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return {"status": "ok"}

    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

# ==============================================================================
# PAYSTACK WEBHOOK (AUTOMATED COMMISSION TRACKING)
# ==============================================================================
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    if not PAYSTACK_SECRET:
        raise HTTPException(status_code=500, detail="Paystack secret not set")

    body = await request.body()
    expected = hmac.new(PAYSTACK_SECRET.encode(), body, hashlib.sha512).hexdigest()

    if x_paystack_signature is None or not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    if payload.get("event") == "charge.success":
        data = payload["data"]
        reference = data.get("reference", "unknown")

        if webhook_events_col is not None:
            try:
                webhook_events_col.insert_one({"reference": reference, "processed_at": datetime.utcnow()})
            except DuplicateKeyError:
                return {"status": "already_processed"}

        verified = await verify_paystack_transaction(reference)
        if not verified or verified.get("status") != "success":
            return {"status": "unverified"}

        meta = data.get("metadata", {}) or {}
        tg_id = meta.get("telegram_id")
        channel_type = meta.get("channel_type", "gold")
        days = int(meta.get("days", 30))
        ref_code = meta.get("ref_code")
        is_renewal = bool(meta.get("is_renewal", False))

        if not tg_id:
            return {"status": "ignored"}

        now = datetime.utcnow()
        expires = now + timedelta(days=days)

        if users_col is not None:
            users_col.update_one(
                {"telegram_id": tg_id, "channel_type": channel_type},
                {"$set": {"is_active": True, "expires_at": expires, "paystack_reference": reference}},
                upsert=True
            )

        # Affiliate Commission Attribution
        if ref_code and affiliates_col is not None:
            aff = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
            if aff and aff["telegram_id"] != tg_id:
                rate = COMMISSION_RENEWAL if is_renewal else COMMISSION_FIRST_SALE
                paid_cents = data.get("amount", 0)
                comm_cents = int(paid_cents * rate / 100)
                comm_usd = comm_cents / 100

                affiliates_col.update_one(
                    {"_id": aff["_id"]},
                    {"$inc": {"total_earnings": comm_usd, "total_referrals": 0 if is_renewal else 1}}
                )

                log_affiliate_transaction(aff["_id"], "commission", comm_usd, f"Earned {rate}% commission from user {tg_id}")

                try:
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=aff["telegram_id"],
                        text=f"🎯 <b>New Referral Sale!</b>\n\nYou earned <b>${comm_usd:,.2f}</b> ({rate}% commission) from a subscription purchase.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify affiliate of commission: {e}")

    return {"status": "success"}

# ==============================================================================
# LIFESPAN & API
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    if TELEGRAM_WEBHOOK_SECRET:
        await bot.set_webhook(url=webhook_target, secret_token=TELEGRAM_WEBHOOK_SECRET)
    else:
        await bot.set_webhook(url=webhook_target)

    yield
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    return {"status": "active", "timestamp": datetime.utcnow().isoformat()}

@app.post("/api/initiate-payment")
async def api_initiate_payment(payload: dict):
    plan = PLANS_BY_KEY.get(payload.get("plan_key"))
    rate = CURRENCY_RATES.get(payload.get("currency"), 1.0)
    amount_minor = round(plan["usd"] * rate * 100)
    reference = f"JAY-{secrets.token_hex(8).upper()}"

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            json={
                "email": payload.get("email", f"user_{payload.get('telegram_id')}@jayempire.com"),
                "amount": amount_minor,
                "currency": payload.get("currency"),
                "reference": reference,
                "metadata": payload
            },
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()

    return {"access_code": data["data"]["access_code"], "reference": reference}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
