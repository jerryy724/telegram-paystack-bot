"""
server.py -- Jay Empire VIP Backend + Affiliate System
With Paystack Split Payments, Auto-Payouts, and Milestone Rewards
Africa-Wide: Bank Transfer + Mobile Money (Momo) Support
Withdrawal Request System + Affiliate Portal + Admin Dashboard

SECURITY REVISION NOTES (read before deploying):
- Checkout price is now locked server-side via /api/initiate-payment, which calls
  Paystack's Initialize Transaction endpoint. The Mini App no longer sends an amount
  to Paystack directly -- it can't be tampered with in the browser anymore.
- VIP channel invite links are no longer hardcoded in the front-end. Each paying
  user gets a fresh, single-use Telegram invite link generated after payment.
- All /admin/* and /cron/* endpoints now require an X-Admin-Key header matching
  the ADMIN_API_KEY environment variable. Set this in Render before deploying.
- The Telegram webhook now checks Telegram's secret_token header. Set
  TELEGRAM_WEBHOOK_SECRET in Render (any random string) before deploying.
- Automated Paystack payouts (Transfers) only work within the country your
  Paystack business account is registered in. Set PAYSTACK_ACCOUNT_COUNTRY to
  that country -- affiliates outside it are routed to manual payout instead of
  a broken automated one. Mobile money recipienhts are Ghana/Kenya only on
  Paystack; bank transfer recipients are Ghana/Nigeria/South Africa/Kenya only.
- No minimum withdrawal threshold -- affiliates can withdraw their full
  available balance, whatever it is.
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

# Required for the fixes below. Set these in Render's Environment tab.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
# The single country your Paystack account can actually pay out to automatically.
PAYSTACK_ACCOUNT_COUNTRY = os.getenv("PAYSTACK_ACCOUNT_COUNTRY", "ghana").lower()

# ==============================================================================
# CANONICAL PRICING (server is the source of truth -- never trust client amounts)
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
# No minimum withdrawal threshold -- affiliates can withdraw any available balance.

# ==============================================================================
# AFRICA PAYOUT CONFIGURATION
# Trimmed to the countries Paystack Transfers actually supports. Momo recipients
# are further restricted to Ghana/Kenya inside the flow below.
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
        # Idempotency: a given Paystack reference can only be processed once.
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
# ADMIN AUTH DEPENDENCY
# ==============================================================================
def verify_admin(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_API_KEY:
        # Fail closed: if you haven't set a key, nobody gets in -- including you.
        # Set ADMIN_API_KEY in Render, then pass it as X-Admin-Key on every
        # admin/cron request (e.g. Render Cron Job -> Header).
        raise HTTPException(status_code=503, detail="Admin API not configured (ADMIN_API_KEY unset)")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ==============================================================================
# PAYSTACK HELPERS
# ==============================================================================
def get_paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}

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
        logger.error(f"Transfer recipient creation failed: {data}")
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
    """
    account_type='nuban' -> regular bank list for a country (bank transfer).
    account_type='mobile_money' -> live list of mobile money telcos for a
    currency, fetched from Paystack directly instead of hardcoded, so this
    never goes stale or uses the wrong code again.
    """
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
    """Defense in depth: confirm the transaction with Paystack directly
    rather than trusting the webhook body alone."""
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
        "description": description,
        "reference": reference or f"TXN-{secrets.token_hex(6).upper()}",
        "metadata": metadata or {},
        "created_at": datetime.utcnow()
    }
    transactions_col.insert_one(doc)

# ==============================================================================
# TELEGRAM BOT
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

    logger.info(f"/start from {chat_id} (@{username})")

    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            ref_code = payload.replace("ref_", "")
            user_states[chat_id] = {"referred_by": ref_code}
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

    if is_affiliate is None:
        kb.append([InlineKeyboardButton("🤝 Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
        active_refs = 0
        if referrals_col is not None:
            active_refs = referrals_col.count_documents({
                "affiliate_id": is_affiliate["_id"],
                "is_active": True
            })
        if active_refs >= REFERRAL_MILESTONE and not is_affiliate.get("milestone_notified"):
            await notify_milestone(update, is_affiliate, active_refs)

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
            [InlineKeyboardButton("✅ I Agree and Join", callback_data="affiliate_agree")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        terms = (
            f"👑 <b>Jay Empire Affiliate Program</b>\n\n"
            f"<b>💰 Commissions:</b>\n"
            f"- First Sale: {COMMISSION_FIRST_SALE}%\n"
            f"- Renewals: {COMMISSION_RENEWAL}%\n"
            f"- Lifetime tracking\n\n"
            f"<b>💸 Payout:</b> Request anytime, any amount. Processed within 24-48hrs.\n\n"
            f"<b>🏆 Bonus:</b> {REFERRAL_MILESTONE}+ active referrals = Lifetime VIP!\n\n"
            f"<b>📋 Rules:</b> No fake signups, no self-referrals, no spam. Self-referred sales earn no commission.\n\n"
            f"Tap 'I Agree and Join' to accept terms and set up your payout method."
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "affiliate_agree":
        user_states[chat_id] = {"step": "awaiting_full_name", "data": {}}
        await bot.send_message(
            chat_id=chat_id,
            text="Step 1/5: Enter your Full Name (as on ID / account):",
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
        link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔗 Your Link:\n\n<code>{link}</code>\n\nTap and hold to copy!",
            parse_mode="HTML"
        )

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
                    text=(
                        f"⚠️ Automated payouts aren't available for "
                        f"{AFRICA_COUNTRIES.get(country_key, {}).get('name', country_key)} on our current "
                        f"Paystack account yet. Please contact @{ADMIN_USERNAME} to arrange payout manually."
                    ),
                )
                return

            if method == "momo" and country_key not in MOMO_ELIGIBLE_COUNTRIES:
                await bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Mobile Money payouts aren't supported for this country. Please choose Bank Transfer.",
                )
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

            await bot.send_message(
                chat_id=chat_id,
                text=f"Step 4/5: Enter your {provider_code} Mobile Money number (with country code, e.g. +233...):",
                parse_mode="HTML"
            )

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

async def show_affiliate_dashboard(chat_id, bot):
    aff = None
    if affiliates_col is not None:
        aff = affiliates_col.find_one({"telegram_id": chat_id})

    if aff is None:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate yet.")
        return

    ref_link = f"https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}"
    total_refs = 0
    active_refs = 0
    if referrals_col is not None:
        total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]})
        active_refs = referrals_col.count_documents({"affiliate_id": aff["_id"], "is_active": True})

    total_earnings = aff.get("total_earnings", 0)
    total_withdrawn = aff.get("total_withdrawn", 0)
    available_balance = total_earnings - total_withdrawn

    pending_withdrawals = 0
    if withdrawals_col is not None:
        pending_withdrawals = withdrawals_col.count_documents({
            "affiliate_id": aff["_id"],
            "status": "pending"
        })

    milestone = "UNLOCKED!" if active_refs >= REFERRAL_MILESTONE else f"({active_refs}/{REFERRAL_MILESTONE})"

    payout_method = aff.get("payout_method", "bank")
    payout_detail = ""
    if payout_method == "bank":
        bank = aff.get("bank_details", {})
        payout_detail = f"🏦 Bank: {bank.get('bank_name','N/A')} ({bank.get('country_name','N/A')})"
    else:
        momo = aff.get("mobile_money_details", {})
        payout_detail = f"📱 Momo: {momo.get('provider_name','N/A')} | {momo.get('phone_number','N/A')} ({momo.get('country_name','N/A')})"

    dashboard = (
        f"💎 <b>Jay Empire Affiliate Dashboard</b>\n\n"
        f"🔗 <b>Referral Link:</b>\n<code>{ref_link}</code>\n\n"
        f"💰 <b>Earnings:</b>\n"
        f"  Total Earned: ${total_earnings:,.2f}\n"
        f"  Total Withdrawn: ${total_withdrawn:,.2f}\n"
        f"  <b>Available Balance: ${available_balance:,.2f}</b>\n"
        f"  Pending Withdrawals: {pending_withdrawals}\n\n"
        f"👥 <b>Referrals:</b>\n"
        f"  Total: {total_refs} | Active: {active_refs}\n"
        f"  Commission: {COMMISSION_FIRST_SALE}% first | {COMMISSION_RENEWAL}% renewal\n"
        f"  🏆 Milestone: {milestone}\n\n"
        f"💳 <b>Payout Method:</b>\n"
        f"  {payout_detail}\n\n"
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

    await bot.send_message(
        chat_id=chat_id,
        text=dashboard,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def show_affiliate_statement(chat_id, bot):
    aff = None
    if affiliates_col is not None:
        aff = affiliates_col.find_one({"telegram_id": chat_id})

    if aff is None:
        await bot.send_message(chat_id=chat_id, text="Affiliate not found.")
        return

    transactions = []
    if transactions_col is not None:
        transactions = list(transactions_col.find(
            {"affiliate_id": aff["_id"]},
            {"_id": 0}
        ).sort("created_at", DESCENDING).limit(20))

    if not transactions:
        await bot.send_message(
            chat_id=chat_id,
            text="📋 <b>Your Statement</b>\n\nNo transactions yet. Start sharing your link to earn!",
            parse_mode="HTML"
        )
        return

    statement_text = "📋 <b>Your Financial Statement</b>\n\n"
    statement_text += f"{'Date':<12} {'Type':<12} {'Amount':>10}\n"
    statement_text += "─" * 40 + "\n"

    total_in = 0
    total_out = 0

    for txn in transactions:
        date_str = txn["created_at"].strftime("%d/%m/%Y")
        txn_type = txn["type"]
        amount = txn["amount"]

        if txn_type in ["commission"]:
            total_in += amount
            statement_text += f"{date_str:<12} {txn_type:<12} <code>+${amount:,.2f}</code>\n"
        else:
            total_out += amount
            statement_text += f"{date_str:<12} {txn_type:<12} <code>-${amount:,.2f}</code>\n"

        if txn.get("description"):
            statement_text += f"   ↳ {txn['description']}\n"

    statement_text += "─" * 40 + "\n"
    statement_text += f"{'Total In:':<25} <code>+${total_in:,.2f}</code>\n"
    statement_text += f"{'Total Out:':<25} <code>-${total_out:,.2f}</code>\n"
    statement_text += f"{'Balance:':<25} <b><code>${(total_in - total_out):,.2f}</code></b>"

    kb = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="affiliate_dashboard")]]
    await bot.send_message(
        chat_id=chat_id,
        text=statement_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def show_affiliate_referrals(chat_id, bot):
    aff = None
    if affiliates_col is not None:
        aff = affiliates_col.find_one({"telegram_id": chat_id})

    if aff is None:
        await bot.send_message(chat_id=chat_id, text="Affiliate not found.")
        return

    referrals = []
    if referrals_col is not None:
        referrals = list(referrals_col.find(
            {"affiliate_id": aff["_id"]}
        ).sort("created_at", DESCENDING).limit(15))

    if not referrals:
        await bot.send_message(
            chat_id=chat_id,
            text="👥 <b>Your Referrals</b>\n\nNo referrals yet. Share your link to start earning!",
            parse_mode="HTML"
        )
        return

    text = "👥 <b>Your Referrals</b>\n\n"
    for ref in referrals:
        status = "🟢 Active" if ref.get("is_active") else "🔴 Inactive"
        last_payment = ref.get("last_payment", {})
        amount = last_payment.get("amount", 0) / 100 if last_payment else 0
        commission = last_payment.get("commission_paid", 0) / 100 if last_payment else 0

        text += f"User: <code>{ref['customer_telegram_id']}</code> {status}\n"
        text += f"  Channel: {ref.get('customer_channel', 'N/A').upper()}\n"
        if last_payment:
            text += f"  Last Pay: ${amount:,.2f} | Your Commission: ${commission:,.2f}\n"
        text += "\n"

    kb = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="affiliate_dashboard")]]
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_withdrawal_request(chat_id, bot):
    aff = None
    if affiliates_col is not None:
        aff = affiliates_col.find_one({"telegram_id": chat_id})

    if aff is None:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate.")
        return

    if withdrawals_col is not None:
        pending = withdrawals_col.count_documents({
            "affiliate_id": aff["_id"],
            "status": "pending"
        })
        if pending > 0:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ You already have a pending withdrawal request. Please wait for it to be processed.",
                parse_mode="HTML"
            )
            return

    total_earnings = aff.get("total_earnings", 0)
    total_withdrawn = aff.get("total_withdrawn", 0)
    available = total_earnings - total_withdrawn

    if available <= 0:
        await bot.send_message(
            chat_id=chat_id,
            text="❌ You don't have any available balance to withdraw yet.",
            parse_mode="HTML"
        )
        return

    kb = []
    preset_amounts = [10, 25, 50, 100, 250]
    for amount in preset_amounts:
        if amount <= available:
            kb.append([InlineKeyboardButton(f"Withdraw ${amount}", callback_data=f"withdraw_confirm:{int(amount * 100)}")])

    full_cents = int(round(available * 100))
    kb.append([InlineKeyboardButton(f"💰 Withdraw Full Balance (${available:,.2f})", callback_data=f"withdraw_confirm:{full_cents}")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_dashboard")])

    await bot.send_message(
        chat_id=chat_id,
        text=f"💸 <b>Request Withdrawal</b>\n\nAvailable Balance: <code>${available:,.2f}</code>\n\nNo minimum — select an amount, or withdraw it all:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def process_withdrawal_confirmation(chat_id, amount_cents, bot):
    aff = None
    if affiliates_col is not None:
        aff = affiliates_col.find_one({"telegram_id": chat_id})

    if aff is None:
        return

    amount = amount_cents / 100

    withdrawal_doc = {
        "affiliate_id": aff["_id"],
        "telegram_id": chat_id,
        "username": aff.get("username", ""),
        "full_name": aff.get("full_name", ""),
        "ref_code": aff.get("ref_code", ""),
        "amount": amount,
        "amount_cents": amount_cents,
        "payout_method": aff.get("payout_method", "bank"),
        "payout_details": aff.get("bank_details") if aff.get("payout_method") == "bank" else aff.get("mobile_money_details"),
        "status": "pending",
        "admin_approved": False,
        "admin_notes": "",
        "paystack_transfer_code": None,
        "created_at": datetime.utcnow(),
        "processed_at": None
    }

    if withdrawals_col is not None:
        withdrawals_col.insert_one(withdrawal_doc)

    log_affiliate_transaction(
        affiliate_id=aff["_id"],
        transaction_type="withdrawal_request",
        amount=amount,
        description=f"Withdrawal request of ${amount:,.2f}",
        reference=str(withdrawal_doc.get("_id", datetime.utcnow()))
    )

    await bot.send_message(
        chat_id=chat_id,
        text=f"✅ <b>Withdrawal Request Submitted!</b>\n\nAmount: ${amount:,.2f}\nStatus: Pending\n\nSent to admin for approval. Processing time: 24-48 hours.",
        parse_mode="HTML"
    )

    try:
        payout_info = ""
        if aff.get("payout_method") == "bank":
            bank = aff.get("bank_details", {})
            payout_info = f"Bank: {bank.get('bank_name','N/A')}\nAccount: {bank.get('account_number','N/A')}\nName: {bank.get('account_name','N/A')}"
        else:
            momo = aff.get("mobile_money_details", {})
            payout_info = f"Provider: {momo.get('provider_name','N/A')}\nNumber: {momo.get('phone_number','N/A')}"

        await bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=(
                f"🚨 <b>New Withdrawal Request</b>\n\n"
                f"Affiliate: {aff.get('full_name', 'N/A')} (@{aff.get('username', 'N/A')})\n"
                f"Code: <code>{aff['ref_code']}</code>\n"
                f"Amount: ${amount:,.2f}\n"
                f"Method: {aff.get('payout_method', 'N/A').upper()}\n"
                f"{payout_info}\n\n"
                f"Approve via admin API: POST /admin/withdrawals/{{id}}/approve (X-Admin-Key required)"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify admin of withdrawal: {e}")

async def show_payout_info(chat_id, bot):
    aff = None
    if affiliates_col is not None:
        aff = affiliates_col.find_one({"telegram_id": chat_id})
    if aff is not None:
        payout_method = aff.get("payout_method", "bank")
        if payout_method == "bank":
            b = aff.get("bank_details", {})
            await bot.send_message(
                chat_id=chat_id,
                text=f"💳 Payout Details (Bank Transfer)\n\nBank: {b.get('bank_name','N/A')}\nAccount: ****{b.get('account_number','0000')[-4:]}\nName: {b.get('account_name','N/A')}\nCountry: {b.get('country_name','N/A')}\n\nAutomatic via Paystack.",
                parse_mode="HTML"
            )
        else:
            m = aff.get("mobile_money_details", {})
            await bot.send_message(
                chat_id=chat_id,
                text=f"📱 Payout Details (Mobile Money)\n\nProvider: {m.get('provider_name','N/A')}\nNumber: {m.get('phone_number','N/A')}\nName: {m.get('account_name','N/A')}\nCountry: {m.get('country_name','N/A')}\n\nAutomatic via Paystack.",
                parse_mode="HTML"
            )

async def show_country_selection(chat_id, bot, method):
    kb = []
    row = []
    for key, info in AFRICA_COUNTRIES.items():
        if method == "momo" and key not in MOMO_ELIGIBLE_COUNTRIES:
            continue
        btn = InlineKeyboardButton(f"{info['flag']} {info['name']}", callback_data=f"country:{key}:{method}")
        row.append(btn)
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("Cancel", callback_data="affiliate_agree")])

    method_text = "Bank Transfer" if method == "bank" else "Mobile Money"
    await bot.send_message(
        chat_id=chat_id,
        text=f"Step 2/5: Select your country for {method_text} payouts:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def show_bank_selection(chat_id, bot, country):
    banks = await get_paystack_bank_list(country, account_type="nuban")
    kb = []
    for b in banks[:20]:
        kb.append([InlineKeyboardButton(b["name"], callback_data=f"aff_bank:{b['code']}:{b['name']}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="affiliate_agree")])
    await bot.send_message(
        chat_id=chat_id,
        text="Step 3/5: Select Your Bank",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def show_momo_provider_selection(chat_id, bot, country):
    currency = AFRICA_COUNTRIES.get(country, {}).get("currency", "GHS")
    providers = await get_paystack_bank_list(country, account_type="mobile_money", currency=currency)
    if not providers:
        await bot.send_message(
            chat_id=chat_id,
            text=f"No Mobile Money providers available from Paystack right now. Please choose Bank Transfer instead, or contact @{ADMIN_USERNAME}."
        )
        return
    kb = []
    for p in providers[:10]:
        kb.append([InlineKeyboardButton(p["name"], callback_data=f"momo_provider:{p['code']}")])
    kb.append([InlineKeyboardButton("Cancel", callback_data="affiliate_agree")])
    await bot.send_message(
        chat_id=chat_id,
        text="Step 3/5: Select your Mobile Money provider:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ==============================================================================
# SUBSCRIPTION MANAGEMENT
# ==============================================================================
async def generate_single_use_invite(channel_id: str):
    """Fresh, single-use invite link -- never a static link embedded anywhere
    the public can read it."""
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
# LIFESPAN
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
        logger.warning("TELEGRAM_WEBHOOK_SECRET is not set -- webhook is unauthenticated. Set it in Render.")
        await bot.set_webhook(url=webhook_target)
    logger.info(f"Webhook set: {webhook_target}")

    if not ADMIN_API_KEY:
        logger.warning("ADMIN_API_KEY is not set -- all /admin and /cron endpoints are locked out (fail-closed). Set it in Render.")

    asyncio.create_task(scheduler_loop())
    yield
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# PUBLIC ENDPOINTS
# ==============================================================================

@app.get("/")
async def health():
    return {
        "status": "active",
        "mongodb": "connected" if db is not None else "disconnected",
        "service": "Jay Empire VIP + Affiliate (Africa-Wide + Withdrawals)",
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
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=503)

@app.get("/api/plans")
async def api_plans():
    """Lets the Mini App fetch canonical pricing instead of hardcoding it,
    so front-end and back-end can never drift apart."""
    return {"plans": PLANS, "currency_rates": CURRENCY_RATES}

class InitiatePaymentRequest(BaseModel):
    telegram_id: int
    channel_type: str
    plan_key: str
    currency: str
    ref_code: Optional[str] = None
    email: Optional[str] = None

@app.post("/api/initiate-payment")
async def api_initiate_payment(payload: InitiatePaymentRequest):
    """
    The only place an amount is ever decided. The Mini App calls this first,
    gets back an access_code, and hands that to Paystack's popup. The client
    never gets to tell Paystack how much to charge -- closing the price
    tampering hole in the old flow.
    """
    plan = PLANS_BY_KEY.get(payload.plan_key)
    if plan is None:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if payload.channel_type not in ("gold", "fx"):
        raise HTTPException(status_code=400, detail="Invalid channel")
    rate = CURRENCY_RATES.get(payload.currency)
    if rate is None:
        raise HTTPException(status_code=400, detail="Invalid currency")
    if users_col is None:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    existing = users_col.find_one({"telegram_id": payload.telegram_id, "channel_type": payload.channel_type})

    if plan.get("is_test") and existing and existing.get("test_used"):
        raise HTTPException(status_code=400, detail="Test phase already used for this channel. Please choose a paid plan.")

    is_renewal = bool(existing and existing.get("is_active") and existing.get("expires_at", datetime.utcnow()) > datetime.utcnow())

    amount_minor = round(plan["usd"] * rate * 100)
    reference = f"JAY-{secrets.token_hex(8).upper()}"
    email = payload.email or f"user_{payload.telegram_id}@jayempire.com"

    metadata = {
        "telegram_id": payload.telegram_id,
        "channel_type": payload.channel_type,
        "plan_key": plan["key"],
        "days": plan["days"],
        "ref_code": payload.ref_code,
        "is_renewal": is_renewal,
        "is_test": bool(plan.get("is_test")),
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            json={
                "email": email,
                "amount": amount_minor,
                "currency": payload.currency,
                "reference": reference,
                "metadata": metadata
            },
            headers=get_paystack_headers(),
            timeout=15.0
        )
        data = res.json()

    if not data.get("status"):
        logger.error(f"Paystack initialize failed: {data}")
        raise HTTPException(status_code=502, detail="Could not start payment. Please try again.")

    return {
        "access_code": data["data"]["access_code"],
        "reference": data["data"]["reference"],
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
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        username = query["from"].get("username", "")

        try:
            await Bot(token=BOT_TOKEN).answer_callback_query(callback_query_id=query["id"])
        except:
            pass

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
                    text=f"Step 4/5: Enter your Account Number for {bank_name}:",
                    parse_mode="HTML"
                )
            return {"status": "ok"}

        if action == "affiliate_confirm":
            if chat_id not in user_states:
                return {"status": "ok"}
            state = user_states[chat_id]
            d = state["data"]
            payout_method = d.get("payout_method", "bank")
            currency = d.get("currency", "GHS")

            if payout_method == "bank":
                recipient = await create_paystack_transfer_recipient(
                    d["full_name"], d["account_number"], d["bank_code"], currency, recipient_type="nuban"
                )
                if recipient is None:
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=chat_id,
                        text=f"Failed to create payout account. Contact @{ADMIN_USERNAME}."
                    )
                    return {"status": "ok"}

                payout_details = {
                    "paystack_transfer_recipient": recipient,
                    "bank_details": {
                        "bank_code": d["bank_code"],
                        "bank_name": d["bank_name"],
                        "account_number": d["account_number"],
                        "account_name": d.get("account_name", d["full_name"]),
                        "country": d.get("country", ""),
                        "country_name": d.get("country_name", "")
                    }
                }
            else:
                recipient = await create_paystack_transfer_recipient(
                    d["full_name"], d["momo_number"], d["momo_provider"], currency, recipient_type="mobile_money"
                )
                if recipient is None:
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=chat_id,
                        text=f"Failed to create Mobile Money recipient. Contact @{ADMIN_USERNAME}."
                    )
                    return {"status": "ok"}

                payout_details = {
                    "paystack_transfer_recipient": recipient,
                    "mobile_money_details": {
                        "provider": d["momo_provider"],
                        "provider_name": d.get("momo_provider_name", ""),
                        "phone_number": d["momo_number"],
                        "account_name": d.get("account_name", d["full_name"]),
                        "country": d.get("country", ""),
                        "country_name": d.get("country_name", "")
                    }
                }

            ref_code = generate_ref_code()
            aff_doc = {
                "telegram_id": chat_id,
                "username": username,
                "full_name": d["full_name"],
                "ref_code": ref_code,
                "payout_method": payout_method,
                "country": d.get("country", ""),
                "country_name": d.get("country_name", ""),
                "commission_rates": {"first_sale": COMMISSION_FIRST_SALE, "renewal": COMMISSION_RENEWAL},
                "total_earnings": 0,
                "total_withdrawn": 0,
                "total_referrals": 0,
                "is_active": True,
                "milestone_notified": False,
                "created_at": datetime.utcnow(),
                **payout_details
            }

            if affiliates_col is not None:
                affiliates_col.insert_one(aff_doc)

            ref_link = f"https://t.me/JayEmpire_bot?start=ref_{ref_code}"
            payout_text = "Bank Transfer" if payout_method == "bank" else "Mobile Money"
            await Bot(token=BOT_TOKEN).send_message(
                chat_id=chat_id,
                text=(
                    f"🎉 Welcome to the Affiliate Program!\n\n"
                    f"Your Link:\n{ref_link}\n\n"
                    f"Commissions: {COMMISSION_FIRST_SALE}% first | {COMMISSION_RENEWAL}% renewal\n"
                    f"Bonus: {REFERRAL_MILESTONE}+ refs = Lifetime VIP\n"
                    f"Payout Method: {payout_text}\n"
                    f"No minimum withdrawal — cash out anytime.\n\n"
                    f"Start sharing now!"
                ),
                parse_mode="HTML"
            )
            del user_states[chat_id]
            return {"status": "ok"}

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
                state["step"] = "awaiting_payout_method"

                kb = [[InlineKeyboardButton("🏦 Bank Transfer", callback_data="payout_method_bank")]]
                if PAYSTACK_ACCOUNT_COUNTRY in MOMO_ELIGIBLE_COUNTRIES:
                    kb.append([InlineKeyboardButton("📱 Mobile Money (Momo)", callback_data="payout_method_momo")])
                kb.append([InlineKeyboardButton("Cancel", callback_data="back_main")])

                await bot.send_message(
                    chat_id=chat_id,
                    text="Step 2/5: How would you like to receive your commissions?",
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
                        text=f"Confirm:\n\nName: {state['data']['full_name']}\nCountry: {state['data'].get('country_name', 'N/A')}\nBank: {state['data']['bank_name']}\nAccount: {text}\nVerified: {acc_name}\n\nTap confirm to start earning!",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                else:
                    await bot.send_message(chat_id=chat_id, text="Could not verify. Check and try again.")
                    state["step"] = "awaiting_account_number"
                return {"status": "ok"}

            elif step == "awaiting_momo_number":
                state["data"]["momo_number"] = text
                state["data"]["account_name"] = state["data"]["full_name"]
                state["step"] = "awaiting_confirmation"

                kb = [
                    [InlineKeyboardButton("Confirm and Create", callback_data="affiliate_confirm")],
                    [InlineKeyboardButton("Start Over", callback_data="affiliate_agree")]
                ]
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Confirm:\n\nName: {state['data']['full_name']}\nCountry: {state['data'].get('country_name', 'N/A')}\nProvider: {state['data'].get('momo_provider_name', 'N/A')}\nNumber: {text}\n\nTap confirm to start earning!",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return {"status": "ok"}

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
            reference = data.get("reference", "unknown")

            # Idempotency: Paystack retries webhooks. Without this, a retry
            # would double-extend access and double-pay commission.
            if webhook_events_col is not None:
                try:
                    webhook_events_col.insert_one({"reference": reference, "processed_at": datetime.utcnow()})
                except DuplicateKeyError:
                    logger.info(f"Duplicate webhook for {reference}, skipping.")
                    return {"status": "already_processed"}

            # Defense in depth: confirm directly with Paystack rather than
            # trusting the webhook body alone.
            verified = await verify_paystack_transaction(reference)
            if verified is None or verified.get("status") != "success":
                logger.warning(f"Transaction {reference} failed verification, not fulfilling.")
                return {"status": "unverified"}
            if verified.get("amount") != data.get("amount"):
                logger.warning(f"Amount mismatch for {reference}: webhook={data.get('amount')} verify={verified.get('amount')}")
                return {"status": "amount_mismatch"}

            meta = data.get("metadata", {}) or {}

            tg_id = meta.get("telegram_id")
            channel_type = meta.get("channel_type", "gold")
            days = int(meta.get("days", 30))
            ref_code = meta.get("ref_code")
            is_renewal = bool(meta.get("is_renewal", False))
            is_test = bool(meta.get("is_test", False))

            if not tg_id or tg_id == 0:
                return {"status": "ignored"}

            now = datetime.utcnow()

            existing = None
            if users_col is not None:
                existing = users_col.find_one({"telegram_id": tg_id, "channel_type": channel_type})

            # Extend from the current expiry if still active, instead of
            # resetting to now + days (which used to cost early renewers days).
            if existing and existing.get("is_active") and existing.get("expires_at") and existing["expires_at"] > now:
                expires = existing["expires_at"] + timedelta(days=days)
            else:
                expires = now + timedelta(days=days)

            if users_col is not None:
                update_fields = {
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

                if ref_code and affiliates_col is not None and referrals_col is not None:
                    affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
                    if affiliate is not None and affiliate["telegram_id"] == tg_id:
                        logger.warning(f"Self-referral blocked: affiliate {ref_code} tried to refer themselves ({tg_id}).")
                    elif affiliate is not None:
                        rate = COMMISSION_RENEWAL if is_renewal else COMMISSION_FIRST_SALE
                        amount = data.get("amount", 0)
                        commission = int(amount * rate / 100)
                        commission_dollars = commission / 100

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
                                "$inc": {"total_earnings": commission_dollars, "total_referrals": 0 if is_renewal else 1},
                                "$set": {"last_earning_at": now}
                            }
                        )

                        log_affiliate_transaction(
                            affiliate_id=affiliate["_id"],
                            transaction_type="commission",
                            amount=commission_dollars,
                            description=f"{'Renewal' if is_renewal else 'First sale'} commission from user {tg_id}",
                            reference=reference,
                            metadata={"customer_id": tg_id, "channel": channel_type, "rate": rate}
                        )

                        logger.info(f"Affiliate {ref_code} earned {rate}% = {commission} from {tg_id}")

            channel_id = GOLD_CHANNEL_ID if channel_type == "gold" else FOREX_CHANNEL_ID
            name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
            bot = Bot(token=BOT_TOKEN)
            try:
                if is_renewal:
                    await bot.send_message(
                        chat_id=tg_id,
                        text=f"✅ PAYMENT VERIFIED!\n\nPlan: {channel_type.upper()}\nExpires: {expires.strftime('%B %d, %Y')}\n\nYou're already in {name} — no action needed.",
                        parse_mode="HTML",
                    )
                else:
                    invite_link = await generate_single_use_invite(channel_id)
                    if invite_link:
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"Enter {name}", url=invite_link)]])
                        await bot.send_message(
                            chat_id=tg_id,
                            text=f"✅ PAYMENT VERIFIED!\n\nPlan: {channel_type.upper()}\nExpires: {expires.strftime('%B %d, %Y')}\n\nTap below (this link works once):",
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
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ==============================================================================
# ADMIN ENDPOINTS (all require X-Admin-Key header == ADMIN_API_KEY)
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
            "total_earnings": aff.get("total_earnings", 0),
            "total_withdrawn": aff.get("total_withdrawn", 0),
            "available_balance": aff.get("total_earnings", 0) - aff.get("total_withdrawn", 0),
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
    total_earnings = sum(a.get("total_earnings", 0) for a in affiliates_col.find())

    pending_withdrawals = 0
    total_withdrawn = 0
    if withdrawals_col is not None:
        pending_withdrawals = withdrawals_col.count_documents({"status": "pending"})
        total_withdrawn = sum(w.get("amount", 0) for w in withdrawals_col.find({"status": "approved"}))

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
            "amount": w.get("amount", 0),
            "payout_method": w.get("payout_method", "N/A"),
            "payout_details": w.get("payout_details", {}),
            "status": w.get("status", "N/A"),
            "admin_notes": w.get("admin_notes", ""),
            "created_at": w.get("created_at").isoformat() if w.get("created_at") else None,
            "processed_at": w.get("processed_at").isoformat() if w.get("processed_at") else None,
            "paystack_transfer_code": w.get("paystack_transfer_code"),
            "affiliate_total_earnings": aff.get("total_earnings", 0) if aff else 0,
            "affiliate_total_withdrawn": aff.get("total_withdrawn", 0) if aff else 0
        })

    return {
        "status": status,
        "count": len(enriched),
        "total_amount": round(sum(w["amount"] for w in enriched), 2),
        "withdrawals": enriched
    }

@app.post("/admin/withdrawals/{withdrawal_id}/approve")
async def admin_approve_withdrawal(withdrawal_id: str, notes: str = "", _: bool = Depends(verify_admin)):
    if withdrawals_col is None or affiliates_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)

    from bson.objectid import ObjectId

    try:
        withdrawal = withdrawals_col.find_one({"_id": ObjectId(withdrawal_id)})
    except:
        return JSONResponse({"error": "Invalid withdrawal ID"}, status_code=400)

    if withdrawal is None:
        return JSONResponse({"error": "Withdrawal not found"}, status_code=404)

    if withdrawal.get("status") != "pending":
        return JSONResponse({"error": f"Withdrawal already {withdrawal['status']}"}, status_code=400)

    affiliate = affiliates_col.find_one({"_id": withdrawal["affiliate_id"]})
    if affiliate is None:
        return JSONResponse({"error": "Affiliate not found"}, status_code=404)

    available = affiliate.get("total_earnings", 0) - affiliate.get("total_withdrawn", 0)
    if available < withdrawal["amount"]:
        return JSONResponse({"error": "Insufficient affiliate balance"}, status_code=400)

    recipient = affiliate.get("paystack_transfer_recipient")
    if recipient:
        amount_cents = int(withdrawal["amount"] * 100)
        transfer_result = await initiate_paystack_transfer(
            amount=amount_cents,
            recipient_code=recipient,
            reason=f"Affiliate withdrawal - {affiliate['ref_code']}"
        )
    else:
        transfer_result = {"success": False, "error": "No payout recipient on file for this affiliate"}

    now = datetime.utcnow()
    withdrawals_col.update_one(
        {"_id": ObjectId(withdrawal_id)},
        {
            "$set": {
                "status": "approved",
                "admin_approved": True,
                "admin_notes": notes,
                "processed_at": now,
                "paystack_transfer_code": transfer_result.get("transfer_code") if transfer_result else None
            }
        }
    )

    affiliates_col.update_one(
        {"_id": affiliate["_id"]},
        {
            "$inc": {"total_withdrawn": withdrawal["amount"]},
            "$set": {"last_withdrawal_at": now}
        }
    )

    log_affiliate_transaction(
        affiliate_id=affiliate["_id"],
        transaction_type="withdrawal",
        amount=withdrawal["amount"],
        description=f"Withdrawal approved: ${withdrawal['amount']:,.2f}",
        reference=withdrawal_id,
        metadata={"admin_notes": notes, "paystack_result": transfer_result}
    )

    try:
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=affiliate["telegram_id"],
            text=f"✅ <b>Withdrawal Approved!</b>\n\nAmount: ${withdrawal['amount']:,.2f}\nStatus: Processed\n\nYour payment is on its way! Processing time: 24-48 hours.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify affiliate of approval: {e}")

    return {
        "status": "approved",
        "withdrawal_id": withdrawal_id,
        "amount": withdrawal["amount"],
        "affiliate": affiliate["ref_code"],
        "paystack_result": transfer_result,
        "processed_at": now.isoformat()
    }

@app.post("/admin/withdrawals/{withdrawal_id}/reject")
async def admin_reject_withdrawal(withdrawal_id: str, notes: str = "", _: bool = Depends(verify_admin)):
    if withdrawals_col is None:
        return JSONResponse({"error": "DB offline"}, status_code=503)

    from bson.objectid import ObjectId

    try:
        withdrawal = withdrawals_col.find_one({"_id": ObjectId(withdrawal_id)})
    except:
        return JSONResponse({"error": "Invalid withdrawal ID"}, status_code=400)

    if withdrawal is None:
        return JSONResponse({"error": "Withdrawal not found"}, status_code=404)

    if withdrawal.get("status") != "pending":
        return JSONResponse({"error": f"Withdrawal already {withdrawal['status']}"}, status_code=400)

    now = datetime.utcnow()
    withdrawals_col.update_one(
        {"_id": ObjectId(withdrawal_id)},
        {
            "$set": {
                "status": "rejected",
                "admin_approved": False,
                "admin_notes": notes,
                "processed_at": now
            }
        }
    )

    log_affiliate_transaction(
        affiliate_id=withdrawal["affiliate_id"],
        transaction_type="withdrawal_reversal",
        amount=0,
        description=f"Withdrawal rejected: ${withdrawal['amount']:,.2f} - {notes}",
        reference=withdrawal_id
    )

    try:
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=withdrawal["telegram_id"],
            text=f"❌ <b>Withdrawal Rejected</b>\n\nAmount: ${withdrawal['amount']:,.2f}\nReason: {notes}\n\nPlease contact @{ADMIN_USERNAME} for more information.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify affiliate of rejection: {e}")

    return {
        "status": "rejected",
        "withdrawal_id": withdrawal_id,
        "amount": withdrawal["amount"],
        "reason": notes,
        "processed_at": now.isoformat()
    }

# ==============================================================================
# RUN
# ==============================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
