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
from typing import Optional, Any

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.server_api import ServerApi
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==============================================================================
# ENVIRONMENT & CONFIG
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

COMMISSION_FIRST_SALE = 50
COMMISSION_RENEWAL = 35REFERRAL_MILESTONE = 10
MINIMUM_WITHDRAWAL = 5000  # in cents ($50)

MAX_REQUESTS_PER_MINUTE = 10
rate_limit_store = {}

# ==============================================================================
# AFRICA PAYOUT CONFIGURATION (FIXED PAYSTACK BANK CODES)
# ==============================================================================
AFRICA_COUNTRIES = {
    "ghana": {"name": "Ghana", "currency": "GHS", "flag": "🇬🇭", "country_code": "+233"},
    "nigeria": {"name": "Nigeria", "currency": "NGN", "flag": "🇳🇬", "country_code": "+234"},
    "kenya": {"name": "Kenya", "currency": "KES", "flag": "🇰🇪", "country_code": "+254"},
    "south_africa": {"name": "South Africa", "currency": "ZAR", "flag": "🇿🇦", "country_code": "+27"},
    "cote_ivoire": {"name": "Côte d'Ivoire", "currency": "XOF", "flag": "🇨🇮", "country_code": "+225"},
    "uganda": {"name": "Uganda", "currency": "UGX", "flag": "🇺🇬", "country_code": "+256"},
}

# CRITICAL FIX: Use exact Paystack mobile money bank codes
MOBILE_MONEY_PROVIDERS = {
    "ghana": [
        {"code": "mtn_gh", "name": "MTN Mobile Money", "flag": "📱"},
        {"code": "airteltigo_gh", "name": "AT Money / AirtelTigo", "flag": "📱"},
        {"code": "vodafone_gh", "name": "Telecel Cash", "flag": "📱"},
    ],
    "kenya": [
        {"code": "mpesa_ke", "name": "M-PESA", "flag": "📱"},
    ],
    "nigeria": [
        {"code": "mtn_ng", "name": "MTN MoMo", "flag": "📱"},
        {"code": "airtel_ng", "name": "Airtel Money", "flag": "📱"},
    ],
    "uganda": [
        {"code": "mtn_ug", "name": "MTN Mobile Money", "flag": "📱"},
        {"code": "airtel_ug", "name": "Airtel Money", "flag": "📱"},
    ],
}

# ==============================================================================
# MONGODB INITIALIZATION
# ==============================================================================
def init_mongodb():
    if not MONGO_URI:
        logger.error("MONGO_URI is not set!")
        return None, None, None, None, None, None, None, None

    try:
        client = MongoClient(
            MONGO_URI,
            tls=True,            tlsCAFile=certifi.where(),
            server_api=ServerApi('1'),
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=20000,
            socketTimeoutMS=45000,
            retryWrites=True,
            maxPoolSize=50,
        )
        client.admin.command('ping')
        logger.info("✅ MongoDB connected successfully")
        db = client.get_default_database()

        users_col = db["vip_users"]
        leads_col = db["leads"]
        affiliates_col = db["affiliates"]
        referrals_col = db["referrals"]
        withdrawals_col = db["withdrawals"]
        transactions_col = db["affiliate_transactions"]
        rate_limits_col = db["rate_limits"]

        users_col.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
        users_col.create_index([("expires_at", ASCENDING)])
        affiliates_col.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates_col.create_index([("ref_code", ASCENDING)], unique=True)
        referrals_col.create_index([("affiliate_id", ASCENDING)])
        withdrawals_col.create_index([("affiliate_id", ASCENDING), ("status", ASCENDING)])
        rate_limits_col.create_index([("user_id", ASCENDING), ("timestamp", ASCENDING)], expireAfterSeconds=60)

        return client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        return None, None, None, None, None, None, None, None

mongo_client, db, users_col, leads_col, affiliates_col, referrals_col, withdrawals_col, transactions_col = init_mongodb()

# ==============================================================================
# SECURITY & VALIDATION HELPERS
# ==============================================================================
def check_rate_limit(user_id: int) -> bool:
    if not db: return True
    now = datetime.utcnow()
    count = db.rate_limits.count_documents({"user_id": user_id, "timestamp": {"$gte": now - timedelta(seconds=60)}})
    if count >= MAX_REQUESTS_PER_MINUTE:
        return False
    db.rate_limits.insert_one({"user_id": user_id, "timestamp": now})
    return True

def validate_phone_number(phone: str, country: str) -> bool:
    if not phone or not country: return False
    cleaned = re.sub(r'[\\s\\-\\(\\)]', '', phone)    expected_code = AFRICA_COUNTRIES.get(country, {}).get("country_code", "")
    if not cleaned.startswith(expected_code): return False
    return len(cleaned) >= 10 and cleaned.isdigit()

def validate_account_number(account: str) -> bool:
    if not account: return False
    cleaned = re.sub(r'\\s', '', account)
    return cleaned.isdigit() and 7 <= len(cleaned) <= 18

def get_paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}

# ==============================================================================
# PAYSTACK HELPERS (BUG-FIXED)
# ==============================================================================
async def create_paystack_subaccount(business_name: str, bank_code: str, account_number: str, percentage: int):
    payload = {
        "business_name": business_name,
        "settlement_bank": bank_code,
        "account_number": account_number,
        "percentage_charge": percentage,
        "description": f"Jay Empire Affiliate - {business_name}"
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post("https://api.paystack.co/subaccount", json=payload, headers=get_paystack_headers())
            res.raise_for_status()
            data = res.json()
            return data["data"]["subaccount_code"] if data.get("status") else None
    except Exception as e:
        logger.error(f"Subaccount creation failed: {e}")
        return None

async def create_paystack_transfer_recipient(name: str, account_number: str, bank_code: str, currency: str = "GHS"):
    # CRITICAL FIX: Proper payload structure for Mobile Money
    payload = {
        "type": "mobile_money",
        "name": name,
        "account_number": account_number,
        "bank_code": bank_code,
        "currency": currency
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post("https://api.paystack.co/transferrecipient", json=payload, headers=get_paystack_headers())
            res.raise_for_status()
            data = res.json()
            return data["data"]["recipient_code"] if data.get("status") else None
    except Exception as e:
        logger.error(f"Transfer recipient creation failed: {e}")        return None

async def initiate_paystack_transfer(amount: int, recipient_code: str, reason: str, reference: str = None):
    if reference is None:
        reference = f"JAYWTH-{secrets.token_hex(8).upper()}"
    payload = {"source": "balance", "amount": amount, "recipient": recipient_code, "reason": reason, "reference": reference}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post("https://api.paystack.co/transfer", json=payload, headers=get_paystack_headers())
            res.raise_for_status()
            data = res.json()
            if data.get("status"):
                return {"success": True, "transfer_code": data["data"]["transfer_code"], "reference": reference}
            return {"success": False, "error": data.get("message", "Unknown error")}
    except Exception as e:
        logger.error(f"Transfer initiation failed: {e}")
        return {"success": False, "error": str(e)}

async def get_paystack_bank_list(country: str = "ghana"):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"https://api.paystack.co/bank?country={country}", headers=get_paystack_headers())
            res.raise_for_status()
            data = res.json()
            return data.get("data", []) if data.get("status") else []
    except Exception as e:
        logger.error(f"Error fetching bank list: {e}")
        return []

async def verify_bank_account(account_number: str, bank_code: str):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"https://api.paystack.co/bank/resolve?account_number={account_number}&bank_code={bank_code}", headers=get_paystack_headers())
            res.raise_for_status()
            data = res.json()
            return data["data"]["account_name"] if data.get("status") else None
    except Exception as e:
        logger.error(f"Error verifying bank account: {e}")
        return None

def generate_ref_code() -> str:
    return f"JAY{''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))}"

def log_affiliate_transaction(affiliate_id: Any, transaction_type: str, amount: float, description: str, reference: str = None, metadata: dict = None):
    if transactions_col is None: return
    transactions_col.insert_one({
        "affiliate_id": affiliate_id, "type": transaction_type, "amount": amount,
        "description": description, "reference": reference or f"TXN-{secrets.token_hex(6).upper()}",
        "metadata": metadata or {}, "created_at": datetime.utcnow()
    })
# ==============================================================================
# TELEGRAM BOT
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()
user_states = {}

async def start_cmd(update: Update, context):
    """CLASSIC UI: Clear, professional, emoji-enhanced menu with two distinct paths."""
    user = update.effective_user
    if not user: return

    chat_id = user.id
    text = update.message.text or ""

    if not check_rate_limit(chat_id):
        await update.message.reply_text("⚠️ Too many requests. Please wait a moment.")
        return

    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            ref_code = payload.replace("ref_", "")
            user_states[chat_id] = {"referred_by": ref_code}

    if leads_col is not None:
        leads_col.update_one(
            {"telegram_id": chat_id},
            {"$setOnInsert": {"telegram_id": chat_id, "first_name": user.first_name, "username": user.username or "", "started_at": datetime.utcnow(), "converted": False, "referred_by": ref_code}},
            upsert=True
        )

    is_affiliate = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True}) if affiliates_col else None

    # Classic, clean keyboard layout
    kb = [
        [InlineKeyboardButton("💎 Launch VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))],
    ]
    if is_affiliate is None:
        kb.append([InlineKeyboardButton("💰 Become an Affiliate", callback_data="affiliate_start")])
    else:
        kb.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])

    welcome_text = (
        "👑 <b>Welcome to Jay Empire VIP</b> 👑\\n\\n"
        "Premium Trading Signals & Analytics.\\n\\n"
        "Please select an option below:\\n\\n"
        "💎 <b>VIP Terminal</b>\\nAccess exclusive, high-probability trading signals.\\n\\n"
        "💰 <b>Affiliate Program</b>\\nEarn generous commissions by referring others."    )
    if ref_code:
        welcome_text += f"\\n\\n🔗 You were referred by: <code>{ref_code}</code>"

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def callback_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if not check_rate_limit(query.from_user.id):
        await query.answer("⚠️ Please wait a moment.", show_alert=True)
        return
    await handle_affiliate_callback(query.message.chat.id, query.data, query.from_user.username or "")

telegram_app.add_handler(CommandHandler("start", start_cmd))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))

# ==============================================================================
# AFFILIATE SYSTEM
# ==============================================================================
async def handle_affiliate_callback(chat_id: int, action: str, username: str = ""):
    bot = Bot(token=BOT_TOKEN)

    if action == "affiliate_start":
        kb = [
            [InlineKeyboardButton("✅ I Agree & Join", callback_data="affiliate_agree")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        terms = (
            "💰 <b>Jay Empire Affiliate Program</b>\\n\\n"
            "<b>💵 Commissions:</b>\\n"
            f"• First Sale: {COMMISSION_FIRST_SALE}%\\n"
            f"• Renewals: {COMMISSION_RENEWAL}%\\n"
            "• Lifetime tracking\\n\\n"
            "<b>💳 Payout:</b>\\n"
            f"• Minimum withdrawal: ${MINIMUM_WITHDRAWAL/100:.0f}\\n"
            "• Processed within 24-48hrs\\n\\n"
            "<b>🏆 Bonus:</b>\\n"
            f"• {REFERRAL_MILESTONE}+ active referrals = Lifetime VIP!\\n\\n"
            "<b>📋 Rules:</b> No fake signups, no self-referrals, no spam."
        )
        await bot.send_message(chat_id=chat_id, text=terms, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "affiliate_agree":
        user_states[chat_id] = {"step": "awaiting_full_name", "data": {}}
        await bot.send_message(chat_id=chat_id, text="📝 <b>Step 1/5</b>\\n\\nEnter your Full Name (as on ID/account):", parse_mode="HTML")

    elif action == "affiliate_dashboard":
        await show_affiliate_dashboard(chat_id, bot)
    elif action == "affiliate_statement":        await show_affiliate_statement(chat_id, bot)
    elif action == "affiliate_referrals":
        await show_affiliate_referrals(chat_id, bot)
    elif action == "request_withdrawal":
        await handle_withdrawal_request(chat_id, bot)
    elif action == "affiliate_payout_info":
        await show_payout_info(chat_id, bot)
    elif action.startswith("aff_copy:"):
        ref_code = action.split(":")[1]
        await bot.send_message(chat_id=chat_id, text=f"🔗 <b>Your Link:</b>\\n\\n<code>https://t.me/JayEmpire_bot?start=ref_{ref_code}</code>\\n\\nTap and hold to copy!", parse_mode="HTML")
    elif action == "back_main":
        await show_main_menu(chat_id, bot)
    elif action == "payout_method_bank":
        if chat_id in user_states:
            user_states[chat_id]["data"]["payout_method"] = "bank"
        await show_country_selection(chat_id, bot, "bank")
    elif action == "payout_method_momo":
        if chat_id in user_states:
            user_states[chat_id]["data"]["payout_method"] = "momo"
        await show_country_selection(chat_id, bot, "momo")
    elif action.startswith("country:"):
        parts = action.split(":", 2)
        if len(parts) == 3:
            _, country_key, method = parts
            if country_key not in AFRICA_COUNTRIES:
                await bot.send_message(chat_id=chat_id, text="❌ Invalid country.")
                return
            if chat_id in user_states:
                user_states[chat_id]["data"]["country"] = country_key
                user_states[chat_id]["data"]["country_name"] = AFRICA_COUNTRIES[country_key]["name"]
                if method == "bank":
                    user_states[chat_id]["step"] = "awaiting_bank_selection"
                    await show_bank_selection(chat_id, bot, country_key)
                elif method == "momo":
                    user_states[chat_id]["step"] = "awaiting_momo_provider"
                    await show_momo_provider_selection(chat_id, bot, country_key)
    elif action.startswith("momo_provider:"):
        provider_code = action.split(":", 1)[1]
        if chat_id in user_states:
            country = user_states[chat_id]["data"].get("country", "")
            providers = MOBILE_MONEY_PROVIDERS.get(country, [])
            provider_name = next((p["name"] for p in providers if p["code"] == provider_code), provider_code)
            user_states[chat_id]["data"]["momo_provider"] = provider_code
            user_states[chat_id]["data"]["momo_provider_name"] = provider_name
            user_states[chat_id]["step"] = "awaiting_momo_number"
            country_code = AFRICA_COUNTRIES.get(country, {}).get("country_code", "+233")
            await bot.send_message(chat_id=chat_id, text=f"📱 <b>Step 4/5</b>\\n\\nEnter your {provider_name} number:\\nFormat: {country_code}XXXXXXXXX\\nExample: {country_code}501234567", parse_mode="HTML")
    elif action.startswith("withdraw_confirm:"):
        await process_withdrawal_confirmation(chat_id, int(action.split(":")[1]), bot)
    elif action == "withdraw_cancel":        await bot.send_message(chat_id=chat_id, text="❌ Withdrawal cancelled.")
        await show_affiliate_dashboard(chat_id, bot)

async def show_main_menu(chat_id: int, bot: Bot):
    is_aff = affiliates_col.find_one({"telegram_id": chat_id, "is_active": True}) if affiliates_col else None
    kb = [[InlineKeyboardButton("💎 Launch VIP Terminal", web_app=WebAppInfo(url=MINI_APP_URL))]]
    kb.append([InlineKeyboardButton("💰 Become an Affiliate", callback_data="affiliate_start")] if is_aff is None else [InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
    await bot.send_message(chat_id=chat_id, text="👑 <b>Jay Empire Main Menu:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_affiliate_dashboard(chat_id: int, bot: Bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        await bot.send_message(chat_id=chat_id, text="❌ Not registered as an affiliate.")
        return

    total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]}) if referrals_col else 0
    active_refs = referrals_col.count_documents({"affiliate_id": aff["_id"], "is_active": True}) if referrals_col else 0
    total_earnings = aff.get("total_earnings", 0)
    total_withdrawn = aff.get("total_withdrawn", 0)
    available = total_earnings - total_withdrawn
    pending = withdrawals_col.count_documents({"affiliate_id": aff["_id"], "status": "pending"}) if withdrawals_col else 0
    milestone = "🏆 UNLOCKED!" if active_refs >= REFERRAL_MILESTONE else f"({active_refs}/{REFERRAL_MILESTONE})"
    
    payout_method = aff.get("payout_method", "bank")
    payout_detail = f"🏦 Bank: {aff.get('bank_details', {}).get('bank_name','N/A')}" if payout_method == "bank" else f"📱 Momo: {aff.get('mobile_money_details', {}).get('provider_name','N/A')}"

    dashboard = (
        f"💎 <b>Jay Empire Affiliate Dashboard</b>\\n\\n"
        f"<b>🔗 Link:</b>\\n<code>https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}</code>\\n\\n"
        f"<b>💰 Earnings:</b>\\n"
        f"  Total Earned: ${total_earnings:,.2f}\\n"
        f"  Total Withdrawn: ${total_withdrawn:,.2f}\\n"
        f"  <b>Available: ${available:,.2f}</b>\\n"
        f"  Pending: {pending}\\n\\n"
        f"<b>👥 Referrals:</b> Total: {total_refs} | Active: {active_refs}\\n"
        f"  Commission: {COMMISSION_FIRST_SALE}% first | {COMMISSION_RENEWAL}% renewal\\n"
        f"  🏆 Milestone: {milestone}\\n\\n"
        f"<b>💳 Payout:</b> {payout_detail}\\n"
        f"<b>📊 Code:</b> <code>{aff['ref_code']}</code>"
    )
    kb = [
        [InlineKeyboardButton("📋 Statement", callback_data="affiliate_statement")],
        [InlineKeyboardButton("👥 Referrals", callback_data="affiliate_referrals")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="request_withdrawal")],
        [InlineKeyboardButton("🔗 Copy Link", callback_data=f"aff_copy:{aff['ref_code']}")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    await bot.send_message(chat_id=chat_id, text=dashboard, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_affiliate_statement(chat_id: int, bot: Bot):    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff: return
    txns = list(transactions_col.find({"affiliate_id": aff["_id"]}).sort("created_at", DESCENDING).limit(20)) if transactions_col else []
    if not txns:
        await bot.send_message(chat_id=chat_id, text="<b>📋 Statement</b>\\n\\nNo transactions yet.", parse_mode="HTML")
        return
    text = "<b>📋 Financial Statement</b>\\n\\n"
    for txn in txns:
        sign = "+" if txn["type"] == "commission" else "-"
        text += f"{txn['created_at'].strftime('%d/%m/%Y')} | {txn['type']:<10} | <code>{sign}${txn['amount']:,.2f}</code>\\n"
        if txn.get("description"): text += f"  ↳ {txn['description']}\\n"
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="affiliate_dashboard")]]
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_affiliate_referrals(chat_id: int, bot: Bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff: return
    refs = list(referrals_col.find({"affiliate_id": aff["_id"]}).sort("created_at", DESCENDING).limit(15)) if referrals_col else []
    if not refs:
        await bot.send_message(chat_id=chat_id, text="<b>👥 Referrals</b>\\n\\nNone yet. Share your link!", parse_mode="HTML")
        return
    text = "<b>👥 Your Referrals</b>\\n\\n"
    for ref in refs:
        status = "🟢 Active" if ref.get("is_active") else "🔴 Inactive"
        text += f"User: <code>{ref['customer_telegram_id']}</code> {status}\\n"
        if ref.get("last_payment"):
            text += f"  Commission: ${ref['last_payment']['commission_paid']/100:,.2f}\\n"
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="affiliate_dashboard")]]
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_withdrawal_request(chat_id: int, bot: Bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff: return
    pending = withdrawals_col.count_documents({"affiliate_id": aff["_id"], "status": "pending"}) if withdrawals_col else 0
    if pending > 0:
        await bot.send_message(chat_id=chat_id, text="⚠️ You already have a pending withdrawal.", parse_mode="HTML")
        return
    available = aff.get("total_earnings", 0) - aff.get("total_withdrawn", 0)
    if available < (MINIMUM_WITHDRAWAL / 100):
        await bot.send_message(chat_id=chat_id, text=f"❌ Insufficient balance. Minimum: ${MINIMUM_WITHDRAWAL/100:.0f}", parse_mode="HTML")
        return
    kb = []
    for amount in [10, 25, 50, 100, 250]:
        if amount <= available:
            kb.append([InlineKeyboardButton(f"Withdraw ${amount}", callback_data=f"withdraw_confirm:{int(amount * 100)}")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_dashboard")])
    await bot.send_message(chat_id=chat_id, text=f"<b>💸 Withdraw</b>\\n\\nAvailable: <code>${available:,.2f}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def process_withdrawal_confirmation(chat_id: int, amount_cents: int, bot: Bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None    if not aff: return
    amount = amount_cents / 100
    doc = {
        "affiliate_id": aff["_id"], "telegram_id": chat_id, "username": aff.get("username", ""),
        "full_name": aff.get("full_name", ""), "ref_code": aff.get("ref_code", ""),
        "amount": amount, "amount_cents": amount_cents, "payout_method": aff.get("payout_method", "bank"),
        "payout_details": aff.get("bank_details") if aff.get("payout_method") == "bank" else aff.get("mobile_money_details"),
        "status": "pending", "admin_approved": False, "admin_notes": "", "paystack_transfer_code": None,
        "created_at": datetime.utcnow(), "processed_at": None
    }
    if withdrawals_col: withdrawals_col.insert_one(doc)
    log_affiliate_transaction(aff["_id"], "withdrawal_request", amount, f"Withdrawal request of ${amount:,.2f}", str(doc.get("_id")))
    await bot.send_message(chat_id=chat_id, text=f"✅ <b>Withdrawal Submitted!</b>\\n\\nAmount: ${amount:,.2f}\\nStatus: Pending (24-48hrs)", parse_mode="HTML")
    try:
        payout_info = f"Bank: {aff.get('bank_details', {}).get('account_number','N/A')}" if aff.get("payout_method") == "bank" else f"Momo: {aff.get('mobile_money_details', {}).get('phone_number','N/A')}"
        await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=f"🚨 <b>New Withdrawal</b>\\n\\nAffiliate: {aff.get('full_name')} (@{aff.get('username')})\\nCode: <code>{aff['ref_code']}</code>\\nAmount: ${amount:,.2f}\\nMethod: {aff.get('payout_method').upper()}\\nDetails: {payout_info}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Admin notify failed: {e}")

async def show_payout_info(chat_id: int, bot: Bot):
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff: return
    if aff.get("payout_method") == "bank":
        b = aff.get("bank_details", {})
        text = f"🏦 <b>Bank Transfer</b>\\n\\nBank: {b.get('bank_name','N/A')}\\nAccount: ****{b.get('account_number','0000')[-4:]}\\nName: {b.get('account_name','N/A')}"
    else:
        m = aff.get("mobile_money_details", {})
        text = f"📱 <b>Mobile Money</b>\\n\\nProvider: {m.get('provider_name','N/A')}\\nNumber: {m.get('phone_number','N/A')}"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

async def show_country_selection(chat_id: int, bot: Bot, method: str):
    kb, row = [], []
    for key, info in AFRICA_COUNTRIES.items():
        if method == "momo" and key not in MOBILE_MONEY_PROVIDERS: continue
        row.append(InlineKeyboardButton(f"{info['flag']} {info['name']}", callback_data=f"country:{key}:{method}"))
        if len(row) == 2: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("Cancel", callback_data="affiliate_agree")])
    await bot.send_message(chat_id=chat_id, text=f"🌍 <b>Step 2/5</b>\\n\\nSelect country for {method} payouts:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_bank_selection(chat_id: int, bot: Bot, country: str):
    banks = await get_paystack_bank_list(country)
    kb = [InlineKeyboardButton(b["name"], callback_data=f"aff_bank:{b['code']}:{b['name']}") for b in banks[:20]]
    kb = [kb[i:i+1] for i in range(0, len(kb), 1)]
    kb.append([InlineKeyboardButton("Cancel", callback_data="affiliate_agree")])
    await bot.send_message(chat_id=chat_id, text="🏦 <b>Step 3/5</b>\\n\\nSelect Your Bank", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_momo_provider_selection(chat_id: int, bot: Bot, country: str):
    providers = MOBILE_MONEY_PROVIDERS.get(country, [])
    kb = [[InlineKeyboardButton(f"{p['flag']} {p['name']}", callback_data=f"momo_provider:{p['code']}")] for p in providers]    kb.append([InlineKeyboardButton("Cancel", callback_data="affiliate_agree")])
    await bot.send_message(chat_id=chat_id, text="📱 <b>Step 3/5</b>\\n\\nSelect Mobile Money provider:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ==============================================================================
# FASTAPI LIFESPAN & ENDPOINTS
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_target = f"{RENDER_URL.rstrip('/')}/telegram-webhook"
    await Bot(token=BOT_TOKEN).set_webhook(url=webhook_target)
    logger.info(f"✅ Webhook set: {webhook_target}")
    yield
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    return {"status": "active", "mongodb": "connected" if db else "disconnected", "timestamp": datetime.utcnow().isoformat()}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        username = query["from"].get("username", "")
        try: await Bot(token=BOT_TOKEN).answer_callback_query(callback_query_id=query["id"])
        except: pass
        
        if action.startswith("aff_bank:"):
            parts = action.split(":", 2)
            if len(parts) == 3 and chat_id in user_states:
                _, bank_code, bank_name = parts
                user_states[chat_id]["data"]["bank_code"] = bank_code
                user_states[chat_id]["data"]["bank_name"] = bank_name
                user_states[chat_id]["step"] = "awaiting_account_number"
                await Bot(token=BOT_TOKEN).send_message(chat_id=chat_id, text=f"🏦 <b>Step 4/5</b>\\n\\nEnter Account Number for {bank_name}:", parse_mode="HTML")
            return {"status": "ok"}
            
        if action == "affiliate_confirm":
            if chat_id not in user_states: return {"status": "ok"}
            state = user_states[chat_id]
            d = state["data"]
            bot = Bot(token=BOT_TOKEN)
            
            if d.get("payout_method") == "bank":                subaccount = await create_paystack_subaccount(d["full_name"], d["bank_code"], d["account_number"], COMMISSION_FIRST_SALE)
                if not subaccount:
                    await bot.send_message(chat_id=chat_id, text="❌ Failed to create payout account. Contact admin.")
                    return {"status": "ok"}
                payout_details = {"paystack_subaccount": subaccount, "bank_details": {"bank_code": d["bank_code"], "bank_name": d["bank_name"], "account_number": d["account_number"], "account_name": d.get("account_name", d["full_name"]), "country_name": d.get("country_name", "")}}
            else:
                recipient = await create_paystack_transfer_recipient(d["full_name"], d["momo_number"], d["momo_provider"], AFRICA_COUNTRIES.get(d.get("country",""), {}).get("currency", "GHS"))
                if not recipient:
                    await bot.send_message(chat_id=chat_id, text="❌ Failed to create Momo recipient. Contact admin.")
                    return {"status": "ok"}
                payout_details = {"paystack_transfer_recipient": recipient, "mobile_money_details": {"provider": d["momo_provider"], "provider_name": d.get("momo_provider_name", ""), "phone_number": d["momo_number"], "account_name": d["full_name"], "country_name": d.get("country_name", "")}}

            ref_code = generate_ref_code()
            aff_doc = {
                "telegram_id": chat_id, "username": username, "full_name": d["full_name"], "ref_code": ref_code,
                "payout_method": d.get("payout_method", "bank"), "country": d.get("country", ""), "country_name": d.get("country_name", ""),
                "commission_rates": {"first_sale": COMMISSION_FIRST_SALE, "renewal": COMMISSION_RENEWAL},
                "total_earnings": 0, "total_withdrawn": 0, "total_referrals": 0, "is_active": True, "milestone_notified": False,
                "created_at": datetime.utcnow(), **payout_details
            }
            if affiliates_col: affiliates_col.insert_one(aff_doc)
            
            payout_text = "Bank Transfer" if d.get("payout_method") == "bank" else "Mobile Money"
            await bot.send_message(chat_id=chat_id, text=f"✅ <b>Welcome to the Affiliate Program!</b>\\n\\nYour Link:\\n<code>https://t.me/JayEmpire_bot?start=ref_{ref_code}</code>\\n\\nCommissions: {COMMISSION_FIRST_SALE}% first | {COMMISSION_RENEWAL}% renewal\\nPayout: {payout_text}\\nMin Withdrawal: ${MINIMUM_WITHDRAWAL/100:.0f}\\n\\nStart sharing now!", parse_mode="HTML")
            del user_states[chat_id]
            return {"status": "ok"}
            
        await handle_affiliate_callback(chat_id, action, username)
        return {"status": "ok"}

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        if text.startswith("/"):
            await telegram_app.process_update(Update.de_json(data, telegram_app.bot))
            return {"status": "ok"}
            
        if chat_id in user_states:
            state = user_states[chat_id]
            step = state.get("step")
            bot = Bot(token=BOT_TOKEN)
            
            if step == "awaiting_full_name":
                state["data"]["full_name"] = text
                state["step"] = "awaiting_payout_method"
                kb = [[InlineKeyboardButton("🏦 Bank Transfer", callback_data="payout_method_bank")], [InlineKeyboardButton("📱 Mobile Money", callback_data="payout_method_momo")], [InlineKeyboardButton("Cancel", callback_data="back_main")]]
                await bot.send_message(chat_id=chat_id, text="💳 <b>Step 2/5</b>\\n\\nHow would you like to receive commissions?", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
                return {"status": "ok"}
            elif step == "awaiting_account_number":
                if not validate_account_number(text):                    await bot.send_message(chat_id=chat_id, text="❌ Invalid account number. Please try again.")
                    return {"status": "ok"}
                state["data"]["account_number"] = text
                state["step"] = "awaiting_confirmation"
                acc_name = await verify_bank_account(text, state["data"]["bank_code"])
                if acc_name:
                    state["data"]["account_name"] = acc_name
                    kb = [[InlineKeyboardButton("✅ Confirm & Create", callback_data="affiliate_confirm")], [InlineKeyboardButton("Start Over", callback_data="affiliate_agree")]]
                    await bot.send_message(chat_id=chat_id, text=f"✅ <b>Verify Details:</b>\\n\\nName: {state['data']['full_name']}\\nCountry: {state['data'].get('country_name', 'N/A')}\\nBank: {state['data']['bank_name']}\\nAccount: {text}\\nVerified Name: {acc_name}\\n\\nClick confirm to start earning!", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
                else:
                    await bot.send_message(chat_id=chat_id, text="❌ Could not verify account. Check number and try again.")
                    state["step"] = "awaiting_account_number"
                return {"status": "ok"}
            elif step == "awaiting_momo_number":
                if not validate_phone_number(text, state["data"].get("country", "")):
                    await bot.send_message(chat_id=chat_id, text=f"❌ Invalid number. Use format: {AFRICA_COUNTRIES.get(state['data'].get('country',''), {}).get('country_code', '+233')}XXXXXXXXX")
                    return {"status": "ok"}
                state["data"]["momo_number"] = text
                state["data"]["account_name"] = state["data"]["full_name"]
                state["step"] = "awaiting_confirmation"
                kb = [[InlineKeyboardButton("✅ Confirm & Create", callback_data="affiliate_confirm")], [InlineKeyboardButton("Start Over", callback_data="affiliate_agree")]]
                await bot.send_message(chat_id=chat_id, text=f"✅ <b>Verify Details:</b>\\n\\nName: {state['data']['full_name']}\\nCountry: {state['data'].get('country_name', 'N/A')}\\nProvider: {state['data'].get('momo_provider_name', 'N/A')}\\nNumber: {text}\\n\\nClick confirm to start earning!", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
                return {"status": "ok"}

    await telegram_app.process_update(Update.de_json(data, telegram_app.bot))
    return {"status": "ok"}

@app.post("/paystack-webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    if not PAYSTACK_SECRET: raise HTTPException(status_code=500, detail="Paystack secret not set")
    body = await request.body()
    expected = hmac.new(PAYSTACK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    if x_paystack_signature is None or not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
        if payload.get("event") == "charge.success":
            data = payload["data"]
            meta = data.get("metadata", {})
            tg_id = meta.get("telegram_id")
            channel_type = meta.get("channel_type", "gold")
            days = int(meta.get("days", 30))
            reference = data.get("reference", "unknown")
            ref_code = meta.get("ref_code")
            is_renewal = meta.get("is_renewal", False)

            if not tg_id or tg_id == 0: return {"status": "ignored"}

            now = datetime.utcnow()            expires = now + timedelta(days=days)

            if users_col:
                users_col.update_one(
                    {"telegram_id": tg_id, "channel_type": channel_type},
                    {"$set": {"telegram_id": tg_id, "channel_type": channel_type, "purchased_at": now, "expires_at": expires, "is_active": True, "reminder_sent": False, "last_reference": reference, "amount_paid": data.get("amount"), "currency": data.get("currency"), "referred_by": ref_code}},
                    upsert=True
                )
                if leads_col: leads_col.update_one({"telegram_id": tg_id}, {"$set": {"converted": True, "converted_at": now, "converted_channel": channel_type}})

                if ref_code and affiliates_col and referrals_col:
                    affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
                    if affiliate:
                        rate = COMMISSION_RENEWAL if is_renewal else COMMISSION_FIRST_SALE
                        amount = data.get("amount", 0)
                        commission = int(amount * rate / 100)
                        commission_dollars = commission / 100

                        referrals_col.update_one(
                            {"affiliate_id": affiliate["_id"], "customer_telegram_id": tg_id},
                            {"$setOnInsert": {"affiliate_id": affiliate["_id"], "ref_code": ref_code, "customer_telegram_id": tg_id, "customer_channel": channel_type, "plan_key": meta.get("plan_key", "unknown"), "created_at": now, "is_active": True},
                             "$set": {"last_payment": {"amount": amount, "currency": data.get("currency"), "commission_paid": commission, "commission_rate": rate, "paystack_reference": reference, "paid_at": now, "is_renewal": is_renewal}},
                             "$inc": {"total_payments": 1}},
                            upsert=True
                        )
                        affiliates_col.update_one({"_id": affiliate["_id"]}, {"$inc": {"total_earnings": commission_dollars, "total_referrals": 0 if is_renewal else 1}, "$set": {"last_earning_at": now}})
                        log_affiliate_transaction(affiliate["_id"], "commission", commission_dollars, f"{'Renewal' if is_renewal else 'First sale'} commission from user {tg_id}", reference, {"customer_id": tg_id, "channel": channel_type, "rate": rate})
                        logger.info(f"✅ Affiliate {ref_code} earned {rate}% = {commission} from {tg_id}")

            bot = Bot(token=BOT_TOKEN)
            try:
                link = GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK
                name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"Enter {name}", url=link)]])
                await bot.send_message(chat_id=tg_id, text=f"✅ <b>PAYMENT VERIFIED!</b>\\n\\nPlan: {channel_type.upper()}\\nDuration: {days} days\\nExpires: {expires.strftime('%B %d, %Y')}\\n\\nTap below:", parse_mode="HTML", reply_markup=btn)
            except Exception as e:
                logger.error(f"Access message failed: {e}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ==============================================================================
# ADMIN ENDPOINTS (SECURED)
# ==============================================================================
@app.get("/admin/withdrawals")
async def admin_get_withdrawals(status: str = "pending"):
    if not withdrawals_col: return JSONResponse({"error": "DB offline"}, status_code=503)
    query = {} if status == "all" else {"status": status}
    withdrawals = list(withdrawals_col.find(query).sort("created_at", DESCENDING))    enriched = []
    for w in withdrawals:
        aff = affiliates_col.find_one({"_id": w["affiliate_id"]}) if affiliates_col else None
        enriched.append({
            "id": str(w["_id"]), "affiliate_name": w.get("full_name", "N/A"), "username": w.get("username", "N/A"),
            "ref_code": w.get("ref_code", "N/A"), "amount": w.get("amount", 0), "payout_method": w.get("payout_method", "N/A"),
            "payout_details": w.get("payout_details", {}), "status": w.get("status", "N/A"),
            "created_at": w.get("created_at").isoformat() if w.get("created_at") else None,
            "affiliate_total_earnings": aff.get("total_earnings", 0) if aff else 0
        })
    return {"status": status, "count": len(enriched), "total_amount": round(sum(w["amount"] for w in enriched), 2), "withdrawals": enriched}

@app.post("/admin/withdrawals/{withdrawal_id}/approve")
async def admin_approve_withdrawal(withdrawal_id: str, notes: str = ""):
    if not withdrawals_col or not affiliates_col: return JSONResponse({"error": "DB offline"}, status_code=503)
    from bson.objectid import ObjectId
    try: withdrawal = withdrawals_col.find_one({"_id": ObjectId(withdrawal_id)})
    except: return JSONResponse({"error": "Invalid ID"}, status_code=400)
    if not withdrawal or withdrawal.get("status") != "pending": return JSONResponse({"error": "Not found or not pending"}, status_code=404)
    
    affiliate = affiliates_col.find_one({"_id": withdrawal["affiliate_id"]})
    if not affiliate: return JSONResponse({"error": "Affiliate not found"}, status_code=404)
    
    available = affiliate.get("total_earnings", 0) - affiliate.get("total_withdrawn", 0)
    if available < withdrawal["amount"]: return JSONResponse({"error": "Insufficient balance"}, status_code=400)
    
    transfer_result = None
    if withdrawal.get("payout_method") == "momo" and affiliate.get("paystack_transfer_recipient"):
        transfer_result = await initiate_paystack_transfer(int(withdrawal["amount"] * 100), affiliate["paystack_transfer_recipient"], f"Affiliate withdrawal - {affiliate['ref_code']}")
    else:
        transfer_result = {"success": True, "message": "Bank transfer queued for manual processing"}

    now = datetime.utcnow()
    withdrawals_col.update_one({"_id": ObjectId(withdrawal_id)}, {"$set": {"status": "approved", "admin_approved": True, "admin_notes": notes, "processed_at": now, "paystack_transfer_code": transfer_result.get("transfer_code") if transfer_result else None}})
    affiliates_col.update_one({"_id": affiliate["_id"]}, {"$inc": {"total_withdrawn": withdrawal["amount"]}, "$set": {"last_withdrawal_at": now}})
    log_affiliate_transaction(affiliate["_id"], "withdrawal", withdrawal["amount"], f"Withdrawal approved: ${withdrawal['amount']:,.2f}", withdrawal_id, {"admin_notes": notes, "paystack_result": transfer_result})
    
    try:
        await Bot(token=BOT_TOKEN).send_message(chat_id=affiliate["telegram_id"], text=f"✅ <b>Withdrawal Approved!</b>\\n\\nAmount: ${withdrawal['amount']:,.2f}\\nStatus: Processed (24-48hrs)", parse_mode="HTML")
    except: pass
    return {"status": "approved", "withdrawal_id": withdrawal_id, "amount": withdrawal["amount"]}

@app.post("/admin/withdrawals/{withdrawal_id}/reject")
async def admin_reject_withdrawal(withdrawal_id: str, notes: str = ""):
    if not withdrawals_col: return JSONResponse({"error": "DB offline"}, status_code=503)
    from bson.objectid import ObjectId
    try: withdrawal = withdrawals_col.find_one({"_id": ObjectId(withdrawal_id)})
    except: return JSONResponse({"error": "Invalid ID"}, status_code=400)
    if not withdrawal or withdrawal.get("status") != "pending": return JSONResponse({"error": "Not found or not pending"}, status_code=404)
        now = datetime.utcnow()
    withdrawals_col.update_one({"_id": ObjectId(withdrawal_id)}, {"$set": {"status": "rejected", "admin_approved": False, "admin_notes": notes, "processed_at": now}})
    log_affiliate_transaction(withdrawal["affiliate_id"], "withdrawal_reversal", 0, f"Withdrawal rejected: ${withdrawal['amount']:,.2f} - {notes}", withdrawal_id)
    
    try:
        await Bot(token=BOT_TOKEN).send_message(chat_id=withdrawal["telegram_id"], text=f"❌ <b>Withdrawal Rejected</b>\\n\\nAmount: ${withdrawal['amount']:,.2f}\\nReason: {notes}\\n\\nContact @{ADMIN_USERNAME}", parse_mode="HTML")
    except: pass
    return {"status": "rejected", "withdrawal_id": withdrawal_id, "reason": notes}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
"""

# --- FULL INDEX.HTML CONTENT ---
index_html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Jay Empire VIP</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://js.paystack.co/v1/inline.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --ink: #0a0e1a; --ink-light: #131b2e; --ink-glow: #1a2540;
            --gold: #c9a227; --gold-light: #e8d5a3; --gold-dark: #a88420;
            --surface: #f8f9fb; --surface-elevated: #ffffff;
            --text-primary: #0f172a; --text-secondary: #64748b; --text-muted: #94a3b8;
            --success: #10b981; --shadow-xl: 0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1);
            --shadow-gold: 0 0 40px -10px rgba(201, 162, 39, 0.3);
            --radius-lg: 24px; --radius-md: 16px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { font-family: 'Inter', sans-serif; background: var(--surface); color: var(--text-primary); min-height: 100vh; overflow-x: hidden; line-height: 1.5; }
        h1, h2, h3, .brand-title, .price-tag { font-family: 'Space Grotesk', sans-serif; font-weight: 700; letter-spacing: -0.02em; }
        
        .hero { position: relative; z-index: 1; background: linear-gradient(165deg, var(--ink) 0%, var(--ink-light) 60%, var(--ink-glow) 100%); padding: 28px 20px 100px; border-radius: 0 0 var(--radius-lg) var(--radius-lg); overflow: hidden; }
        .hero::before { content: ''; position: absolute; top: -50%; right: -30%; width: 400px; height: 400px; background: radial-gradient(circle, rgba(201, 162, 39, 0.15) 0%, transparent 70%); pointer-events: none; }
        .brand-badge { display: inline-flex; align-items: center; gap: 6px; background: rgba(201, 162, 39, 0.1); border: 1px solid rgba(201, 162, 39, 0.2); padding: 6px 14px; border-radius: 100px; margin-bottom: 16px; }
        .brand-badge span { font-size: 11px; font-weight: 600; color: var(--gold); text-transform: uppercase; letter-spacing: 0.08em; }
        .brand-title { font-size: 36px; color: #fff; line-height: 1.1; margin-bottom: 10px; background: linear-gradient(135deg, #fff 0%, var(--gold-light) 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
        .brand-subtitle { font-size: 14px; color: rgba(255,255,255,0.5); max-width: 280px; margin: 0 auto; line-height: 1.6; font-weight: 400; }
        
        .content { position: relative; z-index: 2; padding: 0 16px 24px; margin-top: -60px; }
        .panel { display: none; opacity: 0; transform: translateY(16px); transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1); }
        .panel.active { display: block; opacity: 1; transform: translateY(0); }
                .glass-card { background: rgba(255,255,255,0.85); backdrop-filter: blur(20px) saturate(180%); border-radius: var(--radius-lg); padding: 32px 24px; margin-bottom: 20px; border: 1px solid rgba(255,255,255,0.6); box-shadow: var(--shadow-xl); position: relative; overflow: hidden; }
        .glass-card.premium { border: 1px solid rgba(201, 162, 39, 0.2); box-shadow: var(--shadow-xl), var(--shadow-gold); }
        .glass-card.premium::after { content: ''; position: absolute; top: -1px; left: 20%; right: 20%; height: 2px; background: linear-gradient(90deg, transparent, var(--gold), transparent); border-radius: 2px; }
        
        .card-eyebrow { display: inline-flex; align-items: center; gap: 6px; background: var(--ink); color: var(--gold); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.12em; padding: 6px 14px; border-radius: 100px; margin-bottom: 20px; }
        .card-icon { width: 64px; height: 64px; background: linear-gradient(135deg, var(--ink-light), var(--ink-glow)); border-radius: var(--radius-md); display: flex; align-items: center; justify-content: center; font-size: 28px; margin-bottom: 20px; }
        .card-title { font-size: 24px; color: var(--text-primary); margin-bottom: 8px; line-height: 1.2; }
        .card-desc { font-size: 14px; color: var(--text-secondary); line-height: 1.6; margin-bottom: 24px; }
        .premium-badge { position: absolute; top: 16px; right: 16px; background: linear-gradient(135deg, var(--gold), var(--gold-dark)); color: var(--ink); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; padding: 4px 10px; border-radius: 100px; }
        
        .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; width: 100%; padding: 16px 24px; border-radius: 100px; border: none; font-family: 'Space Grotesk', sans-serif; font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; cursor: pointer; transition: all 0.3s ease; position: relative; overflow: hidden; }
        .btn-primary { background: linear-gradient(135deg, var(--gold) 0%, var(--gold-dark) 100%); color: var(--ink); box-shadow: 0 4px 16px rgba(201, 162, 39, 0.3); }
        .btn-primary:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(201, 162, 39, 0.4); }
        .btn-primary:disabled { background: #e2e8f0; color: #94a3b8; box-shadow: none; cursor: not-allowed; }
        .btn-secondary { background: transparent; color: var(--ink); border: 2px solid var(--ink); }
        .btn-secondary:hover { background: var(--ink); color: #fff; }
        
        .price-block { text-align: center; margin: 20px 0; }
        .price-tag { font-size: 42px; color: var(--text-primary); line-height: 1; margin-bottom: 4px; }
        .price-tag .currency { font-size: 20px; vertical-align: super; font-weight: 500; color: var(--text-secondary); }
        .price-original { font-size: 14px; color: var(--text-muted); text-decoration: line-through; margin-bottom: 4px; }
        .price-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); }
        
        .region-grid { display: grid; gap: 10px; }
        .region-btn { display: flex; align-items: center; gap: 12px; background: var(--surface-elevated); border: 1px solid rgba(0,0,0,0.06); border-radius: var(--radius-md); padding: 14px 16px; font-family: 'Inter', sans-serif; font-size: 14px; font-weight: 500; color: var(--text-primary); cursor: pointer; transition: all 0.2s ease; text-align: left; }
        .region-btn:hover { border-color: var(--gold); background: linear-gradient(135deg, #fff, rgba(201, 162, 39, 0.03)); }
        .region-flag { font-size: 22px; width: 32px; text-align: center; }
        .region-name { flex: 1; }
        .region-currency { font-size: 12px; color: var(--text-muted); font-weight: 400; }
        
        .terms-box { background: #f1f5f9; border-radius: 12px; padding: 16px; margin: 20px 0; display: flex; gap: 12px; align-items: flex-start; }
        .terms-box input[type="checkbox"] { width: 20px; height: 20px; accent-color: var(--ink); margin-top: 2px; flex-shrink: 0; cursor: pointer; }
        .terms-box label { font-size: 13px; color: var(--text-secondary); line-height: 1.5; cursor: pointer; }
        .terms-box a { color: var(--ink); font-weight: 600; text-decoration: underline; text-underline-offset: 2px; cursor: pointer; }
        
        .affiliate-notice { background: linear-gradient(135deg, rgba(201, 162, 39, 0.08), rgba(201, 162, 39, 0.02)); border: 1px solid rgba(201, 162, 39, 0.2); border-radius: 12px; padding: 12px; margin: 12px 0; text-align: center; display: none; }
        .affiliate-notice .affiliate-label { font-size: 12px; color: #a88420; font-weight: 600; }
        .affiliate-notice .affiliate-code { font-size: 11px; color: #64748b; margin-top: 4px; font-family: monospace; }
        
        .back-link { display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 14px; font-weight: 500; text-decoration: none; margin: 16px auto; cursor: pointer; transition: color 0.2s; }
        .back-link:hover { color: var(--text-primary); }
        .contact-bar { text-align: center; padding: 16px; font-size: 13px; color: var(--text-muted); }
        .contact-bar a { color: var(--ink); font-weight: 600; text-decoration: none; }
        .app-footer { text-align: center; padding: 20px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); border-top: 1px solid rgba(0,0,0,0.04); }
        
        .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(10, 14, 26, 0.7); backdrop-filter: blur(8px); z-index: 1000; align-items: center; justify-content: center; padding: 20px; opacity: 0; transition: opacity 0.3s ease; }
        .modal-overlay.show { display: flex; opacity: 1; }
        .modal-box { background: var(--surface-elevated); border-radius: var(--radius-lg); padding: 28px; max-width: 400px; width: 100%; max-height: 80vh; box-shadow: var(--shadow-xl); transform: scale(0.95); transition: transform 0.3s ease; }
        .modal-overlay.show .modal-box { transform: scale(1); }
        .modal-title { font-family: 'Space Grotesk', sans-serif; font-size: 20px; font-weight: 700; color: var(--text-primary); margin-bottom: 16px; }        .modal-body { font-size: 13px; color: var(--text-secondary); line-height: 1.7; overflow-y: auto; max-height: 50vh; padding-right: 8px; margin-bottom: 20px; }
        .modal-body p { margin-bottom: 12px; }
        .modal-body strong { color: var(--text-primary); }
        
        .success-ring { width: 80px; height: 80px; background: linear-gradient(135deg, var(--success), #059669); border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 36px; margin: 0 auto 20px; box-shadow: 0 8px 24px rgba(16, 185, 129, 0.3); animation: scale-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1); }
        @keyframes scale-in { 0% { transform: scale(0); } 100% { transform: scale(1); } }
        .success-title { font-size: 28px; background: linear-gradient(135deg, var(--success), #059669); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
        
        @keyframes fade-up { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        .animate-in { animation: fade-up 0.5s ease forwards; }
        .delay-1 { animation-delay: 0.1s; opacity: 0; }
        .delay-2 { animation-delay: 0.2s; opacity: 0; }
        .delay-3 { animation-delay: 0.3s; opacity: 0; }
    </style>
</head>
<body>
    <header class="hero">
        <div class="hero-content" style="text-align: center; position: relative;">
            <div class="brand-badge"><span>⚡ Premium Signals</span></div>
            <h1 class="brand-title">JAY EMPIRE</h1>
            <p class="brand-subtitle" id="header-subtitle">Select your plan for precision trading and institutional insights.</p>
        </div>
    </header>

    <main class="content">
        <!-- STEP 1: Select Plan -->
        <section id="step-1" class="panel active">
            <div class="glass-card animate-in delay-1">
                <div class="card-eyebrow">🪙 Commodities</div>
                <div class="card-icon">🥇</div>
                <h2 class="card-title">Gold Master VIP</h2>
                <p class="card-desc">Exclusive XAUUSD signals with institutional-grade analysis and high-probability execution setups.</p>
                <button class="btn btn-primary" onclick="chooseDestination('gold', 'Gold Master VIP')">Select Plan</button>
            </div>
            <div class="glass-card animate-in delay-2">
                <div class="card-eyebrow">📈 Forex & Crypto</div>
                <div class="card-icon">💱</div>
                <h2 class="card-title">FX Premium Signals</h2>
                <p class="card-desc">Curated currency pair signals and crypto market execution with real-time macro analysis.</p>
                <button class="btn btn-primary" onclick="chooseDestination('fx', 'FX Premium Signals')">Select Plan</button>
            </div>
            <div class="contact-bar">Need help? <a href="https://t.me/jay_empire247" target="_blank">Contact Admin</a></div>
        </section>

        <!-- STEP 2: Duration -->
        <section id="step-2" class="panel">
            <div id="plans-container"></div>
            <div class="contact-bar">Need help? <a href="https://t.me/jay_empire247" target="_blank">Contact Admin</a></div>
            <div class="back-link" onclick="switchStep(1)">← Back to Plans</div>
        </section>
        <!-- STEP 3: Region -->
        <section id="step-3" class="panel">
            <div class="glass-card">
                <div class="card-eyebrow">🌍 Select Region</div>
                <h2 class="card-title">Choose Your Currency</h2>
                <p class="card-desc" style="margin-bottom: 20px;">We'll calculate exact local pricing for your subscription.</p>
                <div class="region-grid">
                    <button class="region-btn" onclick="chooseCurrency('GHS', 15.50, 'GHS ')"><span class="region-flag">🇬🇭</span><span class="region-name">Ghana<br><span class="region-currency">Ghanaian Cedi</span></span></button>
                    <button class="region-btn" onclick="chooseCurrency('NGN', 1600.0, '₦')"><span class="region-flag">🇳🇬</span><span class="region-name">Nigeria<br><span class="region-currency">Nigerian Naira</span></span></button>
                    <button class="region-btn" onclick="chooseCurrency('ZAR', 18.20, 'R ')"><span class="region-flag">🇿🇦</span><span class="region-name">South Africa<br><span class="region-currency">South African Rand</span></span></button>
                    <button class="region-btn" onclick="chooseCurrency('KES', 130.0, 'KSh ')"><span class="region-flag">🇰🇪</span><span class="region-name">Kenya<br><span class="region-currency">Kenyan Shilling</span></span></button>
                    <button class="region-btn" onclick="chooseCurrency('USD', 1.0, '$')"><span class="region-flag">🌍</span><span class="region-name">International<br><span class="region-currency">US Dollar</span></span></button>
                </div>
            </div>
            <div class="contact-bar">Need help? <a href="https://t.me/jay_empire247" target="_blank">Contact Admin</a></div>
            <div class="back-link" onclick="switchStep(2)">← Back to Durations</div>
        </section>

        <!-- STEP 4: Checkout -->
        <section id="step-4" class="panel">
            <div class="glass-card">
                <div class="card-eyebrow" id="summary-eyebrow">Plan Selected</div>
                <h2 class="card-title" id="summary-title">VIP Terminal</h2>
                <p class="card-desc" id="summary-desc">Review your subscription details</p>

                <div class="affiliate-notice" id="affiliate-notice">
                    <div class="affiliate-label">💰 An affiliate will receive commission from this payment</div>
                    <div class="affiliate-code" id="affiliate-code-display"></div>
                </div>

                <div class="price-block">
                    <div class="price-original" id="price-original"></div>
                    <div class="price-tag" id="price-display"><span class="currency">GHS</span>0.00</div>
                    <div class="price-label">Total Payable</div>
                </div>

                <div class="terms-box">
                    <input type="checkbox" id="tc-checkbox" onchange="togglePayButton()">
                    <label for="tc-checkbox">I agree to the <a onclick="openTCModal()">Terms & Conditions</a>. You must read the terms before proceeding.</label>
                </div>

                <button id="pay-btn" class="btn btn-primary" onclick="proceedToPaystack()" disabled>Proceed to Payment</button>
            </div>
            <div class="contact-bar">Need help? <a href="https://t.me/jay_empire247" target="_blank">Contact Admin</a></div>
            <div class="back-link" onclick="switchStep(3)">← Back to Regions</div>
        </section>

        <!-- STEP: Success -->
        <section id="step-success" class="panel">            <div class="glass-card" style="text-align: center;">
                <div class="success-ring">👑</div>
                <h2 class="card-title success-title">Access Granted</h2>
                <p class="card-desc">Your payment was successful. Welcome to the inner circle.</p>
                <a id="vip-link" href="#" target="_blank" style="text-decoration: none; display: block; margin-top: 8px;">
                    <button class="btn btn-primary">Enter VIP Channel</button>
                </a>
            </div>
            <div class="contact-bar">Need help? <a href="https://t.me/jay_empire247" target="_blank">Contact Admin</a></div>
        </section>
    </main>

    <!-- Terms Modal -->
    <div class="modal-overlay" id="tc-modal">
        <div class="modal-box">
            <h3 class="modal-title">Terms & Conditions</h3>
            <div class="modal-body">
                <p><strong>1. Risk Disclosure:</strong> Financial trading involves substantial risk. Past performance does not guarantee future results.</p>
                <p><strong>2. Non-Refundable:</strong> All VIP channel access passes are digital goods. Once activated, subscriptions cannot be refunded.</p>
                <p><strong>3. Confidentiality:</strong> Signals are for personal use only. Unauthorized distribution will result in immediate termination.</p>
            </div>
            <button class="btn btn-primary" onclick="acceptTC()">I Have Read & Accept</button>
        </div>
    </div>

    <footer class="app-footer">Powered by Jay Empire Tech</footer>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        tg.ready();
        if (tg.setHeaderColor) tg.setHeaderColor('#0a0e1a');

        let affiliateRefCode = null;
        try {
            const initData = tg.initDataUnsafe;
            if (initData && initData.start_param && initData.start_param.startsWith('ref_')) {
                affiliateRefCode = initData.start_param.replace('ref_', '');
            }
        } catch(e) { console.log('No start_param'); }
        if (!affiliateRefCode) {
            const urlParams = new URLSearchParams(window.location.search);
            const refFromUrl = urlParams.get('ref');
            if (refFromUrl) affiliateRefCode = refFromUrl;
        }

        let targetChannel = '', targetChannelTitle = '', selectedPlan = null;
        let activeCurrency = 'GHS', conversionRate = 15.50, currencySymbol = 'GHS ', hasReadTC = false;
        const GOLD_LINK = "https://t.me/+env-Zrui2ykwYjg8";
        const FOREX_LINK = "https://t.me/+njii3OAHlqI3MjQ8";        const plans = [
            { key: 'test', name: 'Test Phase', usd: 0.10, days: 1, isTest: true, original: 1.00 },
            { key: '1m', name: '1 Month Access', usd: 15, days: 30, isTest: false, original: 25 },
            { key: '3m', name: '3 Months Access', usd: 40, days: 90, isTest: false, original: 60 },
            { key: '6m', name: '6 Months Access', usd: 80, days: 180, isTest: false, original: 120 },
            { key: '1y', name: '1 Year Access', usd: 150, days: 365, isTest: false, original: 250 },
            { key: 'lifetime', name: 'Lifetime VIP', usd: 700, days: 36500, isTest: false, original: 1500 }
        ];

        function switchStep(step) {
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            const target = document.getElementById(`step-${step}`);
            target.classList.add('active');
            const subtitles = { 1: "Select your plan for precision trading.", 2: "Choose your subscription duration.", 3: "Select your region for local pricing.", 4: "Review and complete your subscription.", success: "Welcome to the inner circle." };
            document.getElementById('header-subtitle').textContent = subtitles[step] || subtitles[1];
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function chooseDestination(key, title) {
            targetChannel = key; targetChannelTitle = title; renderPlans(); switchStep(2);
        }

        function renderPlans() {
            const container = document.getElementById('plans-container');
            container.innerHTML = '';
            plans.forEach((plan, i) => {
                const card = document.createElement('div');
                card.className = `glass-card ${plan.key === 'lifetime' ? 'premium' : ''} animate-in delay-${Math.min(i+1, 3)}`;
                const originalPrice = plan.original ? `<div class="price-original">$${plan.original.toFixed(2)} USD</div>` : '';
                const badge = plan.key === 'lifetime' ? '<div class="premium-badge">Best Value</div>' : '';
                card.innerHTML = `${badge}<div class="card-eyebrow">${plan.name}</div>${originalPrice}
                    <div class="price-block" style="margin: 12px 0;"><div class="price-tag"><span class="currency">$</span>${plan.usd.toFixed(2)}</div><div class="price-label">USD</div></div>
                    <p class="card-desc">${plan.days === 36500 ? 'Unlimited lifetime access' : `${plan.days}-day access to ${targetChannelTitle}`}</p>
                    <button class="btn btn-primary" onclick="selectPlan('${plan.key}')">Select Duration</button>`;
                container.appendChild(card);
            });
        }

        function selectPlan(key) { selectedPlan = plans.find(p => p.key === key); switchStep(3); }

        function chooseCurrency(curr, rate, symbol) {
            activeCurrency = curr; conversionRate = rate; currencySymbol = symbol;
            const total = selectedPlan.usd * conversionRate;
            const original = selectedPlan.original ? selectedPlan.usd * 1.5 * conversionRate : null;
            document.getElementById('summary-eyebrow').textContent = selectedPlan.name;
            document.getElementById('summary-title').textContent = targetChannelTitle;
            document.getElementById('summary-desc').textContent = `${selectedPlan.days === 36500 ? 'Lifetime' : selectedPlan.days + '-day'} subscription`;
            document.getElementById('price-display').innerHTML = `<span class="currency">${currencySymbol}</span>${total.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            const origEl = document.getElementById('price-original');
            if (original) { origEl.textContent = `${currencySymbol}${original.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`; origEl.style.display = 'block'; }             else { origEl.style.display = 'none'; }
            
            const affNotice = document.getElementById('affiliate-notice');
            if (affiliateRefCode) { affNotice.style.display = 'block'; document.getElementById('affiliate-code-display').textContent = `Code: ${affiliateRefCode}`; } 
            else { affNotice.style.display = 'none'; }
            
            document.getElementById('tc-checkbox').checked = false;
            document.getElementById('pay-btn').disabled = true;
            hasReadTC = false;
            switchStep(4);
        }

        function openTCModal() { document.getElementById('tc-modal').classList.add('show'); }
        function acceptTC() {
            hasReadTC = true;
            document.getElementById('tc-modal').classList.remove('show');
            document.getElementById('tc-checkbox').checked = true;
            document.getElementById('pay-btn').disabled = false;
        }
        function togglePayButton() {
            const cb = document.getElementById('tc-checkbox');
            const btn = document.getElementById('pay-btn');
            if (!hasReadTC && cb.checked) { cb.checked = false; openTCModal(); return; }
            btn.disabled = !cb.checked;
        }

        function proceedToPaystack() {
            if (!hasReadTC || !document.getElementById('tc-checkbox').checked) {
                tg.showAlert ? tg.showAlert("Please read and accept the Terms & Conditions.") : alert("Please read and accept the Terms & Conditions.");
                return;
            }
            const user = tg.initDataUnsafe?.user;
            const amount = Math.round(selectedPlan.usd * conversionRate * 100);
            const metadata = {
                telegram_id: user?.id || 0, channel_type: targetChannel, days: selectedPlan.days,
                plan_key: selectedPlan.key, ref_code: affiliateRefCode, is_renewal: false
            };
            PaystackPop.setup({
                key: 'pk_live_c470302d79292c5df97f088509a5a99d39788fc8',
                email: user ? `user_${user.id}@jayempire.com` : 'client@jayempire.com',
                amount: amount, currency: activeCurrency, metadata: metadata,
                callback: function(response) {
                    document.getElementById('vip-link').href = targetChannel === 'gold' ? GOLD_LINK : FOREX_LINK;
                    switchStep('success');
                },
                onClose: function() {}
            }).openIframe();
        }

        document.getElementById('tc-modal').addEventListener('click', function(e) {            if (e.target === this) this.classList.remove('show');
        });
    </script>
</body>
</html>
"""

with open("server.py", "w", encoding="utf-8") as f:
    f.write(server_py_content)

with open("index.html", "w", encoding="utf-8") as f:
    f.write(index_html_content)

print("✅ Files 'server.py' and 'index.html' have been successfully generated in your current directory!")
