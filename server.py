"""
server.py -- Jay Empire VIP Backend + Affiliate System
With Paystack Split Payments, Auto-Payouts, and Milestone Rewards
Africa-Wide: Bank Transfer + Mobile Money (Momo) Support
Withdrawal Request System + Affiliate Portal + Admin Dashboard

SECURITY REVISION NOTES:
- Checkout price locked server-side via /api/initiate-payment.
- Fresh, single-use Telegram invite link generated post-payment.
- All /admin/* and /cron/* endpoints require X-Admin-Key header.
- Telegram webhook verifies secret_token header.
- Country-restricted automatic Paystack payouts with fallback handling.
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
import re
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.server_api import ServerApi
from bson import ObjectId
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
MINIMUM_WITHDRAWAL = 0

# ==============================================================================
# AFRICA PAYOUT CONFIGURATION
# ==============================================================================
AFRICA_COUNTRIES = {
    "ghana": {"name": "Ghana", "currency": "GHS", "flag": "🇬🇭"},
    "nigeria": {"name": "Nigeria", "currency": "NGN", "flag": "🇳🇬"},
    "kenya": {"name": "Kenya", "currency": "KES", "flag": "🇰🇪"},
    "south_africa": {"name": "South Africa", "currency": "ZAR", "flag": "🇿🇦"},
}
MOMO_ELIGIBLE_COUNTRIES = {"ghana", "kenya"}

# ==============================================================================
# MONGODB
# ==============================================================================
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
        webhook_events_col.create_index([("reference", ASCENDING)], unique=True)

        return client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col, webhook_events_col

    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        return None, None, None, None, None, None, None, None, None

_mongo_result = init_mongodb()
if len(_mongo_result) == 9:
    mongo_client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col, webhook_events_col = _mongo_result
else:
    mongo_client = db = users_col = leads_col = affiliates_col = referrals_col = withdrawals_col = transactions_col = webhook_events_col = None

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
# PAYSTACK HELPERS
# ==============================================================================
def get_paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}

def clean_phone_number(raw_number: str, country_key: str) -> str:
    cleaned = re.sub(r"\D", "", raw_number)
    country_codes = {"ghana": "233", "kenya": "254", "nigeria": "234", "south_africa": "27"}
    cc = country_codes.get(country_key, "")
    if cc and not cleaned.startswith(cc):
        if cleaned.startswith("0"):
            cleaned = cc + cleaned[1:]
        elif len(cleaned) <= 10:
            cleaned = cc + cleaned
    return cleaned

async def create_paystack_transfer_recipient(name, account_number, bank_code, currency, recipient_type="nuban"):
    payload = {
        "type": recipient_type,
        "name": name,
        "account_number": account_number,
        "bank_code": bank_code,
        "currency": currency,
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transferrecipient",
            json=payload,
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()
        if data.get("status"):
            return data["data"]["recipient_code"]
        logger.error(f"Transfer recipient creation FAILED: {data} | payload_sent={payload}")
        return None

async def initiate_paystack_transfer(amount, recipient_code, reason, reference=None):
    if reference is None:
        reference = f"JAYWTH-{secrets.token_hex(8).upper()}"

    payload = {
        "source": "balance",
        "amount": amount,
        "recipient": recipient_code,
        "reason": reason,
        "reference": reference
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transfer",
            json=payload,
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()
        if data.get("status"):
            return {
                "success": True,
                "transfer_code": data["data"]["transfer_code"],
                "reference": reference,
                "status": data["data"]["status"]
            }
        logger.error(f"Transfer initiation failed: {data}")
        return {"success": False, "error": data.get("message", "Unknown error")}

async def get_paystack_bank_list(country="ghana", account_type="nuban", currency=None):
    params = {}
    if account_type == "mobile_money":
        params["type"] = "mobile_money"
        if currency:
            params["currency"] = currency
    else:
        params["country"] = country

    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://api.paystack.co/bank",
            params=params,
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
        "description": description,
        "reference": reference or f"TXN-{secrets.token_hex(6).upper()}",
        "metadata": metadata or {},
        "created_at": datetime.utcnow()
    }
    transactions_col.insert_one(doc)

# ==============================================================================
# TELEGRAM BOT SETUP & HANDLERS
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
# AFFILIATE CALLBACK ROUTER
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
            f"- Renewals: {COMMISSION_RENEWAL}%\n"
            f"- Lifetime tracking\n\n"
            f"<b>💸 Payout:</b> Request anytime. No minimum. Processed within 24-48hrs.\n\n"
            f"<b>🏆 Bonus:</b> {REFERRAL_MILESTONE}+ active referrals = Lifetime VIP!\n\n"
            f"Tap 'I Agree and Join' to accept terms and set up your payout method."
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "affiliate_agree":
        user_states[chat_id] = {"step": "awaiting_full_name", "data": {}}
        await bot.send_message(chat_id=chat_id, text="Step 1/5: Enter your Full Name (as on ID / account):", parse_mode="HTML")

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
        link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
        await bot.send_message(chat_id=chat_id, text=f"🔗 Your Link:\n\n<code>{link}</code>\n\nTap and hold to copy!", parse_mode="HTML")

    elif action == "back_main":
        await show_main_menu(chat_id, bot)

    elif action == "payout_method_bank":
        if chat_id in user_states:
            user_states[chat_id]["data"]["payout_method"] = "bank"
            user_states[chat_id]["step"] = "awaiting_country_selection_bank"
        await show_country_selection(chat_id, bot, "bank")

    elif action == "payout_method_momo":
        if chat_id in user_states:
            user_states[chat_id]["data"]["payout_method"] = "momo"
            user_states[chat_id]["step"] = "awaiting_country_selection_momo"
        await show_country_selection(chat_id, bot, "momo")

    elif action.startswith("country:"):
        parts = action.split(":", 2)
        if len(parts) == 3:
            _, country_key, method = parts
            if country_key != PAYSTACK_ACCOUNT_COUNTRY:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Automated payouts aren't available for {AFRICA_COUNTRIES.get(country_key, {}).get('name', country_key)} yet. Contact @{ADMIN_USERNAME} for manual arrangement."
                )
                return

            if method == "momo" and country_key not in MOMO_ELIGIBLE_COUNTRIES:
                await bot.send_message(chat_id=chat_id, text="⚠️ Mobile Money payouts aren't supported for this country. Please choose Bank Transfer.")
                return

            if chat_id in user_states:
                user_states[chat_id]["data"]["country"] = country_key
                user_states[chat_id]["data"]["country_name"] = AFRICA_COUNTRIES.get(country_key, {}).get("name", country_key)
                user_states[chat_id]["data"]["currency"] = AFRICA_COUNTRIES.get(country_key, {}).get("currency", "GHS")

                if method == "bank":
                    user_states[chat_id]["step"] = "awaiting_bank_selection"
                    await show_bank_selection(chat_id, bot, country_key)
                elif method == "momo":
                    user_states[chat_id]["step"] = "awaiting_momo_provider"
                    await show_momo_provider_selection(chat_id, bot, country_key)

    elif action.startswith("momo_provider:"):
        parts = action.split(":", 1)
        if len(parts) == 2:
            _, provider_code = parts
            if chat_id in user_states:
                user_states[chat_id]["data"]["momo_provider"] = provider_code
                user_states[chat_id]["data"]["momo_provider_name"] = provider_code
                user_states[chat_id]["step"] = "awaiting_momo_number"

            await bot.send_message(chat_id=chat_id, text=f"Step 4/5: Enter your {provider_code} Mobile Money number:", parse_mode="HTML")

    elif action.startswith("withdraw_confirm:"):
        try:
            amount = int(action.split(":")[1])
            await process_withdrawal_confirmation(chat_id, amount, bot)
        except Exception as e:
            logger.error(f"Invalid withdrawal callback: {e}")

    elif action == "withdraw_cancel":
        await show_affiliate_dashboard(chat_id, bot)

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
    if not aff:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate yet.")
        return

    ref_link = f"https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}"
    total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]}) if referrals_col is not None else 0
    active_refs = referrals_col.count_documents({"affiliate_id": aff["_id"], "is_active": True}) if referrals_col is not None else 0

    available_balance = aff.get("total_earnings", 0) - aff.get("total_withdrawn", 0)
    pending_withdrawals = withdrawals_col.count_documents({"affiliate_id": aff["_id"], "status": "pending"}) if withdrawals_col is not None else 0

    dashboard = (
        f"💎 <b>Jay Empire Affiliate Dashboard</b>\n\n"
        f"🔗 <b>Referral Link:</b>\n<code>{ref_link}</code>\n\n"
        f"💰 <b>Earnings:</b>\n"
        f"  Total Earned: ${aff.get('total_earnings', 0):,.2f}\n"
        f"  Total Withdrawn: ${aff.get('total_withdrawn', 0):,.2f}\n"
        f"  <b>Available Balance: ${available_balance:,.2f}</b>\n"
        f"  Pending Withdrawals: {pending_withdrawals}\n\n"
        f"👥 <b>Referrals:</b>\n"
        f"  Total: {total_refs} | Active: {active_refs}\n\n"
        f"📊 <b>Your Code:</b> <code>{aff['ref_code']}</code>"
    )

    kb = [
        [InlineKeyboardButton("📋 View Statement", callback_data="affiliate_statement")],
        [InlineKeyboardButton("👥 My Referrals", callback_data="affiliate_referrals")],
        [InlineKeyboardButton("💸 Request Withdrawal", callback_data="request_withdrawal")],
        [InlineKeyboardButton("💳 Payout Info", callback_data="affiliate_payout_info")],
        [InlineKeyboardButton("🔗 Copy Link", callback_data=f"aff_copy:{aff['ref_code']}")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]
    ]
    await bot.send_message(chat_id=chat_id, text=dashboard, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_affiliate_statement(chat_id, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if not aff:
        return
    transactions = list(transactions_col.find({"affiliate_id": aff["_id"]}, {"_id": 0}).sort("created_at", DESCENDING).limit(20)) if transactions_col is not None else []
    if not transactions:
        await bot.send_message(chat_id=chat_id, text="📋 <b>Your Statement</b>\n\nNo transactions yet.", parse_mode="HTML")
        return

    text = "📋 <b>Your Financial Statement</b>\n\n"
    for txn in transactions:
        sign = "+" if txn["type"] == "commission" else "-"
        text += f"{txn['created_at'].strftime('%d/%m/%Y')} | {txn['type']} | <code>{sign}${txn['amount']:,.2f}</code>\n"

    kb = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="affiliate_dashboard")]]
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_affiliate_referrals(chat_id, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if not aff:
        return
    referrals = list(referrals_col.find({"affiliate_id": aff["_id"]}).sort("created_at", DESCENDING).limit(15)) if referrals_col is not None else []
    if not referrals:
        await bot.send_message(chat_id=chat_id, text="👥 <b>Your Referrals</b>\n\nNo referrals yet.", parse_mode="HTML")
        return

    text = "👥 <b>Your Referrals</b>\n\n"
    for ref in referrals:
        status = "🟢 Active" if ref.get("is_active") else "🔴 Inactive"
        text += f"User: <code>{ref['customer_telegram_id']}</code> {status}\n"

    kb = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="affiliate_dashboard")]]
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_withdrawal_request(chat_id, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if not aff:
        return

    available = aff.get("total_earnings", 0) - aff.get("total_withdrawn", 0)
    if available <= 0:
        await bot.send_message(chat_id=chat_id, text="❌ You have no available balance to withdraw.")
        return

    kb = [[InlineKeyboardButton(f"💰 Withdraw All (${available:,.2f})", callback_data=f"withdraw_confirm:{int(available * 100)}")]]
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_dashboard")])
    await bot.send_message(chat_id=chat_id, text=f"💸 Available Balance: <code>${available:,.2f}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def process_withdrawal_confirmation(chat_id, amount_cents, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if not aff:
        return

    amount = amount_cents / 100
    available = aff.get("total_earnings", 0) - aff.get("total_withdrawn", 0)
    if amount > available or amount <= 0:
        await bot.send_message(chat_id=chat_id, text="❌ Invalid amount request.")
        return

    withdrawal_doc = {
        "affiliate_id": aff["_id"],
        "telegram_id": chat_id,
        "amount": amount,
        "status": "pending",
        "created_at": datetime.utcnow()
    }
    if withdrawals_col is not None:
        withdrawals_col.insert_one(withdrawal_doc)

    log_affiliate_transaction(aff["_id"], "withdrawal_request", amount, f"Withdrawal request of ${amount:,.2f}")
    await bot.send_message(chat_id=chat_id, text="✅ <b>Withdrawal Request Submitted!</b> Processing time: 24-48 hours.", parse_mode="HTML")

async def show_payout_info(chat_id, bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col is not None else None
    if aff:
        method = aff.get("payout_method", "bank")
        await bot.send_message(chat_id=chat_id, text=f"💳 Active Payout Method: {method.upper()}", parse_mode="HTML")

async def show_country_selection(chat_id, bot, method):
    kb = [[InlineKeyboardButton(f"{v['flag']} {v['name']}", callback_data=f"country:{k}:{method}")] for k, v in AFRICA_COUNTRIES.items()]
    await bot.send_message(chat_id=chat_id, text="Step 2/5: Select payout country:", reply_markup=InlineKeyboardMarkup(kb))

async def show_bank_selection(chat_id, bot, country):
    banks = await get_paystack_bank_list(country, account_type="nuban")
    kb = [[InlineKeyboardButton(b["name"], callback_data=f"aff_bank:{b['code']}:{b['name']}")] for b in banks[:15]]
    await bot.send_message(chat_id=chat_id, text="Step 3/5: Select Bank:", reply_markup=InlineKeyboardMarkup(kb))

async def show_momo_provider_selection(chat_id, bot, country):
    currency = AFRICA_COUNTRIES.get(country, {}).get("currency", "GHS")
    providers = await get_paystack_bank_list(country, account_type="mobile_money", currency=currency)
    kb = [[InlineKeyboardButton(p["name"], callback_data=f"momo_provider:{p['code']}")] for p in providers[:10]]
    await bot.send_message(chat_id=chat_id, text="Step 3/5: Select Mobile Money Provider:", reply_markup=InlineKeyboardMarkup(kb))

# ==============================================================================
# LIFESPAN & FASTAPI ENGINE
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(url=webhook_target, secret_token=TELEGRAM_WEBHOOK_SECRET if TELEGRAM_WEBHOOK_SECRET else None)
    yield
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# ROUTE ENDPOINTS
# ==============================================================================
@app.get("/")
async def health():
    return {"status": "active", "service": "Jay Empire VIP Backend"}

@app.get("/api/plans")
async def api_plans():
    return {"plans": PLANS, "currency_rates": CURRENCY_RATES}

class InitiatePaymentRequest(BaseModel):
    telegram_id: int
    channel_type: str
    plan_key: str
    currency: str
    ref_code: Optional[str] = None

@app.post("/api/initiate-payment")
async def api_initiate_payment(payload: InitiatePaymentRequest):
    plan = PLANS_BY_KEY.get(payload.plan_key)
    rate = CURRENCY_RATES.get(payload.currency)
    if not plan or not rate:
        raise HTTPException(status_code=400, detail="Invalid request options")

    amount_minor = round(plan["usd"] * rate * 100)
    reference = f"JAY-{secrets.token_hex(8).upper()}"

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            json={
                "email": f"user_{payload.telegram_id}@jayempire.com",
                "amount": amount_minor,
                "currency": payload.currency,
                "reference": reference,
                "metadata": {
                    "telegram_id": payload.telegram_id,
                    "channel_type": payload.channel_type,
                    "plan_key": plan["key"],
                    "days": plan["days"],
                    "ref_code": payload.ref_code
                }
            },
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()
        if not data.get("status"):
            raise HTTPException(status_code=502, detail="Paystack failed")
        return {"access_code": data["data"]["access_code"], "reference": reference}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

@app.post("/paystack-webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    body = await request.body()
    expected = hmac.new(PAYSTACK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    if not x_paystack_signature or not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    if payload.get("event") == "charge.success":
        data = payload["data"]
        ref = data.get("reference")
        
        if webhook_events_col is not None:
            try:
                webhook_events_col.insert_one({"reference": ref, "processed_at": datetime.utcnow()})
            except DuplicateKeyError:
                return {"status": "already_processed"}

        meta = data.get("metadata", {})
        tg_id = meta.get("telegram_id")
        days = meta.get("days", 30)

        expires = datetime.utcnow() + timedelta(days=days)
        if users_col is not None:
            users_col.update_one(
                {"telegram_id": tg_id, "channel_type": meta.get("channel_type")},
                {"$set": {"is_active": True, "expires_at": expires}},
                upsert=True
            )

    return {"status": "success"}

# ==============================================================================
# RESTORED ADMIN & CRON ENDPOINTS
# ==============================================================================
@app.post("/cron/daily-check")
async def cron_daily(_: bool = Depends(verify_admin)):
    now = datetime.utcnow()
    expired_count = 0
    if users_col is not None:
        expired = users_col.find({"is_active": True, "expires_at": {"$lte": now}})
        for u in expired:
            users_col.update_one({"_id": u["_id"]}, {"$set": {"is_active": False}})
            expired_count += 1
    return {"status": "completed", "users_deactivated": expired_count}

@app.get("/admin/users")
async def admin_users(_: bool = Depends(verify_admin)):
    users = list(users_col.find({}, {"_id": 0})) if users_col is not None else []
    return {"total": len(users), "users": users}

@app.get("/admin/leads")
async def admin_leads(_: bool = Depends(verify_admin)):
    leads = list(leads_col.find({}, {"_id": 0})) if leads_col is not None else []
    return {"total": len(leads), "leads": leads}

@app.get("/admin/affiliates")
async def admin_affiliates(_: bool = Depends(verify_admin)):
    affs = list(affiliates_col.find({}, {"_id": 0})) if affiliates_col is not None else []
    return {"total": len(affs), "affiliates": affs}

@app.get("/admin/withdrawals")
async def admin_withdrawals(_: bool = Depends(verify_admin)):
    withdrawals = list(withdrawals_col.find().sort("created_at", DESCENDING)) if withdrawals_col is not None else []
    for w in withdrawals:
        w["_id"] = str(w["_id"])
        w["affiliate_id"] = str(w["affiliate_id"])
    return {"withdrawals": withdrawals}

@app.post("/admin/withdrawals/{withdrawal_id}/approve")
async def approve_withdrawal(withdrawal_id: str, _: bool = Depends(verify_admin)):
    if withdrawals_col is None or affiliates_col is None:
        raise HTTPException(status_code=503, detail="Database uninitialized")

    w = withdrawals_col.find_one({"_id": ObjectId(withdrawal_id), "status": "pending"})
    if not w:
        raise HTTPException(status_code=404, detail="Pending withdrawal request not found")

    withdrawals_col.update_one({"_id": w["_id"]}, {"$set": {"status": "approved", "processed_at": datetime.utcnow()}})
    affiliates_col.update_one({"_id": w["affiliate_id"]}, {"$inc": {"total_withdrawn": w["amount"]}})

    log_affiliate_transaction(w["affiliate_id"], "withdrawal_approved", w["amount"], f"Approved payout of ${w['amount']:,.2f}")
    return {"status": "approved", "withdrawal_id": withdrawal_id}

@app.post("/admin/withdrawals/{withdrawal_id}/reject")
async def reject_withdrawal(withdrawal_id: str, _: bool = Depends(verify_admin)):
    if withdrawals_col is None:
        raise HTTPException(status_code=503, detail="Database uninitialized")

    w = withdrawals_col.find_one({"_id": ObjectId(withdrawal_id), "status": "pending"})
    if not w:
        raise HTTPException(status_code=404, detail="Pending withdrawal request not found")

    withdrawals_col.update_one({"_id": w["_id"]}, {"$set": {"status": "rejected", "processed_at": datetime.utcnow()}})
    log_affiliate_transaction(w["affiliate_id"], "withdrawal_rejected", 0, f"Rejected payout request of ${w['amount']:,.2f}")
    return {"status": "rejected", "withdrawal_id": withdrawal_id}
