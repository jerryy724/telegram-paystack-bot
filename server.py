"""
server.py -- Jay Empire VIP Backend + Affiliate System
Customer Payments via Paystack + Internal Affiliate Ledger + Manual Payouts
Manual Bank Transfer + Mobile Money payout records
Withdrawal Request System + Affiliate Portal + Admin Dashboard

SECURITY REVISION NOTES:
- Checkout price is locked server-side via /api/initiate-payment.
- Single-use Telegram VIP invite links are generated per successful payment.
- All /admin/* and /cron/* endpoints require X-Admin-Key matching ADMIN_API_KEY.
- Webhook checks TELEGRAM_WEBHOOK_SECRET header if configured.
- Affiliate payouts are manual: Paystack is used only to collect customer subscription payments.
- Affiliate payout details are stored internally and sent manually by the administrator.
"""

import os
import asyncio
import logging
import json
import re
import ssl
import certifi
import hmac
import hashlib
import httpx
import secrets
import string
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import parse_qsl
from datetime import timezone
import html
from fastapi.middleware.cors import CORSMiddleware

from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
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
BOT_USERNAME = os.getenv("BOT_USERNAME", "JayEmpire_bot").lstrip("@")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
PAYSTACK_ACCOUNT_COUNTRY = os.getenv("PAYSTACK_ACCOUNT_COUNTRY", "ghana").lower()

# ==============================================================================
# CANONICAL PRICING
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

PAYMENT_CURRENCIES = tuple(x.strip().upper() for x in os.getenv("PAYMENT_CURRENCIES", "GHS").split(",") if x.strip())

CURRENCY_RATES = {
    "GHS": 15.50, "NGN": 1600.0, "ZAR": 18.20, "KES": 130.0,
    "USD": 1.0, "GBP": 0.78, "EUR": 0.92, "XOF": 605.0,
}

# ==============================================================================
# COMMISSION CONFIG
# ==============================================================================
COMMISSION_FIRST_SALE = 50
COMMISSION_RENEWAL = 35
REFERRAL_MILESTONE = 10
MIN_WITHDRAWAL_MINOR = int(float(os.getenv("MIN_WITHDRAWAL_GHS", "50")) * 100)
WITHDRAWAL_COOLDOWN_DAYS = int(os.getenv("WITHDRAWAL_COOLDOWN_DAYS", "7"))

# ==============================================================================
# AFRICA PAYOUT CONFIGURATION
# ==============================================================================
AFRICA_COUNTRIES = {
    "ghana": {"name": "Ghana", "currency": "GHS", "flag": "🇬🇭", "calling_code": "233"},
    "nigeria": {"name": "Nigeria", "currency": "NGN", "flag": "🇳🇬", "calling_code": "234"},
    "kenya": {"name": "Kenya", "currency": "KES", "flag": "🇰🇪", "calling_code": "254"},
    "south_africa": {"name": "South Africa", "currency": "ZAR", "flag": "🇿🇦", "calling_code": "27"},
}
MOMO_ELIGIBLE_COUNTRIES = {"ghana", "kenya"}

def normalize_local_phone(raw: str, calling_code: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if calling_code and digits.startswith(calling_code):
        digits = "0" + digits[len(calling_code):]
    elif not digits.startswith("0"):
        digits = "0" + digits
    return digits

# ==============================================================================
# MONGODB
# ==============================================================================
onboarding_col = None

def init_mongodb():
    if not MONGO_URI:
        logger.error("MONGO_URI is not set!")
        return None, None, None, None, None, None, None, None, None

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
        payment_intents_col = db["payment_intents"]
        customers_col = db["customers"]
        onboarding_col = db["affiliate_onboarding"]

        users_col.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
        users_col.create_index([("expires_at", ASCENDING)])
        users_col.create_index([("is_active", ASCENDING)])
        users_col.create_index([("referred_by", ASCENDING)])

        leads_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("ref_code", ASCENDING)], unique=True)
        referrals_col.create_index([("affiliate_id", ASCENDING)])
        referrals_col.create_index([("customer_telegram_id", ASCENDING)])
        withdrawals_col.create_index([("affiliate_id", ASCENDING), ("status", ASCENDING)])
        withdrawals_col.create_index([("created_at", DESCENDING)])
        transactions_col.create_index([("affiliate_id", ASCENDING), ("created_at", DESCENDING)])
        transactions_col.create_index([("reference", ASCENDING)], unique=True)
        try:
            webhook_events_col.drop_index("reference_1")
        except Exception:
            pass
        webhook_events_col.create_index([("event_key", ASCENDING)], unique=True)
        payment_intents_col.create_index([("reference", ASCENDING)], unique=True)
        payment_intents_col.create_index([("telegram_id", ASCENDING), ("created_at", DESCENDING)])
        customers_col.create_index([("telegram_id", ASCENDING)], unique=True)
        onboarding_col.create_index([("telegram_id", ASCENDING)], unique=True)

        # Migrate the old floating-point wallet fields once, preserving balances.
        for aff in affiliates_col.find({"wallet_earned_minor": {"$exists": False}},
                                       {"_id": 1, "total_earnings": 1, "total_withdrawn": 1}):
            affiliates_col.update_one(
                {"_id": aff["_id"]},
                {"$set": {
                    "wallet_earned_minor": int(round(float(aff.get("total_earnings", 0)) * 100)),
                    "wallet_withdrawn_minor": int(round(float(aff.get("total_withdrawn", 0)) * 100)),
                    "wallet_reserved_minor": 0,
                }}
            )

        return client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col, webhook_events_col, payment_intents_col, customers_col

    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        return None, None, None, None, None, None, None, None, None

_mongo_result = init_mongodb()
if len(_mongo_result) == 11:
    mongo_client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col, webhook_events_col, payment_intents_col, customers_col = _mongo_result
    onboarding_col = db["affiliate_onboarding"] if db is not None else None
else:
    mongo_client = db = users_col = leads_col = affiliates_col = referrals_col = withdrawals_col = transactions_col = webhook_events_col = payment_intents_col = customers_col = onboarding_col = None

# ==============================================================================
# ADMIN AUTH DEPENDENCY
# ==============================================================================
def verify_admin(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API not configured (ADMIN_API_KEY unset)")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ==============================================================================
# TELEGRAM MINI APP AUTHENTICATION
# ==============================================================================
def validate_telegram_init_data(init_data: str) -> dict:
    """Validate Telegram Mini App initData server-side and return its fields."""
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Bot is not configured")
    if not init_data or len(init_data) > 4096:
        raise HTTPException(status_code=401, detail="Invalid Telegram session")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash or len(received_hash) != 64:
        raise HTTPException(status_code=401, detail="Invalid Telegram session")

    data_check_string = "\n".join(
        f"{key}={pairs[key]}" for key in sorted(pairs)
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram session")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Telegram session")

    # Telegram recommends checking freshness of initData.
    if abs(datetime.now(timezone.utc).timestamp() - auth_date) > 3600:
        raise HTTPException(status_code=401, detail="Telegram session expired")

    try:
        user = json.loads(pairs.get("user", "{}"))
        telegram_id = int(user["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram user")

    return {"telegram_id": telegram_id, "user": user, "fields": pairs}


# ==============================================================================
# PAYSTACK HELPERS
# ==============================================================================

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
        if data.get("status"):
            return data["data"]
        return None

def generate_ref_code():
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
    return f"JAY{suffix}"

# ==============================================================================
# TRANSACTION LOGGING
# ==============================================================================
def log_affiliate_transaction(affiliate_id, transaction_type, amount, description, reference=None, metadata=None):
    if transactions_col is None:
        return

    doc = {
        "affiliate_id": affiliate_id,
        "type": transaction_type,
        "amount": amount,
        "amount_minor": int(round(float(amount) * 100)),
        "currency": "GHS",
        "description": description,
        "reference": reference or f"TXN-{secrets.token_hex(6).upper()}",
        "metadata": metadata or {},
        "created_at": datetime.utcnow()
    }
    transactions_col.insert_one(doc)

# ==============================================================================
# TELEGRAM BOT & STATE SETUP
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()
# Affiliate onboarding is persisted in MongoDB instead of process memory.
# This prevents the flow from disappearing after a Render restart/redeploy and
# also makes the confirmation button independent of local server memory.

def save_affiliate_onboarding(telegram_id, step, data):
    if onboarding_col is None:
        return False
    onboarding_col.update_one(
        {"telegram_id": int(telegram_id)},
        {"$set": {"telegram_id": int(telegram_id), "step": step, "data": data, "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return True

def get_affiliate_onboarding(telegram_id):
    if onboarding_col is None:
        return None
    return onboarding_col.find_one({"telegram_id": int(telegram_id)})

def clear_affiliate_onboarding(telegram_id):
    if onboarding_col is not None:
        onboarding_col.delete_one({"telegram_id": int(telegram_id)})

async def start_cmd(update: Update, context):
    user = update.effective_user
    if not user:
        return

    chat_id = user.id
    username = user.username or ""
    text = update.message.text or ""

    logger.info(f"/start from {chat_id} (@{username})")

    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            candidate = payload[4:].strip().upper()
            ref_code = candidate if re.fullmatch(r"JAY[A-Z0-9]{7}", candidate) else None
            if ref_code:
                # Persist referral attribution in MongoDB; do not rely on Render memory.
                if leads_col is not None:
                    leads_col.update_one(
                        {"telegram_id": chat_id, "$or": [{"referred_by": {"$exists": False}}, {"referred_by": None}, {"referred_by": ""}]},
                        {"$set": {"referred_by": ref_code, "referral_locked_at": datetime.utcnow()}, "$setOnInsert": {"telegram_id": chat_id, "started_at": datetime.utcnow()}},
                        upsert=True
                    )
                if customers_col is not None:
                    customers_col.update_one(
                        {"telegram_id": chat_id, "$or": [{"referred_by": {"$exists": False}}, {"referred_by": None}, {"referred_by": ""}]},
                        {"$set": {"referred_by": ref_code, "referral_locked_at": datetime.utcnow()}},
                        upsert=True
                    )
            logger.info(f"Referral detected: {ref_code} for user {chat_id}")

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

    is_affiliate = None
    if affiliates_col is not None:
        is_affiliate = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True})

    kb = [[InlineKeyboardButton("📈 Subscribe", web_app=WebAppInfo(url=MINI_APP_URL))]]

    if is_affiliate is None and not ref_code:
        kb.append([InlineKeyboardButton("🤝 Become an Affiliate", callback_data="affiliate_start")])
    elif is_affiliate is not None:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
        active_refs = 0
        if referrals_col is not None:
            active_refs = referrals_col.count_documents({
                "affiliate_id": is_affiliate["_id"],
                "is_active": True
            })
        if active_refs >= REFERRAL_MILESTONE and not is_affiliate.get("milestone_notified"):
            await notify_milestone(update, is_affiliate, active_refs)

    if ref_code:
        welcome_text = (
            "👑 <b>JAY EMPIRE VIP TERMINAL</b>\n"
            "<i>Success Is Our Aim</i>\n\n"
            "🎯 <b>You’ve been invited to JAY Trading Hub.</b>\n\n"
            "JAY Trading Hub is a premium trading community for serious traders seeking structured market insights, educational resources, trading ideas and private member-only updates.\n\n"
            "Your invitation gives you access to our secure VIP Terminal, where you can review the available plans and choose the subscription that fits you best.\n\n"
            "<b>Inside the Hub:</b>\n"
            "• Premium Gold &amp; FX trading resources\n"
            "• Market insights and educational content\n"
            "• Private community access\n"
            "• Member-only updates and opportunities\n\n"
            "Tap <b>📈 Subscribe</b> below to view the available plans and begin your access."
        )
    else:
        welcome_text = (
            "👑 <b>JAY EMPIRE VIP TERMINAL</b>\n"
            "<i>Success Is Our Aim</i>\n\n"
            "📈 <b>Subscribe</b> — get institutional-grade Gold &amp; FX signals\n"
            + ("🤝 <b>Become an Affiliate</b> — earn commission sharing your link\n\n" if not ref_code and is_affiliate is None else "")
            + "Choose an option below to get started:"
        )

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def notify_milestone(update, affiliate, count):
    try:
        await update.message.reply_text(
            f"🏆 <b>Milestone unlocked!</b>\n\n"
            f"You've referred {count} active members and earned <b>Lifetime VIP Access</b>.\n\n"
            f"Contact @{ADMIN_USERNAME} to claim your reward. Show this message as proof.\n\n"
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

async def callback_handler(update: Update, context):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass
    chat_id = query.from_user.id if query.from_user else query.message.chat.id
    action = query.data or ""
    username = query.from_user.username or "" if query.from_user else ""
    try:
        await handle_affiliate_callback(chat_id, action, username)
    except Exception as e:
        logger.exception("Callback handling failed: action=%s chat_id=%s", action, chat_id)
        try:
            await Bot(token=BOT_TOKEN).send_message(
                chat_id=chat_id,
                text="⚠️ Something went wrong while processing that button. Please try again."
            )
        except Exception:
            pass

telegram_app.add_handler(CommandHandler("start", start_cmd))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))

# ==============================================================================
# AFFILIATE CALLBACK HANDLERS
# ==============================================================================

async def show_affiliate_confirmation(chat_id, bot):
    state = get_affiliate_onboarding(chat_id)
    if not state:
        await bot.send_message(chat_id=chat_id, text="Your affiliate setup session expired. Tap Become an Affiliate and start again.")
        return
    d = state.get("data", {})
    details = d.get("payout_details_input", "")
    kb = [
        [InlineKeyboardButton("✅ Confirm and Create", callback_data="affiliate_confirm")],
        [InlineKeyboardButton("🔄 Start Over", callback_data="affiliate_agree")],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
    ]
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"<b>Confirm Affiliate Account</b>\n\n"
            f"Name: {html.escape(str(d.get('full_name', '')))}\n"
            f"Mobile Money: {html.escape(str(d.get('payout_details_input', '')))}\n\n"
            "If these details are correct, tap <b>Confirm and Create</b>. "
            "Your affiliate account will be created directly in MongoDB and your unique referral link will be generated."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def handle_affiliate_callback(chat_id, action, username=""):
    bot = Bot(token=BOT_TOKEN)

    if action == "affiliate_confirm":
        logger.info("Affiliate confirm received via PTB handler for Telegram ID %s", chat_id)
        if affiliates_col is None or onboarding_col is None:
            await bot.send_message(chat_id=chat_id, text="⚠️ Affiliate service is temporarily unavailable because the database is not connected. Please try again.")
            return

        state = get_affiliate_onboarding(chat_id)
        if not state or state.get("step") != "awaiting_confirmation":
            await bot.send_message(chat_id=chat_id, text="⚠️ Your affiliate setup session is not active. Tap Become an Affiliate and start again.")
            return

        existing = affiliates_col.find_one({"telegram_id": chat_id})
        if existing and existing.get("is_active", True):
            ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{existing['ref_code']}"
            clear_affiliate_onboarding(chat_id)
            await bot.send_message(chat_id=chat_id, text=f"You already have an affiliate account.\n\nYour referral link:\n{ref_link}")
            return

        d = state.get("data") or {}
        full_name = str(d.get("full_name", "")).strip()
        momo_number = str(d.get("payout_details_input", "")).strip()
        if len(full_name) < 2 or not re.fullmatch(r"0[0-9]{9}", momo_number):
            await bot.send_message(chat_id=chat_id, text="⚠️ The saved details are invalid. Please start again and use: Full Name - Mobile Money Number")
            return

        # Generate a collision-safe referral code. The code is created locally; Paystack is not involved.
        ref_code = None
        for _ in range(10):
            candidate = generate_ref_code()
            if affiliates_col.find_one({"ref_code": candidate}) is None:
                ref_code = candidate
                break
        if not ref_code:
            await bot.send_message(chat_id=chat_id, text="⚠️ Could not generate a unique affiliate link. Please try again.")
            return

        aff_doc = {
            "telegram_id": int(chat_id),
            "username": username or "",
            "full_name": full_name,
            "ref_code": ref_code,
            "payout_method": "momo",
            "country": "ghana",
            "country_name": "Ghana",
            "commission_rates": {"first_sale": COMMISSION_FIRST_SALE, "renewal": COMMISSION_RENEWAL},
            "total_earnings": 0,
            "total_withdrawn": 0,
            "total_referrals": 0,
            "wallet_earned_minor": 0,
            "wallet_withdrawn_minor": 0,
            "wallet_reserved_minor": 0,
            "is_active": True,
            "milestone_notified": False,
            "manual_payout_details": momo_number,
            "created_at": datetime.utcnow(),
        }

        try:
            result = affiliates_col.insert_one(aff_doc)
            created = affiliates_col.find_one({"_id": result.inserted_id})
            if not created:
                raise RuntimeError("Affiliate record was not found after insertion")
        except DuplicateKeyError:
            existing = affiliates_col.find_one({"telegram_id": chat_id})
            if existing:
                ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{existing['ref_code']}"
                clear_affiliate_onboarding(chat_id)
                await bot.send_message(chat_id=chat_id, text=f"Your affiliate account already exists.\n\nYour referral link:\n{ref_link}")
                return
            await bot.send_message(chat_id=chat_id, text="⚠️ The affiliate account could not be created because of a database conflict. Please try again.")
            return
        except Exception as e:
            logger.exception("Affiliate creation failed for %s", chat_id)
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Affiliate account creation failed: {type(e).__name__}. Please try again.")
            if ADMIN_TELEGRAM_ID:
                try:
                    await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=f"⚠️ Affiliate creation error\nTelegram ID: {chat_id}\nError: {type(e).__name__}: {e}")
                except Exception:
                    pass
            return

        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{ref_code}"
        clear_affiliate_onboarding(chat_id)

        try:
            await bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=(
                    f"🆕 <b>New Affiliate Created</b>\n\n"
                    f"Name: {html.escape(full_name)}\n"
                    f"Username: @{html.escape(username or 'N/A')}\n"
                    f"Telegram ID: <code>{chat_id}</code>\n"
                    f"Referral Code: <code>{ref_code}</code>\n"
                    f"Referral Link: {html.escape(ref_link)}\n"
                    f"Payout Method: Mobile Money\n"
                    f"MoMo Number: <code>{html.escape(momo_number)}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Failed to notify admin about new affiliate: %s", e)

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🎉 <b>Affiliate Account Created Successfully!</b>\n\n"
                f"Name: {html.escape(full_name)}\n"
                f"Mobile Money: <code>{html.escape(momo_number)}</code>\n\n"
                f"<b>Your unique referral link:</b>\n{html.escape(ref_link)}\n\n"
                f"Commission: {COMMISSION_FIRST_SALE}% first qualifying sale | {COMMISSION_RENEWAL}% renewal\n"
                f"Milestone: {REFERRAL_MILESTONE} active referrals = Lifetime VIP reward\n\n"
                "Share this link with customers. When they start the bot through your link, their referral attribution is locked to you before checkout. "
                "Paystack is only used later for the customer's normal subscription payment; it is not involved in affiliate creation or payouts."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📈 Open VIP Subscription", web_app=WebAppInfo(url=MINI_APP_URL))],
                [InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")]
            ])
        )
        return

    # Admin payout buttons are only actionable from the configured admin chat.
    if action.startswith("admin_withdraw_"):
        if chat_id != ADMIN_TELEGRAM_ID:
            logger.warning("Unauthorized admin callback from %s", chat_id)
            return
        try:
            withdrawal_id = action.split(":", 1)[1]
        except IndexError:
            return
        if action.startswith("admin_withdraw_approve:"):
            result = await approve_withdrawal_internal(withdrawal_id)
            msg = "Marked as PAID. Manual payout should already have been sent." if result.get("ok") else f"Payment confirmation failed: {result.get('error','unknown error')}"
        else:
            result = await reject_withdrawal_internal(withdrawal_id, "Rejected by admin")
            msg = "Withdrawal rejected." if result.get("ok") else f"Rejection failed: {result.get('error','unknown error')}"
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    if action == "affiliate_start":
        kb = [
            [InlineKeyboardButton("✅ I Agree and Join", callback_data="affiliate_agree")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        terms = (
            f"👑 <b>JAY Trading Hub Affiliate Program</b>\n\n"
            f"<b>💰 Commission:</b> {COMMISSION_FIRST_SALE}% on a first qualifying sale and {COMMISSION_RENEWAL}% on renewals.\n"
            f"<b>🏆 Milestone:</b> {REFERRAL_MILESTONE} active referrals = Lifetime VIP reward.\n"
            f"<b>💸 Withdrawal:</b> Minimum GHS {MIN_WITHDRAWAL_MINOR/100:,.2f}; payouts are sent manually by JAY Trading Hub.\n\n"
            f"No fake signups, self-referrals or spam. By joining, you agree to these terms and to provide accurate payout information.\n\n"
            "Tap <b>I Agree and Join</b> to continue."
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "affiliate_agree":
        save_affiliate_onboarding(chat_id, "awaiting_affiliate_details", {})
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>Affiliate Setup</b>\n\n"
                "Send your details in ONE message using this format:\n\n"
                "<code>Full Name - Mobile Money Number</code>\n\n"
                "Example:\n"
                "<code>John Doe - 0241234567</code>\n\n"
                "Use the MoMo number that should receive your manual affiliate payouts."
            ),
            parse_mode="HTML"
        )

    elif action == "affiliate_dashboard":
        await show_affiliate_dashboard(chat_id, bot)

    elif action == "affiliate_statement":
        await show_affiliate_statement(chat_id, bot)

    elif action == "affiliate_referrals":
        await show_affiliate_referrals(chat_id, bot)

    elif action == "request_withdrawal":
        await handle_withdrawal_request(chat_id, bot)

    elif action == "affiliate_payout_info":
        await show_payout_info(chat_id, bot)

    elif action.startswith("aff_copy:"):
        ref_code = action.split(":")[1]
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{ref_code}"
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔗 Your Link:\n\n<code>{link}</code>\n\nTap and hold to copy!",
            parse_mode="HTML"
        )

    elif action == "back_main":
        await show_main_menu(chat_id, bot)

    elif action.startswith("withdraw_confirm:"):
        amount = int(action.split(":")[1])
        await process_withdrawal_confirmation(chat_id, amount, bot)

    elif action == "withdraw_cancel":
        await bot.send_message(chat_id=chat_id, text="Withdrawal cancelled.")
        await show_affiliate_dashboard(chat_id, bot)

async def show_main_menu(chat_id, bot):
    is_aff = None
    if affiliates_col is not None:
        is_aff = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True})
    kb = [[InlineKeyboardButton("📈 Subscribe", web_app=WebAppInfo(url=MINI_APP_URL))]]
    if is_aff is None:
        kb.append([InlineKeyboardButton("🤝 Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
    await bot.send_message(chat_id=chat_id, text="👑 <b>Jay Empire Main Menu</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def get_affiliate(chat_id):
    if affiliates_col is None:
        return None
    return affiliates_col.find_one({"telegram_id": chat_id, "is_active": True})


def wallet_values(aff):
    # Backward-compatible read of old accounts; all new accounting is integer GHS minor units.
    earned = int(aff.get("wallet_earned_minor", round(float(aff.get("total_earnings", 0)) * 100)))
    withdrawn = int(aff.get("wallet_withdrawn_minor", round(float(aff.get("total_withdrawn", 0)) * 100)))
    reserved = int(aff.get("wallet_reserved_minor", 0))
    return earned, withdrawn, reserved, max(0, earned - withdrawn - reserved)


async def show_affiliate_dashboard(chat_id, bot):
    aff = await get_affiliate(chat_id)
    if aff is None:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an active affiliate yet.")
        return

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{aff['ref_code']}"
    total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]}) if referrals_col is not None else 0
    active_refs = 0
    if referrals_col is not None:
        for ref in referrals_col.find({"affiliate_id": aff["_id"]}, {"customer_telegram_id": 1}):
            if users_col is not None and users_col.count_documents({
                "telegram_id": ref["customer_telegram_id"],
                "is_active": True
            }) > 0:
                active_refs += 1

    earned, withdrawn, reserved, available = wallet_values(aff)
    pending = withdrawals_col.count_documents({
        "affiliate_id": aff["_id"],
        "status": {"$in": ["pending", "processing"]}
    }) if withdrawals_col is not None else 0

    milestone = "UNLOCKED!" if active_refs >= REFERRAL_MILESTONE else f"({active_refs}/{REFERRAL_MILESTONE})"
    payout_method = aff.get("payout_method", "bank")
    if payout_method == "bank":
        b = aff.get("bank_details", {})
        payout_detail = f"🏦 {b.get('bank_name','N/A')} • ****{str(b.get('account_number',''))[-4:]}"
    else:
        m = aff.get("mobile_money_details", {})
        payout_detail = f"📱 {m.get('provider_name','N/A')} • {m.get('phone_number','N/A')}"

    dashboard = (
        "💎 <b>Jay Empire Affiliate Dashboard</b>\n\n"
        f"🔗 <b>Referral Link:</b>\n<code>{html.escape(ref_link)}</code>\n\n"
        "💰 <b>Wallet (GHS)</b>\n"
        f"  Total Earned: GHS {earned/100:,.2f}\n"
        f"  Total Withdrawn: GHS {withdrawn/100:,.2f}\n"
        f"  Reserved/Pending: GHS {reserved/100:,.2f}\n"
        f"  <b>Available: GHS {available/100:,.2f}</b>\n"
        f"  Pending Payouts: {pending}\n\n"
        "👥 <b>Referrals</b>\n"
        f"  Total: {total_refs} | Active: {active_refs}\n"
        f"  Commission: {COMMISSION_FIRST_SALE}% first | {COMMISSION_RENEWAL}% renewal\n"
        f"  🏆 Milestone: {milestone}\n\n"
        f"💳 <b>Payout:</b> {html.escape(payout_detail)}\n"
        f"📊 <b>Code:</b> <code>{html.escape(aff['ref_code'])}</code>"
    )
    kb = [
        [InlineKeyboardButton("📋 View Statement", callback_data="affiliate_statement")],
        [InlineKeyboardButton("👥 My Referrals", callback_data="affiliate_referrals")],
        [InlineKeyboardButton("💸 Request Withdrawal", callback_data="request_withdrawal")],
        [InlineKeyboardButton("💳 Payout Info", callback_data="affiliate_payout_info")],
        [InlineKeyboardButton("🔗 Copy Link", callback_data=f"aff_copy:{aff['ref_code']}")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]
    ]
    await bot.send_message(chat_id=chat_id, text=dashboard, parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(kb))


async def show_affiliate_statement(chat_id, bot):
    aff = await get_affiliate(chat_id)
    if aff is None:
        await bot.send_message(chat_id=chat_id, text="Affiliate not found.")
        return
    txns = list(transactions_col.find({"affiliate_id": aff["_id"]}).sort("created_at", DESCENDING).limit(30)) if transactions_col is not None else []
    if not txns:
        await bot.send_message(chat_id=chat_id, text="📋 <b>Your Statement</b>\n\nNo transactions yet.", parse_mode="HTML")
        return

    total_in = total_out = 0
    text = "📋 <b>Your Financial Statement (GHS)</b>\n\n"
    for txn in txns:
        amount_minor = int(txn.get("amount_minor", round(float(txn.get("amount", 0))*100)))
        typ = txn.get("type", "transaction")
        if typ in {"commission", "withdrawal_reversal"}:
            total_in += amount_minor
            sign = "+"
        elif typ == "withdrawal":
            total_out += amount_minor
            sign = "-"
        else:
            continue
        date_str = txn.get("created_at", datetime.utcnow()).strftime("%d/%m/%Y")
        text += f"{date_str} {html.escape(typ)} <code>{sign}GHS {amount_minor/100:,.2f}</code>\n"
        if txn.get("description"):
            text += f"  ↳ {html.escape(str(txn['description']))[:120]}\n"
    text += f"\n<b>Total In:</b> GHS {total_in/100:,.2f}\n"
    text += f"<b>Total Out:</b> GHS {total_out/100:,.2f}\n"
    text += f"<b>Net:</b> GHS {(total_in-total_out)/100:,.2f}"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="affiliate_dashboard")]]))


async def show_affiliate_referrals(chat_id, bot):
    aff = await get_affiliate(chat_id)
    if aff is None:
        await bot.send_message(chat_id=chat_id, text="Affiliate not found.")
        return
    refs = list(referrals_col.find({"affiliate_id": aff["_id"]}).sort("created_at", DESCENDING).limit(15)) if referrals_col is not None else []
    if not refs:
        await bot.send_message(chat_id=chat_id, text="👥 <b>Your Referrals</b>\n\nNo referrals yet.", parse_mode="HTML")
        return
    text = "👥 <b>Your Referrals</b>\n\n"
    for ref in refs:
        customer_id = ref.get("customer_telegram_id")
        active = bool(users_col and users_col.count_documents({"telegram_id": customer_id, "is_active": True}))
        lp = ref.get("last_payment", {})
        commission = int(lp.get("commission_paid_minor", 0))
        amount = int(lp.get("amount", 0))
        text += f"User: <code>{customer_id}</code> {'🟢 Active' if active else '🔴 Inactive'}\n"
        text += f"  Channel: {html.escape(str(ref.get('customer_channel','N/A')).upper())}\n"
        if lp:
            text += f"  Last Pay: GHS {amount/100:,.2f} | Commission: GHS {commission/100:,.2f}\n"
        text += "\n"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="affiliate_dashboard")]]))


async def handle_withdrawal_request(chat_id, bot):
    aff = await get_affiliate(chat_id)
    if aff is None:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate.")
        return
    if withdrawals_col is None:
        await bot.send_message(chat_id=chat_id, text="Withdrawals are temporarily unavailable.")
        return
    if withdrawals_col.count_documents({"affiliate_id": aff["_id"], "status": {"$in": ["pending", "processing"]}}):
        await bot.send_message(chat_id=chat_id, text="⚠️ You already have a pending withdrawal. Please wait until it is completed.")
        return
    last = withdrawals_col.find_one({"affiliate_id": aff["_id"], "status": "paid"}, sort=[("processed_at", DESCENDING)])
    if last and WITHDRAWAL_COOLDOWN_DAYS > 0 and last.get("processed_at") and last["processed_at"] > datetime.utcnow() - timedelta(days=WITHDRAWAL_COOLDOWN_DAYS):
        await bot.send_message(chat_id=chat_id, text=f"⏳ Withdrawals are limited to once every {WITHDRAWAL_COOLDOWN_DAYS} days. Please try again later.")
        return
    _, _, _, available = wallet_values(aff)
    if available < MIN_WITHDRAWAL_MINOR:
        await bot.send_message(chat_id=chat_id, text=f"❌ Minimum withdrawal is GHS {MIN_WITHDRAWAL_MINOR/100:,.2f}. Your available balance is GHS {available/100:,.2f}.")
        return
    if available <= 0:
        await bot.send_message(chat_id=chat_id, text="❌ You have no available GHS balance to withdraw.")
        return
    kb = []
    for amount in (50, 100, 250, 500, 1000):
        minor = amount * 100
        if minor <= available:
            kb.append([InlineKeyboardButton(f"Withdraw GHS {amount}", callback_data=f"withdraw_confirm:{minor}")])
    kb.append([InlineKeyboardButton(f"💰 Withdraw Full Balance (GHS {available/100:,.2f})",
                                    callback_data=f"withdraw_confirm:{available}")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_dashboard")])
    await bot.send_message(
        chat_id=chat_id,
        text=f"💸 <b>Request Withdrawal</b>\n\nAvailable: <code>GHS {available/100:,.2f}</code>\n\nSelect an amount:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb)
    )


async def process_withdrawal_confirmation(chat_id, amount_minor, bot):
    if withdrawals_col is None or affiliates_col is None:
        return
    try:
        amount_minor = int(amount_minor)
    except (ValueError, TypeError):
        return
    if amount_minor < MIN_WITHDRAWAL_MINOR:
        await bot.send_message(chat_id=chat_id, text=f"❌ Minimum withdrawal is GHS {MIN_WITHDRAWAL_MINOR/100:,.2f}.")
        return
    aff = await get_affiliate(chat_id)
    if aff is None:
        return

    # Atomic balance reservation prevents double-spend from concurrent clicks.
    now = datetime.utcnow()
    lock_until = now + timedelta(minutes=2)
    locked = affiliates_col.find_one_and_update(
        {
            "_id": aff["_id"],
            "$expr": {
                "$gte": [
                    {"$subtract": [
                        {"$subtract": [
                            {"$ifNull": ["$wallet_earned_minor", 0]},
                            {"$ifNull": ["$wallet_withdrawn_minor", 0]}
                        ]},
                        {"$ifNull": ["$wallet_reserved_minor", 0]}
                    ]},
                    amount_minor
                ]
            },
            "$or": [
                {"withdrawal_lock_until": {"$exists": False}},
                {"withdrawal_lock_until": {"$lte": now}}
            ]
        },
        {"$inc": {"wallet_reserved_minor": amount_minor}, "$set": {"withdrawal_lock_until": lock_until}}
    )
    if not locked:
        await bot.send_message(chat_id=chat_id, text="⚠️ Balance changed or a withdrawal is already being created. Please try again.")
        return

    try:
        withdrawal_doc = {
            "affiliate_id": aff["_id"], "telegram_id": chat_id,
            "username": aff.get("username", ""), "full_name": aff.get("full_name", ""),
            "ref_code": aff.get("ref_code", ""), "amount_minor": amount_minor,
            "amount": amount_minor / 100, "currency": "GHS",
            "payout_method": aff.get("payout_method", "bank"),
            "payout_details": {"details": aff.get("manual_payout_details", "")},
            "status": "pending", "admin_approved": False, "admin_notes": "",
            "payout_reference": None,
            "created_at": now, "processed_at": None
        }
        result = withdrawals_col.insert_one(withdrawal_doc)
        withdrawals_col.update_one({"_id": result.inserted_id}, {"$set": {"id": str(result.inserted_id)}})
        affiliates_col.update_one({"_id": aff["_id"]}, {"$unset": {"withdrawal_lock_until": ""}})
    except Exception:
        affiliates_col.update_one({"_id": aff["_id"], "$expr": {"$gte": [{"$ifNull": ["$wallet_reserved_minor", 0]}, amount_minor]}},
                                  {"$inc": {"wallet_reserved_minor": -amount_minor}, "$unset": {"withdrawal_lock_until": ""}})
        raise

    await bot.send_message(chat_id=chat_id,
        text=f"✅ <b>Withdrawal Request Submitted</b>\n\nAmount: GHS {amount_minor/100:,.2f}\nStatus: Pending admin approval.",
        parse_mode="HTML")
    try:
        payout_text = f"Details: {aff.get('manual_payout_details', 'N/A')}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ MARK AS PAID", callback_data=f"admin_withdraw_approve:{result.inserted_id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"admin_withdraw_reject:{result.inserted_id}")
        ]])
        await bot.send_message(chat_id=ADMIN_TELEGRAM_ID,
            text=(f"🚨 <b>New Affiliate Withdrawal</b>\n\n"
                  f"Affiliate: {html.escape(str(aff.get('full_name','N/A')))} (@{html.escape(str(aff.get('username','N/A')))})\n"
                  f"Code: <code>{html.escape(str(aff.get('ref_code','')))}</code>\n"
                  f"Amount: <b>GHS {amount_minor/100:,.2f}</b>\n"
                  f"Method: {html.escape(str(aff.get('payout_method','N/A')).upper())}\n{html.escape(payout_text)}\n\n<b>Action:</b> Send the money manually, then tap <b>MARK AS PAID</b>."),
            parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Failed to notify admin: %s", e)


async def show_payout_info(chat_id, bot):
    aff = await get_affiliate(chat_id)
    if not aff:
        return
    method = "Bank Transfer" if aff.get("payout_method") == "bank" else "Mobile Money"
    details = aff.get("manual_payout_details", "Not provided")
    text = (
        f"💳 <b>Payout Details</b>\n\n"
        f"Method: {html.escape(method)}\n"
        f"Details: {html.escape(str(details))}\n\n"
        "Payouts are sent manually by JAY Trading Hub. Paystack is used only for customer subscription payments."
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


# ==============================================================================
# SUBSCRIPTION MANAGEMENT
# ==============================================================================
async def generate_single_use_invite(channel_id: str):
    bot = Bot(token=BOT_TOKEN)
    try:
        link = await bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            expire_date=datetime.utcnow() + timedelta(minutes=30),
            name=f"paid-{secrets.token_hex(4)}"
        )
        return link.invite_link
    except Exception as e:
        logger.error(f"Invite link generation failed for {channel_id}: {e}")
        return None

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
    if users_col is None or leads_col is None:
        logger.error("DB not available")
        return {"error": "DB not connected"}

    now = datetime.utcnow()
    results = {"reminders_sent": 0, "users_kicked": 0, "leads_followed": 0, "errors": []}

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
# LIFESPAN & FASTAPI APP INITIALIZATION
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [name for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
        "PAYSTACK_SECRET_KEY": PAYSTACK_SECRET,
        "MONGO_URI": MONGO_URI,
        "ADMIN_API_KEY": ADMIN_API_KEY,
        "TELEGRAM_WEBHOOK_SECRET": TELEGRAM_WEBHOOK_SECRET,
    }.items() if not value]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    if TELEGRAM_WEBHOOK_SECRET:
        await bot.set_webhook(url=webhook_target, secret_token=TELEGRAM_WEBHOOK_SECRET)
    else:
        logger.warning("TELEGRAM_WEBHOOK_SECRET is not set -- webhook is unauthenticated. Set it in Render.")
        await bot.set_webhook(url=webhook_target)
    logger.info(f"Webhook set: {webhook_target}")

    if not ADMIN_API_KEY:
        logger.warning("ADMIN_API_KEY is not set -- all /admin and /cron endpoints are locked out (fail-closed). Set it in Render.")

    asyncio.create_task(scheduler_loop())
    yield
    await telegram_app.stop()

# INITIALIZE FASTAPI BEFORE ALL ROUTE DEFINITIONS BELOW
app = FastAPI(lifespan=lifespan)
try:
    mini_origin = MINI_APP_URL.split("://", 1)[1].split("/", 1)[0]
    mini_origin = MINI_APP_URL.split("/", 3)[0] + "//" + mini_origin
except Exception:
    mini_origin = MINI_APP_URL.rstrip("/")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[mini_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Key", "X-Paystack-Signature", "X-Telegram-Bot-Api-Secret-Token", "X-Telegram-Init-Data"],
)

# ==============================================================================
# PUBLIC ENDPOINTS
# ==============================================================================

@app.get("/")
async def health():
    return {
        "status": "active",
        "mongodb": "connected" if db is not None else "disconnected",
        "service": "Jay Empire VIP + Affiliate (Paystack customer payments + Manual payouts)",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health/db")
async def health_db():
    if db is None:
        return JSONResponse({"status": "unhealthy"}, status_code=503)
    try:
        db.command("ping")
        return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error("MongoDB health check failed: %s", e)
        return JSONResponse({"status": "unhealthy"}, status_code=503)

@app.get("/api/plans")
async def api_plans():
    return {"plans": PLANS, "currency_rates": CURRENCY_RATES, "payment_currencies": PAYMENT_CURRENCIES}

class InitiatePaymentRequest(BaseModel):
    init_data: str
    channel_type: str
    plan_key: str
    currency: str


@app.post("/api/initiate-payment")
async def api_initiate_payment(payload: InitiatePaymentRequest):
    tg = validate_telegram_init_data(payload.init_data)
    telegram_id = tg["telegram_id"]

    plan = PLANS_BY_KEY.get(payload.plan_key)
    if plan is None:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if payload.channel_type not in ("gold", "fx"):
        raise HTTPException(status_code=400, detail="Invalid channel")

    # This Ghana Paystack account should only expose currencies actually
    # enabled for the merchant. Default is GHS; expand only after enabling
    # the corresponding currency in Paystack.
    if payload.currency not in PAYMENT_CURRENCIES:
        raise HTTPException(status_code=400, detail="Currency not available")

    if users_col is None or payment_intents_col is None:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    existing = users_col.find_one(
        {"telegram_id": telegram_id, "channel_type": payload.channel_type}
    )

    if existing and existing.get("lifetime"):
        raise HTTPException(status_code=400, detail="Lifetime access is already active for this channel.")

    if plan.get("is_test") and existing and existing.get("test_used"):
        raise HTTPException(status_code=400, detail="Test phase already used for this channel. Please choose a paid plan.")

    # Prevent repeated initialization spam while a previous checkout is still open.
    recent_cutoff = datetime.utcnow() - timedelta(minutes=15)
    recent = payment_intents_col.find_one({
        "telegram_id": telegram_id,
        "status": "pending",
        "created_at": {"$gte": recent_cutoff}
    })
    if recent and recent.get("access_code"):
        return {
            "access_code": recent["access_code"],
            "authorization_url": recent.get("authorization_url"),
            "reference": recent["reference"]
        }

    rate = CURRENCY_RATES[payload.currency]
    amount_minor = int(round(plan["usd"] * rate * 100))
    reference = f"JAY-{secrets.token_hex(8).upper()}"
    email = f"tg_{telegram_id}@jayempire.com"

    # Referral attribution is server-side. The browser cannot choose a referrer.
    lead = leads_col.find_one({"telegram_id": telegram_id}) if leads_col is not None else None
    customer = customers_col.find_one({"telegram_id": telegram_id}) if customers_col is not None else None
    ref_code = (customer or {}).get("referred_by") or (lead or {}).get("referred_by")
    affiliate = None
    if ref_code and affiliates_col is not None:
        affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
        if affiliate and affiliate.get("telegram_id") == telegram_id:
            affiliate = None
            ref_code = None

    paid_purchase_count = int((customer or {}).get("paid_purchase_count", 0))
    is_renewal = paid_purchase_count > 0

    payment_doc = {
        "reference": reference,
        "telegram_id": telegram_id,
        "channel_type": payload.channel_type,
        "plan_key": plan["key"],
        "days": plan["days"],
        "is_test": bool(plan.get("is_test")),
        "is_renewal": is_renewal,
        "currency": payload.currency,
        "amount_minor": amount_minor,
        "ref_code": ref_code,
        "affiliate_id": affiliate["_id"] if affiliate else None,
        "status": "pending",
        "created_at": datetime.utcnow(),
        "fulfilled_at": None,
        "access_code": None,
        "authorization_url": None,
    }

    # Persist the internal payment intent before calling Paystack. This prevents a fast
    # webhook from arriving before our database knows the payment reference.
    try:
        payment_intents_col.insert_one(payment_doc)
    except DuplicateKeyError:
        existing_intent = payment_intents_col.find_one({"reference": reference})
        if existing_intent and existing_intent.get("access_code"):
            return {"access_code": existing_intent["access_code"], "reference": reference}
        raise HTTPException(status_code=500, detail="Could not record payment.")

    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(
                "https://api.paystack.co/transaction/initialize",
                json={
                    "email": email,
                    "amount": amount_minor,
                    "currency": payload.currency,
                    "reference": reference,
                    "metadata": {
                        "payment_reference": reference,
                        "telegram_id": telegram_id,
                    },
                },
                headers=get_paystack_headers(),
                timeout=15.0
            )
            data = res.json()
        except httpx.HTTPError:
            payment_intents_col.update_one({"reference": reference}, {"$set": {"status": "failed", "failed_at": datetime.utcnow()}})
            raise HTTPException(status_code=502, detail="Payment provider unavailable. Please try again.")

    if not data.get("status") or not data.get("data", {}).get("access_code"):
        logger.error("Paystack initialize failed: %s", data)
        payment_intents_col.update_one({"reference": reference}, {"$set": {"status": "failed", "failed_at": datetime.utcnow()}})
        raise HTTPException(status_code=502, detail="Could not start payment. Please try again.")

    access_code = data["data"]["access_code"]
    authorization_url = data["data"].get("authorization_url")
    payment_intents_col.update_one(
        {"reference": reference},
        {"$set": {
            "access_code": access_code,
            "authorization_url": authorization_url,
            "paystack_initialized_at": datetime.utcnow()
        }}
    )

    return {"access_code": access_code, "authorization_url": authorization_url, "reference": reference}


@app.get("/api/payment-status")
async def api_payment_status(reference: str, x_telegram_init_data: Optional[str] = Header(None)):
    tg = validate_telegram_init_data(x_telegram_init_data or "")
    if payment_intents_col is None:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    intent = payment_intents_col.find_one(
        {"reference": reference, "telegram_id": tg["telegram_id"]},
        {"_id": 0, "status": 1, "reference": 1, "fulfilled_at": 1}
    )
    if not intent:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {
        "status": intent.get("status", "pending"),
        "reference": intent["reference"],
    }

# ==============================================================================
# TELEGRAM WEBHOOK
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    if TELEGRAM_WEBHOOK_SECRET:
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    data = await request.json()

    if "callback_query" in data:
        try:
            update = Update.de_json(data, telegram_app.bot)
            await telegram_app.process_update(update)
        except Exception as e:
            logger.exception("Telegram callback webhook processing failed: %s", e)
            # Return 200 so Telegram does not repeatedly redeliver a broken callback.
        return {"status": "ok"}

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]

        if text.startswith("/"):
            update = Update.de_json(data, telegram_app.bot)
            await telegram_app.process_update(update)
            return {"status": "ok"}

        state = get_affiliate_onboarding(chat_id)
        if state:
            step = state.get("step")
            bot = Bot(token=BOT_TOKEN)

            if step == "awaiting_affiliate_details":
                raw = text.strip()
                parts = re.split(r"\s*[-–—]\s*", raw, maxsplit=1)
                if len(parts) != 2:
                    parts = re.split(r"\s*\|\s*", raw, maxsplit=1)
                if len(parts) != 2:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="Please use this exact format:\n\nFull Name - Mobile Money Number\n\nExample: John Doe - 0241234567"
                    )
                    return {"status": "ok"}

                full_name = parts[0].strip()
                phone_raw = parts[1].strip()
                phone_digits = "".join(ch for ch in phone_raw if ch.isdigit())
                if len(phone_digits) == 12 and phone_digits.startswith("233"):
                    phone_digits = "0" + phone_digits[3:]
                if not re.fullmatch(r"0[0-9]{9}", phone_digits):
                    await bot.send_message(
                        chat_id=chat_id,
                        text="Please enter a valid Ghana Mobile Money number, for example 0241234567."
                    )
                    return {"status": "ok"}
                if len(full_name) < 2:
                    await bot.send_message(chat_id=chat_id, text="Please enter your full name.")
                    return {"status": "ok"}

                data_for_state = {
                    "full_name": full_name,
                    "payout_details_input": phone_digits,
                    "payout_method": "momo",
                }
                save_affiliate_onboarding(chat_id, "awaiting_confirmation", data_for_state)
                await show_affiliate_confirmation(chat_id, bot)
                return {"status": "ok"}


    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

# ==============================================================================
# PAYSTACK WEBHOOK
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
            reference = data.get("reference", "unknown")

            verified = await verify_paystack_transaction(reference)
            if verified is None or verified.get("status") != "success":
                logger.warning(f"Transaction {reference} failed verification, not fulfilling.")
                return {"status": "unverified"}
            if verified.get("amount") != data.get("amount") or verified.get("currency") != data.get("currency"):
                logger.warning(f"Amount/currency mismatch for {reference}")
                return {"status": "amount_mismatch"}

            if payment_intents_col is None:
                return JSONResponse({"status": "error"}, status_code=503)

            intent = payment_intents_col.find_one({"reference": reference})
            if intent is None:
                logger.warning(f"No internal payment intent for {reference}; refusing fulfillment.")
                return {"status": "ignored"}

            if intent.get("status") == "fulfilled":
                return {"status": "already_processed"}

            if intent.get("amount_minor") != verified.get("amount") or intent.get("currency") != verified.get("currency"):
                logger.warning(f"Internal payment mismatch for {reference}")
                return {"status": "amount_mismatch"}

            now = datetime.utcnow()
            stale = now - timedelta(minutes=5)
            claimed = payment_intents_col.find_one_and_update(
                {
                    "reference": reference,
                    "$or": [
                        {"status": "pending"},
                        {"status": "processing", "processing_started_at": {"$lte": stale}},
                    ],
                },
                {"$set": {"status": "processing", "processing_started_at": now}},
                return_document=ReturnDocument.AFTER,
            )
            if claimed is None:
                latest = payment_intents_col.find_one({"reference": reference}, {"status": 1})
                if latest and latest.get("status") == "fulfilled":
                    return {"status": "already_processed"}
                return {"status": "already_processing"}

            intent = claimed
            meta = data.get("metadata", {}) or {}

            tg_id = intent["telegram_id"]
            channel_type = intent["channel_type"]
            days = int(intent.get("days") or 0)
            ref_code = intent.get("ref_code")
            is_test = bool(intent.get("is_test", False))
            customer_record = customers_col.find_one({"telegram_id": tg_id}) if customers_col is not None else None
            # Recompute renewal status from the server-side customer history.
            # Metadata sent by a browser is never trusted for commission tiering.
            is_renewal = (not is_test) and int((customer_record or {}).get("paid_purchase_count", 0)) > 0

            if not tg_id or tg_id == 0:
                return {"status": "ignored"}

            existing = None
            if users_col is not None:
                existing = users_col.find_one({"telegram_id": tg_id, "channel_type": channel_type})

            if is_test:
                expires = (existing or {}).get("expires_at")
                if not expires or expires <= now:
                    expires = now + timedelta(days=days)
            elif intent.get("plan_key") == "lifetime":
                expires = None
            elif existing and existing.get("is_active") and existing.get("expires_at") and existing["expires_at"] > now:
                expires = existing["expires_at"] + timedelta(days=days)
            else:
                expires = now + timedelta(days=days)

            if users_col is not None:
                update_fields = {
                    "telegram_id": tg_id,
                    "channel_type": channel_type,
                    "purchased_at": now,
                    "expires_at": expires,
                    "lifetime": intent.get("plan_key") == "lifetime",
                    "is_active": True,
                    "reminder_sent": False,
                    "last_reference": reference,
                    "amount_paid": data.get("amount"),
                    "currency": data.get("currency"),
                    "customer_email": data.get("customer", {}).get("email"),
                    "paystack_reference": reference,
                    "referred_by": ref_code
                }
                if is_test:
                    update_fields["test_used"] = True

                users_col.update_one(
                    {"telegram_id": tg_id, "channel_type": channel_type},
                    {"$set": update_fields},
                    upsert=True
                )

                if leads_col is not None:
                    leads_col.update_one(
                        {"telegram_id": tg_id},
                        {"$set": {"converted": True, "converted_at": now, "converted_channel": channel_type}}
                    )

                if (not is_test) and ref_code and affiliates_col is not None and referrals_col is not None:
                    affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
                    if affiliate is not None and affiliate["telegram_id"] == tg_id:
                        logger.warning(f"Self-referral blocked: affiliate {ref_code} tried to refer themselves ({tg_id}).")
                    elif affiliate is not None:
                        rate = COMMISSION_RENEWAL if is_renewal else COMMISSION_FIRST_SALE
                        amount = int(data.get("amount", 0))
                        commission_minor = (amount * rate) // 100

                        referrals_col.update_one(
                            {"affiliate_id": affiliate["_id"], "customer_telegram_id": tg_id},
                            {
                                "$setOnInsert": {
                                    "affiliate_id": affiliate["_id"],
                                    "ref_code": ref_code,
                                    "customer_telegram_id": tg_id,
                                    "customer_channel": channel_type,
                                    "plan_key": intent.get("plan_key", "unknown"),
                                    "created_at": now,
                                    "is_active": True
                                },
                                "$set": {
                                    "last_payment": {
                                        "amount": amount,
                                        "currency": data.get("currency"),
                                        "commission_paid_minor": commission_minor,
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

                        try:
                            inserted = False
                            if transactions_col is not None:
                                try:
                                    transactions_col.insert_one({
                                        "affiliate_id": affiliate["_id"],
                                        "type": "commission",
                                        "amount_minor": commission_minor,
                                        "currency": "GHS",
                                        "description": f"{'Renewal' if is_renewal else 'First sale'} commission from user {tg_id}",
                                        "reference": reference,
                                        "metadata": {"customer_id": tg_id, "channel": channel_type, "rate": rate},
                                        "created_at": now
                                    })
                                    inserted = True
                                except DuplicateKeyError:
                                    logger.info("Commission already recorded for %s", reference)
                            if inserted:
                                affiliates_col.update_one(
                                    {"_id": affiliate["_id"]},
                                    {"$inc": {"wallet_earned_minor": commission_minor, "total_referrals": 0 if is_renewal else 1},
                                     "$set": {"last_earning_at": now}}
                                )
                                try:
                                    await Bot(token=BOT_TOKEN).send_message(
                                        chat_id=ADMIN_TELEGRAM_ID,
                                        text=(
                                            f"💰 <b>Affiliate Commission Earned</b>\n\n"
                                            f"Affiliate: {html.escape(str(affiliate.get('full_name','N/A')))}\n"
                                            f"Code: <code>{html.escape(str(ref_code))}</code>\n"
                                            f"Customer ID: <code>{tg_id}</code>\n"
                                            f"Sale: {amount/100:,.2f} {data.get('currency','GHS')}\n"
                                            f"Commission: <b>GHS {commission_minor/100:,.2f}</b> ({rate}%)\n"
                                            f"Type: {'Renewal' if is_renewal else 'First Sale'}\n"
                                            f"Paystack Ref: <code>{html.escape(str(reference))}</code>\n\n"
                                            "The full customer payment remains in the merchant Paystack account; the commission is recorded internally for manual payout."
                                        ),
                                        parse_mode="HTML"
                                    )
                                except Exception as e:
                                    logger.error("Failed to notify admin about commission: %s", e)
                        except Exception:
                            logger.exception("Affiliate commission accounting failed for %s", reference)
                            raise

                        logger.info(f"Affiliate {ref_code} earned {rate}% = {commission_minor} GHS minor units from {tg_id}")

            # Mark the payment fulfilled only after all DB-side entitlement/accounting
            # work has completed. A second webhook cannot credit it again.
            if customers_col is not None:
                customer_update = {"$setOnInsert": {"telegram_id": tg_id, "paid_purchase_count": 0}}
                if not is_test:
                    customer_update["$inc"] = {"paid_purchase_count": 1}
                    if ref_code:
                        customer_update["$set"] = {"referred_by": ref_code}
                else:
                    customer_update["$set"] = {}
                customers_col.update_one({"telegram_id": tg_id}, customer_update, upsert=True)

            payment_intents_col.update_one(
                {"reference": reference, "status": {"$ne": "fulfilled"}},
                {"$set": {"status": "fulfilled", "fulfilled_at": now}}
            )

            channel_id = GOLD_CHANNEL_ID if channel_type == "gold" else FOREX_CHANNEL_ID
            name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
            bot = Bot(token=BOT_TOKEN)
            try:
                if is_renewal:
                    await bot.send_message(
                        chat_id=tg_id,
                        text=f"✅ PAYMENT VERIFIED!\n\nPlan: {channel_type.upper()}\nExpires: {'Lifetime' if expires is None else expires.strftime('%B %d, %Y')}\n\nYou're already in {name} — no action needed.",
                        parse_mode="HTML",
                    )
                else:
                    invite_link = await generate_single_use_invite(channel_id)
                    if invite_link:
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"Enter {name}", url=invite_link)]])
                        await bot.send_message(
                            chat_id=tg_id,
                            text=f"✅ PAYMENT VERIFIED!\n\nPlan: {channel_type.upper()}\nExpires: {'Lifetime' if expires is None else expires.strftime('%B %d, %Y')}\n\nTap below (this link works once):",
                            parse_mode="HTML",
                            reply_markup=btn
                        )
                    else:
                        await bot.send_message(
                            chat_id=tg_id,
                            text=f"✅ Payment verified, but we couldn't generate your invite link automatically. Contact @{ADMIN_USERNAME} with your payment reference: {reference}",
                            parse_mode="HTML",
                        )
            except Exception as e:
                logger.error(f"Access message failed: {e}")

        return {"status": "success"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse({"status": "error"}, status_code=500)

# ==============================================================================
# ADMIN ENDPOINTS
# ==============================================================================

@app.post("/cron/daily-check")
async def cron_daily(_: bool = Depends(verify_admin)):
    return JSONResponse({
        "status": "completed",
        "results": await run_daily_checks(),
        "timestamp": datetime.utcnow().isoformat()
    })

@app.get("/admin/users")
async def admin_users(_: bool = Depends(verify_admin)):
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
async def admin_leads(_: bool = Depends(verify_admin)):
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
async def admin_affiliates(_: bool = Depends(verify_admin)):
    if affiliates_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)
    affs = list(affiliates_col.find({}, {"_id": 0, "bank_details": 0, "mobile_money_details": 0}))
    return {
        "total": len(affs),
        "active": sum(1 for a in affs if a.get("is_active")),
        "milestone_reached": sum(1 for a in affs if a.get("milestone_notified")),
        "bank_payouts": sum(1 for a in affs if a.get("payout_method") == "bank"),
        "momo_payouts": sum(1 for a in affs if a.get("payout_method") == "momo"),
        "affiliates": affs
    }

@app.get("/admin/affiliates/detailed")
async def admin_affiliates_detailed(_: bool = Depends(verify_admin)):
    if affiliates_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)

    affs = list(affiliates_col.find({}))
    detailed = []
    for aff in affs:
        aff_data = {
            "telegram_id": aff.get("telegram_id"),
            "username": aff.get("username"),
            "full_name": aff.get("full_name"),
            "ref_code": aff.get("ref_code"),
            "country": aff.get("country_name"),
            "payout_method": aff.get("payout_method"),
            "total_earnings": wallet_values(aff)[0] / 100,
            "total_withdrawn": wallet_values(aff)[1] / 100,
            "reserved_balance": wallet_values(aff)[2] / 100,
            "available_balance": wallet_values(aff)[3] / 100,
            "total_referrals": aff.get("total_referrals", 0),
            "is_active": aff.get("is_active"),
            "created_at": aff.get("created_at").isoformat() if aff.get("created_at") else None,
            "payout_details": aff.get("bank_details") if aff.get("payout_method") == "bank" else aff.get("mobile_money_details")
        }
        detailed.append(aff_data)

    return {"total": len(detailed), "affiliates": detailed}

@app.get("/admin/dashboard")
async def admin_dashboard(_: bool = Depends(verify_admin)):
    if users_col is None or leads_col is None or affiliates_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)

    now = datetime.utcnow()
    total_earnings = sum(wallet_values(a)[0] for a in affiliates_col.find()) / 100

    pending_withdrawals = 0
    total_withdrawn = 0
    if withdrawals_col is not None:
        pending_withdrawals = withdrawals_col.count_documents({"status": "pending"})
        total_withdrawn = sum(int(w.get("amount_minor", round(float(w.get("amount",0))*100))) for w in withdrawals_col.find({"status": "paid"})) / 100

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
            "total_payouts": round(total_earnings, 2),
            "by_country": {country: affiliates_col.count_documents({"country": country}) for country in AFRICA_COUNTRIES.keys()},
            "by_payout_method": {
                "bank": affiliates_col.count_documents({"payout_method": "bank"}),
                "momo": affiliates_col.count_documents({"payout_method": "momo"})
            }
        },
        "withdrawals": {
            "pending_count": pending_withdrawals,
            "total_processed": round(total_withdrawn, 2)
        },
        "timestamp": now.isoformat()
    }

@app.get("/admin/withdrawals")
async def admin_get_withdrawals(status: str = "pending", _: bool = Depends(verify_admin)):
    if withdrawals_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)

    query = {}
    if status != "all":
        query["status"] = status

    withdrawals = list(withdrawals_col.find(query).sort("created_at", DESCENDING))

    enriched = []
    for w in withdrawals:
        aff = None
        if affiliates_col is not None:
            aff = affiliates_col.find_one({"_id": w["affiliate_id"]})

        enriched.append({
            "id": str(w["_id"]),
            "affiliate_name": w.get("full_name", "N/A"),
            "username": w.get("username", "N/A"),
            "ref_code": w.get("ref_code", "N/A"),
            "amount": int(w.get("amount_minor", round(float(w.get("amount", 0))*100))) / 100,
            "payout_method": w.get("payout_method", "N/A"),
            "payout_details": w.get("payout_details", {}),
            "status": w.get("status", "N/A"),
            "admin_notes": w.get("admin_notes", ""),
            "created_at": w.get("created_at").isoformat() if w.get("created_at") else None,
            "processed_at": w.get("processed_at").isoformat() if w.get("processed_at") else None,
            "affiliate_total_earnings": aff.get("total_earnings", 0) if aff else 0,
            "affiliate_total_withdrawn": aff.get("total_withdrawn", 0) if aff else 0
        })

    return {
        "status": status,
        "count": len(enriched),
        "total_amount": round(sum(w["amount"] for w in enriched), 2),
        "withdrawals": enriched
    }

async def approve_withdrawal_internal(withdrawal_id: str, notes: str = "", payout_reference: str = ""):
    """Finalize a manually paid affiliate withdrawal. Paystack is never called here."""
    if withdrawals_col is None or affiliates_col is None:
        return {"ok": False, "status_code": 503, "error": "DB offline"}
    try:
        oid = ObjectId(withdrawal_id)
    except Exception:
        return {"ok": False, "status_code": 400, "error": "Invalid withdrawal ID"}
    now = datetime.utcnow()
    withdrawal = withdrawals_col.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "paid", "admin_approved": True, "admin_notes": notes or "Marked as paid by admin",
                  "payout_reference": (payout_reference or "").strip() or None, "processed_at": now, "paid_at": now, "paid_by": ADMIN_TELEGRAM_ID}},
        return_document=ReturnDocument.AFTER)
    if withdrawal is None:
        return {"ok": False, "status_code": 400, "error": "Withdrawal not found or already processed"}
    amount_minor = int(withdrawal.get("amount_minor", 0))
    affiliate = affiliates_col.find_one({"_id": withdrawal["affiliate_id"]})
    if affiliate is None:
        return {"ok": False, "status_code": 404, "error": "Affiliate not found"}
    changed = affiliates_col.update_one(
        {"_id": affiliate["_id"], "$expr": {"$gte": [{"$ifNull": ["$wallet_reserved_minor", 0]}, amount_minor]}},
        {"$inc": {"wallet_reserved_minor": -amount_minor, "wallet_withdrawn_minor": amount_minor}, "$set": {"last_withdrawal_at": now}})
    if changed.modified_count != 1:
        withdrawals_col.update_one({"_id": oid, "status": "paid"}, {"$set": {"status": "payment_error", "admin_notes": "Could not finalize wallet reservation"}})
        return {"ok": False, "status_code": 409, "error": "Could not finalize affiliate balance"}
    try:
        if transactions_col is not None:
            transactions_col.insert_one({"affiliate_id": affiliate["_id"], "type": "withdrawal", "amount_minor": amount_minor,
                "amount": amount_minor/100, "currency": "GHS", "description": "Manual affiliate payout completed",
                "reference": f"withdrawal:{withdrawal['_id']}", "metadata": {"payout_reference": (payout_reference or "").strip() or None, "paid_by": ADMIN_TELEGRAM_ID}, "created_at": now})
    except DuplicateKeyError:
        pass
    try:
        ref = (payout_reference or "").strip()
        await Bot(token=BOT_TOKEN).send_message(chat_id=affiliate["telegram_id"],
            text=(f"💸 <b>Withdrawal Paid</b>\n\nAmount: GHS {amount_minor/100:,.2f}\n"
                  "Your payout has been manually processed by JAY Trading Hub.\n"
                  + (f"Reference: <code>{html.escape(ref)}</code>\n" if ref else "")
                  + "Thank you for being part of the affiliate program."), parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to notify affiliate: %s", e)
    return {"ok": True, "status": "paid", "withdrawal_id": withdrawal_id, "amount": amount_minor/100}

@app.post("/admin/withdrawals/{withdrawal_id}/approve")
async def admin_approve_withdrawal(withdrawal_id: str, notes: str = "", payout_reference: str = "", _: bool = Depends(verify_admin)):
    result = await approve_withdrawal_internal(withdrawal_id, notes, payout_reference)
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, status_code=result["status_code"])
    return result


async def reject_withdrawal_internal(withdrawal_id: str, notes: str = ""):
    if withdrawals_col is None or affiliates_col is None:
        return {"ok": False, "status_code": 503, "error": "DB offline"}
    try:
        oid = ObjectId(withdrawal_id)
    except Exception:
        return {"ok": False, "status_code": 400, "error": "Invalid withdrawal ID"}

    now = datetime.utcnow()
    withdrawal = withdrawals_col.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "rejected", "admin_approved": False,
                  "admin_notes": notes, "processed_at": now}},
        return_document=ReturnDocument.AFTER
    )
    if withdrawal is None:
        return {"ok": False, "status_code": 400, "error": "Withdrawal not found or already processed"}

    amount_minor = int(withdrawal.get("amount_minor", round(float(withdrawal.get("amount", 0))*100)))
    affiliates_col.update_one(
        {"_id": withdrawal["affiliate_id"]},
        {"$inc": {"wallet_reserved_minor": -amount_minor}}
    )
    try:
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=withdrawal["telegram_id"],
            text=(f"❌ <b>Withdrawal Rejected</b>\n\nAmount: GHS {amount_minor/100:,.2f}\n"
                  f"Reason: {html.escape(notes or 'Not specified')}"),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Failed to notify affiliate: %s", e)
    return {"ok": True, "status": "rejected", "withdrawal_id": withdrawal_id, "amount": amount_minor/100}


@app.post("/admin/withdrawals/{withdrawal_id}/reject")
async def admin_reject_withdrawal(withdrawal_id: str, notes: str = "", _: bool = Depends(verify_admin)):
    result = await reject_withdrawal_internal(withdrawal_id, notes)
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, status_code=result["status_code"])
    return result


# ==============================================================================
# RUN
# ==============================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
