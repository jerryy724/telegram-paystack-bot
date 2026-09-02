"""
Jay Empire VIP Telegram Bot + Paystack + Affiliate System
Production-oriented FastAPI backend for Render + MongoDB Atlas.

Important:
- Never put PAYSTACK_SECRET_KEY, BOT_TOKEN, MONGO_URI or ADMIN_API_KEY in frontend code.
- Configure the environment variables listed in the deployment notes.
- Paystack recipient types are country/currency specific. Ghana bank payouts use `ghipss`;
  Ghana/Kenya mobile money uses `mobile_money`.
"""

import os
import asyncio
import hashlib
import hmac
import logging
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import certifi
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.server_api import ServerApi
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("jay_empire")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip().strip('"').strip("'")

MINI_APP_URL = os.getenv(
    "MINI_APP_URL",
    "https://jerryy724.github.io/telegram-paystack-bot/",
).strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID", "").strip()
FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID", "").strip()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "jay_empire247").lstrip("@")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or 0)
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

# This must match the country/currency enabled for transfers on your Paystack
# business. The code does not pretend that one Ghana Paystack account can
# automatically pay out from unsupported countries.
PAYSTACK_ACCOUNT_COUNTRY = os.getenv("PAYSTACK_ACCOUNT_COUNTRY", "ghana").lower()
PAYOUT_FX_RATES = {
    # USD ledger -> local payout currency. Change these deliberately.
    "GHS": Decimal(os.getenv("PAYOUT_RATE_GHS", "15.50")),
    "NGN": Decimal(os.getenv("PAYOUT_RATE_NGN", "1600")),
    "ZAR": Decimal(os.getenv("PAYOUT_RATE_ZAR", "18.20")),
    "KES": Decimal(os.getenv("PAYOUT_RATE_KES", "130")),
}

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
    "GHS": 15.50,
    "NGN": 1600.0,
    "ZAR": 18.20,
    "KES": 130.0,
    "USD": 1.0,
    "GBP": 0.78,
    "EUR": 0.92,
    "XOF": 605.0,
}

COMMISSION_FIRST_SALE = Decimal("50")
COMMISSION_RENEWAL = Decimal("35")
REFERRAL_MILESTONE = 10

COUNTRIES = {
    "ghana": {"name": "Ghana", "currency": "GHS", "flag": "🇬🇭"},
    "nigeria": {"name": "Nigeria", "currency": "NGN", "flag": "🇳🇬"},
    "kenya": {"name": "Kenya", "currency": "KES", "flag": "🇰🇪"},
    "south_africa": {"name": "South Africa", "currency": "ZAR", "flag": "🇿🇦"},
}
MOMO_COUNTRIES = {"ghana", "kenya"}

# ---------------------------------------------------------------------------
# Validation / utilities
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def minor_units(value: Decimal) -> int:
    return int((money(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generate_ref_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "JAY" + "".join(secrets.choice(alphabet) for _ in range(8))


def require_configuration() -> None:
    missing = []
    for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
        "PAYSTACK_SECRET_KEY": PAYSTACK_SECRET,
        "MONGO_URI": MONGO_URI,
        "MINI_APP_URL": MINI_APP_URL,
        "RENDER_EXTERNAL_URL": RENDER_URL,
        "GOLD_CHANNEL_ID": GOLD_CHANNEL_ID,
        "FOREX_CHANNEL_ID": FOREX_CHANNEL_ID,
    }.items():
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

mongo_client = None
db = None
users_col = None
leads_col = None
affiliates_col = None
referrals_col = None
withdrawals_col = None
transactions_col = None
webhook_events_col = None


def init_mongodb():
    global mongo_client, db
    global users_col, leads_col, affiliates_col, referrals_col
    global withdrawals_col, transactions_col, webhook_events_col

    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured")

    mongo_client = MongoClient(
        MONGO_URI,
        tls=True,
        tlsCAFile=certifi.where(),
        server_api=ServerApi("1"),
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=10000,
        socketTimeoutMS=30000,
        retryWrites=True,
        maxPoolSize=30,
    )
    mongo_client.admin.command("ping")
    db = mongo_client.get_default_database()

    users_col = db["vip_users"]
    leads_col = db["leads"]
    affiliates_col = db["affiliates"]
    referrals_col = db["referrals"]
    withdrawals_col = db["withdrawals"]
    transactions_col = db["affiliate_transactions"]
    webhook_events_col = db["webhook_events"]

    users_col.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
    leads_col.create_index("telegram_id", unique=True)
    affiliates_col.create_index("telegram_id", unique=True)
    affiliates_col.create_index("ref_code", unique=True)
    referrals_col.create_index([("affiliate_id", ASCENDING), ("customer_telegram_id", ASCENDING)], unique=True)
    withdrawals_col.create_index([("affiliate_id", ASCENDING), ("status", ASCENDING)])
    withdrawals_col.create_index([("affiliate_id", ASCENDING), ("created_at", DESCENDING)])
    transactions_col.create_index([("affiliate_id", ASCENDING), ("created_at", DESCENDING)])
    webhook_events_col.create_index("reference", unique=True)
    logger.info("MongoDB connected")


# ---------------------------------------------------------------------------
# Paystack
# ---------------------------------------------------------------------------

PAYSTACK_BASE = "https://api.paystack.co"


def paystack_headers() -> dict:
    if not PAYSTACK_SECRET:
        raise RuntimeError("PAYSTACK_SECRET_KEY is not configured")
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json",
    }


async def paystack_request(method: str, path: str, **kwargs) -> dict:
    timeout = kwargs.pop("timeout", 20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            PAYSTACK_BASE + path,
            headers=paystack_headers(),
            **kwargs,
        )
    try:
        data = response.json()
    except Exception:
        data = {"status": False, "message": response.text}
    if response.status_code >= 400 or not data.get("status"):
        logger.error(
            "Paystack %s %s failed status=%s message=%s",
            method,
            path,
            response.status_code,
            data.get("message"),
        )
    return data


async def get_paystack_bank_list(country: str, account_type: str, currency: str) -> list:
    # Paystack's bank list is currency/type based. Filtering the returned
    # records by country makes the UI deterministic.
    params = {"currency": currency}
    if account_type == "mobile_money":
        params["type"] = "mobile_money"
    data = await paystack_request("GET", "/bank", params=params, timeout=15.0)
    if not data.get("status"):
        return []
    wanted = COUNTRIES.get(country, {}).get("name", "").lower()
    return [
        item for item in data.get("data", [])
        if str(item.get("country", "")).lower() == wanted
        and item.get("active", True)
        and not item.get("is_deleted", False)
    ]


async def verify_bank_account(account_number: str, bank_code: str) -> Optional[str]:
    data = await paystack_request(
        "GET",
        "/bank/resolve",
        params={"account_number": account_number, "bank_code": bank_code},
        timeout=15.0,
    )
    if data.get("status"):
        return data.get("data", {}).get("account_name")
    return None


async def create_transfer_recipient(
    *,
    name: str,
    account_number: str,
    bank_code: str,
    currency: str,
    recipient_type: str,
) -> Optional[str]:
    data = await paystack_request(
        "POST",
        "/transferrecipient",
        json={
            "type": recipient_type,
            "name": name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": currency,
        },
        timeout=20.0,
    )
    if data.get("status"):
        return data["data"]["recipient_code"]
    return None


async def initialize_paystack_transaction(
    email: str,
    amount_minor: int,
    currency: str,
    reference: str,
    metadata: dict,
) -> dict:
    return await paystack_request(
        "POST",
        "/transaction/initialize",
        json={
            "email": email,
            "amount": amount_minor,
            "currency": currency,
            "reference": reference,
            "metadata": metadata,
        },
        timeout=20.0,
    )


async def verify_paystack_transaction(reference: str) -> Optional[dict]:
    data = await paystack_request(
        "GET",
        f"/transaction/verify/{reference}",
        timeout=20.0,
    )
    return data.get("data") if data.get("status") else None


async def initiate_transfer(
    *,
    amount_minor: int,
    currency: str,
    recipient_code: str,
    reason: str,
    reference: str,
) -> dict:
    return await paystack_request(
        "POST",
        "/transfer",
        json={
            "source": "balance",
            "amount": amount_minor,
            "recipient": recipient_code,
            "reason": reason[:100],
            "reference": reference,
        },
        timeout=20.0,
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

telegram_app: Optional[Application] = None
user_states = {}  # Short-lived onboarding state; payout data is persisted in MongoDB.

def main_keyboard(affiliate: Optional[dict] = None):
    rows = [
        [InlineKeyboardButton("Subscribe 📈", web_app=WebAppInfo(url=MINI_APP_URL))]
    ]
    if affiliate:
        rows.append([InlineKeyboardButton("📊 My Affiliate Dashboard", callback_data="affiliate_dashboard")])
    else:
        rows.append([InlineKeyboardButton("🤝 Become an Affiliate", callback_data="affiliate_start")])
    return InlineKeyboardMarkup(rows)


async def send_main_menu(chat_id: int):
    bot = Bot(BOT_TOKEN)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "👑 <b>JAY EMPIRE VIP TERMINAL</b>\n"
            "<i>Success Is Our Aim</i>\n\n"
            "📈 <b>Subscribe</b> — access premium Gold &amp; FX signals.\n"
            "🤝 <b>Become an Affiliate</b> — share your link and earn commission.\n\n"
            "Choose an option below:"
        ),
        parse_mode="HTML",
        reply_markup=main_keyboard(
            affiliates_col.find_one({"telegram_id": chat_id, "is_active": True})
            if affiliates_col is not None else None
        ),
    )


async def start_cmd(update: Update, context):
    user = update.effective_user
    if not user or not update.message:
        return

    chat_id = user.id
    text = update.message.text or ""
    ref_code = None
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
        if payload.startswith("ref_"):
            ref_code = payload[4:].strip().upper()[:32]

    if leads_col is not None:
        leads_col.update_one(
            {"telegram_id": chat_id},
            {
                "$set": {
                    "first_name": user.first_name or "",
                    "username": user.username or "",
                    "last_started_at": utcnow(),
                },
                "$setOnInsert": {
                    "telegram_id": chat_id,
                    "started_at": utcnow(),
                    "converted": False,
                    "followup_sent": False,
                    "referred_by": ref_code,
                },
            },
            upsert=True,
        )

    if ref_code:
        user_states[chat_id] = {"referred_by": ref_code}

    await send_main_menu(chat_id)


async def callback_handler(update: Update, context):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await handle_affiliate_callback(
        query.from_user.id,
        query.data or "",
        query.from_user.username or "",
    )


async def handle_affiliate_callback(chat_id: int, action: str, username: str = ""):
    bot = Bot(BOT_TOKEN)

    if action == "affiliate_start":
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "👑 <b>Jay Empire Affiliate Program</b>\n\n"
                f"💰 First sale: <b>{COMMISSION_FIRST_SALE}%</b>\n"
                f"🔁 Renewals: <b>{COMMISSION_RENEWAL}%</b>\n"
                f"🏆 {REFERRAL_MILESTONE}+ active referrals unlock Lifetime VIP.\n\n"
                "💸 Withdraw your available balance at any time. "
                "There is no minimum withdrawal threshold; Paystack availability and "
                "your configured payout currency still apply.\n\n"
                "📋 No fake signups, self-referrals or spam.\n\n"
                "Tap below to join and configure your payout account."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ I Agree & Join", callback_data="affiliate_agree")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
            ]),
        )
        return

    if action == "affiliate_agree":
        user_states[chat_id] = {"step": "full_name", "data": {}}
        await bot.send_message(
            chat_id=chat_id,
            text="Step 1/5\n\nEnter your <b>full name</b> exactly as registered on your payout account.",
            parse_mode="HTML",
        )
        return

    if action == "affiliate_dashboard":
        await show_affiliate_dashboard(chat_id)
        return

    if action == "affiliate_statement":
        await show_affiliate_statement(chat_id)
        return

    if action == "affiliate_referrals":
        await show_affiliate_referrals(chat_id)
        return

    if action == "request_withdrawal":
        await handle_withdrawal_request(chat_id)
        return

    if action == "affiliate_payout_info":
        await show_payout_info(chat_id)
        return

    if action == "back_main":
        await send_main_menu(chat_id)
        return

    if action == "affiliate_cancel":
        user_states.pop(chat_id, None)
        await send_main_menu(chat_id)
        return

    state = user_states.get(chat_id)

    if action.startswith("payout_method:") and state:
        method = action.split(":", 1)[1]
        if method not in {"bank", "momo"}:
            return
        state["data"]["payout_method"] = method
        state["step"] = "country"
        await show_country_selection(chat_id, method)
        return

    if action.startswith("country:") and state:
        _, country, method = action.split(":", 2)
        if country not in COUNTRIES or method != state["data"].get("payout_method"):
            return
        if method == "momo" and country not in MOMO_COUNTRIES:
            await bot.send_message(chat_id=chat_id, text="Mobile Money is currently available only for Ghana and Kenya on Paystack.")
            return
        if country != PAYSTACK_ACCOUNT_COUNTRY:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Automated payout is not enabled for {COUNTRIES[country]['name']} "
                    f"on this Paystack account. Select {COUNTRIES[PAYSTACK_ACCOUNT_COUNTRY]['name']} "
                    "or contact the admin."
                ),
            )
            return

        state["data"]["country"] = country
        state["data"]["country_name"] = COUNTRIES[country]["name"]
        state["data"]["currency"] = COUNTRIES[country]["currency"]

        if method == "bank":
            state["step"] = "bank"
            await show_bank_selection(chat_id, country)
        else:
            state["step"] = "momo"
            await show_momo_selection(chat_id, country)
        return

    if action.startswith("bank:") and state:
        _, code, name = action.split(":", 2)
        state["data"]["bank_code"] = code
        state["data"]["bank_name"] = name
        state["step"] = "account_number"
        await bot.send_message(chat_id=chat_id, text=f"Step 4/5\n\nEnter your {html_escape(name)} account number:")
        return

    if action.startswith("momo:") and state:
        _, code, name = action.split(":", 2)
        state["data"]["momo_provider"] = code
        state["data"]["momo_provider_name"] = name
        state["step"] = "momo_number"
        await bot.send_message(chat_id=chat_id, text=f"Step 4/5\n\nEnter your {html_escape(name)} mobile-money number:")
        return

    if action == "affiliate_confirm" and state:
        await finalize_affiliate(chat_id, username)
        return

    if action.startswith("withdraw:"):
        try:
            cents = int(action.split(":", 1)[1])
        except ValueError:
            return
        await process_withdrawal(chat_id, cents)
        return


async def show_country_selection(chat_id: int, method: str):
    bot = Bot(BOT_TOKEN)
    rows = []
    for key, info in COUNTRIES.items():
        if method == "momo" and key not in MOMO_COUNTRIES:
            continue
        rows.append([InlineKeyboardButton(
            f"{info['flag']} {info['name']}",
            callback_data=f"country:{key}:{method}",
        )])
    rows.append([InlineKeyboardButton("Cancel", callback_data="affiliate_cancel")])
    await bot.send_message(
        chat_id=chat_id,
        text=f"Step 2/5\n\nChoose your <b>{'Mobile Money' if method == 'momo' else 'Bank Transfer'}</b> payout country:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_bank_selection(chat_id: int, country: str):
    bot = Bot(BOT_TOKEN)
    currency = COUNTRIES[country]["currency"]
    banks = await get_paystack_bank_list(country, "bank", currency)
    if not banks:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ No supported bank list was returned by Paystack. Please try again later or contact the admin.",
        )
        return
    rows = [
        [InlineKeyboardButton(
            str(b["name"])[:60],
            callback_data=f"bank:{b['code']}:{str(b['name'])[:45]}",
        )]
        for b in banks[:40]
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="affiliate_cancel")])
    await bot.send_message(
        chat_id=chat_id,
        text="Step 3/5\n\nSelect your bank:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_momo_selection(chat_id: int, country: str):
    bot = Bot(BOT_TOKEN)
    currency = COUNTRIES[country]["currency"]
    providers = await get_paystack_bank_list(country, "mobile_money", currency)
    if not providers:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Paystack returned no Mobile Money providers for this currency. Please try again later.",
        )
        return
    rows = [
        [InlineKeyboardButton(
            str(p["name"])[:60],
            callback_data=f"momo:{p['code']}:{str(p['name'])[:45]}",
        )]
        for p in providers[:20]
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="affiliate_cancel")])
    await bot.send_message(
        chat_id=chat_id,
        text="Step 3/5\n\nSelect your Mobile Money provider:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def finalize_affiliate(chat_id: int, username: str):
    state = user_states.get(chat_id)
    if not state:
        return
    d = state["data"]
    bot = Bot(BOT_TOKEN)

    full_name = d.get("full_name", "").strip()
    country = d.get("country")
    currency = d.get("currency")
    method = d.get("payout_method")

    if not full_name or not country or not currency or not method:
        await bot.send_message(chat_id=chat_id, text="Your payout setup is incomplete. Please start again.")
        return

    recipient_type = "ghipss" if method == "bank" and currency == "GHS" else (
        "nuban" if method == "bank" and currency == "NGN" else (
            "basa" if method == "bank" and currency == "ZAR" else (
                "kepss" if method == "bank" and currency == "KES" else "mobile_money"
            )
        )
    )

    if method == "bank":
        account_number = d.get("account_number", "").strip()
        bank_code = d.get("bank_code", "")
        account_name = await verify_bank_account(account_number, bank_code)
        if not account_name:
            await bot.send_message(
                chat_id=chat_id,
                text="❌ Paystack could not verify that account. Check the account number and try again.",
            )
            state["step"] = "account_number"
            return
        d["account_name"] = account_name
    else:
        account_number = d.get("momo_number", "").strip()
        bank_code = d.get("momo_provider", "")
        d["account_name"] = full_name

    recipient = await create_transfer_recipient(
        name=full_name,
        account_number=account_number,
        bank_code=bank_code,
        currency=currency,
        recipient_type=recipient_type,
    )
    if not recipient:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ Paystack could not create the payout recipient.\n\n"
                "This is normally caused by an unsupported country/currency, invalid "
                "bank/telco code, account details, or a Paystack transfer capability "
                "restriction. Please check the details and try again."
            ),
        )
        return

    ref_code = generate_ref_code()
    payout_details = {
        "paystack_transfer_recipient": recipient,
        "payout_currency": currency,
        "payout_country": country,
        "account_name": d.get("account_name", full_name),
    }
    if method == "bank":
        payout_details["bank_details"] = {
            "bank_code": d["bank_code"],
            "bank_name": d["bank_name"],
            "account_number": account_number,
            "account_name": d["account_name"],
            "country": country,
            "country_name": d["country_name"],
        }
    else:
        payout_details["mobile_money_details"] = {
            "provider": d["momo_provider"],
            "provider_name": d["momo_provider_name"],
            "phone_number": account_number,
            "account_name": d["account_name"],
            "country": country,
            "country_name": d["country_name"],
        }

    aff = {
        "telegram_id": chat_id,
        "username": username,
        "full_name": full_name,
        "ref_code": ref_code,
        "payout_method": method,
        "country": country,
        "country_name": d["country_name"],
        "payout_currency": currency,
        "commission_rates": {
            "first_sale": float(COMMISSION_FIRST_SALE),
            "renewal": float(COMMISSION_RENEWAL),
        },
        "total_earnings": 0.0,  # USD ledger
        "total_withdrawn": 0.0,  # USD ledger
        "total_referrals": 0,
        "is_active": True,
        "milestone_notified": False,
        "created_at": utcnow(),
        **payout_details,
    }

    try:
        affiliates_col.update_one(
            {"telegram_id": chat_id},
            {"$set": aff, "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )
    except Exception as exc:
        logger.exception("Affiliate save failed: %s", exc)
        await bot.send_message(chat_id=chat_id, text="❌ Your payout account was created but the affiliate record could not be saved. Contact the admin.")
        return

    user_states.pop(chat_id, None)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🎉 <b>Affiliate account activated</b>\n\n"
            f"🔗 Your referral link:\n<code>https://t.me/JayEmpire_bot?start=ref_{ref_code}</code>\n\n"
            f"💰 Commission: {COMMISSION_FIRST_SALE}% first sale • {COMMISSION_RENEWAL}% renewal\n"
            f"💳 Payout: {method.title()} • {currency}\n"
            "💸 Withdrawals: available anytime when your balance is above zero.\n\n"
            "Share your link to start earning."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Open Dashboard", callback_data="affiliate_dashboard")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")],
        ]),
    )


async def show_affiliate_dashboard(chat_id: int):
    bot = Bot(BOT_TOKEN)
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate yet.")
        return

    earned = money(aff.get("total_earnings", 0))
    withdrawn = money(aff.get("total_withdrawn", 0))
    available = max(Decimal("0"), earned - withdrawn)
    total_refs = referrals_col.count_documents({"affiliate_id": aff["_id"]}) if referrals_col else 0
    active_refs = referrals_col.count_documents({"affiliate_id": aff["_id"], "is_active": True}) if referrals_col else 0

    payout = aff.get("payout_currency", "GHS")
    payout_method = aff.get("payout_method", "bank")

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "💎 <b>JAY EMPIRE AFFILIATE</b>\n\n"
            f"🔗 <b>Referral Link</b>\n<code>https://t.me/JayEmpire_bot?start=ref_{aff['ref_code']}</code>\n\n"
            "💰 <b>Wallet</b>\n"
            f"• Earned: <b>${earned:,.2f}</b>\n"
            f"• Withdrawn: ${withdrawn:,.2f}\n"
            f"• Available: <b>${available:,.2f}</b>\n\n"
            "👥 <b>Referrals</b>\n"
            f"• Total: {total_refs}\n"
            f"• Active: {active_refs}\n"
            f"• Commission: {COMMISSION_FIRST_SALE}% / {COMMISSION_RENEWAL}%\n\n"
            "💳 <b>Payout</b>\n"
            f"• {payout_method.title()} • {payout}\n"
            "• No minimum withdrawal threshold"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Withdraw", callback_data="request_withdrawal")],
            [InlineKeyboardButton("📋 Statement", callback_data="affiliate_statement")],
            [InlineKeyboardButton("👥 Referrals", callback_data="affiliate_referrals")],
            [InlineKeyboardButton("💳 Payout Info", callback_data="affiliate_payout_info")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")],
        ]),
    )


async def show_affiliate_statement(chat_id: int):
    bot = Bot(BOT_TOKEN)
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        return
    txns = list(
        transactions_col.find({"affiliate_id": aff["_id"]}, {"_id": 0})
        .sort("created_at", DESCENDING).limit(25)
    )
    if not txns:
        text = "📋 <b>Statement</b>\n\nNo transactions yet."
    else:
        lines = ["📋 <b>Statement</b>", ""]
        for t in txns:
            sign = "+" if t.get("type") == "commission" else "-"
            lines.append(
                f"{t.get('created_at', utcnow()).strftime('%d/%m/%Y')} "
                f"{sign}${money(t.get('amount', 0)):,.2f} "
                f"{html_escape(t.get('description', ''))}"
            )
        text = "\n".join(lines)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Dashboard", callback_data="affiliate_dashboard")]
        ]),
    )


async def show_affiliate_referrals(chat_id: int):
    bot = Bot(BOT_TOKEN)
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        return
    refs = list(referrals_col.find({"affiliate_id": aff["_id"]}).sort("created_at", DESCENDING).limit(20))
    if not refs:
        text = "👥 <b>Your Referrals</b>\n\nNo referrals yet."
    else:
        lines = ["👥 <b>Your Referrals</b>", ""]
        for ref in refs:
            status = "🟢" if ref.get("is_active") else "⚪"
            lines.append(f"{status} {html_escape(ref.get('customer_telegram_id', ''))} • {ref.get('customer_channel', 'N/A').upper()}")
        text = "\n".join(lines)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Dashboard", callback_data="affiliate_dashboard")]
        ]),
    )


async def show_payout_info(chat_id: int):
    bot = Bot(BOT_TOKEN)
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        return
    method = aff.get("payout_method", "bank")
    currency = aff.get("payout_currency", "GHS")
    if method == "bank":
        d = aff.get("bank_details", {})
        account = str(d.get("account_number", ""))
        masked = ("*" * max(0, len(account) - 4)) + account[-4:] if account else "N/A"
        text = (
            "💳 <b>Payout Information</b>\n\n"
            f"Method: Bank Transfer\nCurrency: {currency}\n"
            f"Bank: {html_escape(d.get('bank_name', 'N/A'))}\n"
            f"Account: <code>{masked}</code>\n"
            f"Account Name: {html_escape(d.get('account_name', 'N/A'))}"
        )
    else:
        d = aff.get("mobile_money_details", {})
        text = (
            "📱 <b>Payout Information</b>\n\n"
            f"Method: Mobile Money\nCurrency: {currency}\n"
            f"Provider: {html_escape(d.get('provider_name', 'N/A'))}\n"
            f"Number: <code>{html_escape(d.get('phone_number', 'N/A'))}</code>\n"
            f"Account Name: {html_escape(d.get('account_name', 'N/A'))}"
        )
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Dashboard", callback_data="affiliate_dashboard")]
        ]),
    )


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------

async def handle_withdrawal_request(chat_id: int):
    bot = Bot(BOT_TOKEN)
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        await bot.send_message(chat_id=chat_id, text="You are not registered as an affiliate.")
        return

    earned = money(aff.get("total_earnings", 0))
    withdrawn = money(aff.get("total_withdrawn", 0))
    available = max(Decimal("0"), earned - withdrawn)
    if available <= 0:
        await bot.send_message(chat_id=chat_id, text="Your available balance is $0.00.")
        return

    # No minimum threshold. We still use a minimum transferable unit of one
    # cent because the USD ledger is stored to cents.
    available_cents = minor_units(available)
    buttons = []
    for cents in [100, 500, 1000, 2500, 5000, 10000]:
        if cents <= available_cents:
            buttons.append([InlineKeyboardButton(f"Withdraw ${cents/100:.2f}", callback_data=f"withdraw:{cents}")])
    buttons.append([InlineKeyboardButton(f"Withdraw Full Balance (${available:,.2f})", callback_data=f"withdraw:{available_cents}")])
    buttons.append([InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_dashboard")])

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "💸 <b>Withdraw Earnings</b>\n\n"
            f"Available: <b>${available:,.2f}</b>\n"
            "No minimum withdrawal threshold.\n\n"
            "Choose an amount:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def process_withdrawal(chat_id: int, requested_cents: int):
    bot = Bot(BOT_TOKEN)
    aff = affiliates_col.find_one({"telegram_id": chat_id}) if affiliates_col else None
    if not aff:
        return

    if requested_cents <= 0:
        return

    requested_usd = money(Decimal(requested_cents) / 100)
    if requested_usd <= 0:
        return

    # Reserve the USD ledger amount atomically. A concurrent withdrawal cannot
    # reserve the same balance.
    updated = affiliates_col.find_one_and_update(
        {
            "_id": aff["_id"],
            "$expr": {
                "$gte": [
                    {"$subtract": ["$total_earnings", "$total_withdrawn"]},
                    float(requested_usd),
                ]
            },
        },
        {"$inc": {"total_withdrawn": float(requested_usd)}, "$set": {"last_withdrawal_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        await bot.send_message(chat_id=chat_id, text="❌ Your available balance changed. Please open the withdrawal screen again.")
        return

    payout_currency = updated.get("payout_currency") or COUNTRIES.get(updated.get("country", ""), {}).get("currency")
    payout_rate = PAYOUT_FX_RATES.get(payout_currency or "")
    recipient = updated.get("paystack_transfer_recipient")

    if not payout_currency or not payout_rate or not recipient:
        affiliates_col.update_one({"_id": aff["_id"]}, {"$inc": {"total_withdrawn": -float(requested_usd)}})
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Automatic payout is not fully configured for your account. Your balance was restored. Please contact the admin.",
        )
        return

    local_amount = money(requested_usd * payout_rate)
    local_minor = minor_units(local_amount)
    transfer_reference = f"JAYWTH-{secrets.token_hex(10).upper()}"

    withdrawal = {
        "affiliate_id": aff["_id"],
        "telegram_id": chat_id,
        "ref_code": aff.get("ref_code"),
        "amount_usd": float(requested_usd),
        "amount": float(requested_usd),  # backward-compatible ledger field
        "payout_amount": float(local_amount),
        "payout_amount_minor": local_minor,
        "payout_currency": payout_currency,
        "payout_rate": float(payout_rate),
        "payout_method": updated.get("payout_method"),
        "payout_details": updated.get("bank_details") if updated.get("payout_method") == "bank" else updated.get("mobile_money_details"),
        "paystack_transfer_recipient": recipient,
        "reference": transfer_reference,
        "status": "processing",
        "created_at": utcnow(),
    }
    result = withdrawals_col.insert_one(withdrawal)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "⏳ <b>Withdrawal submitted</b>\n\n"
            f"Wallet amount: <b>${requested_usd:,.2f}</b>\n"
            f"Payout amount: <b>{payout_currency} {local_amount:,.2f}</b>\n"
            "Paystack is processing the transfer."
        ),
        parse_mode="HTML",
    )

    transfer = await initiate_transfer(
        amount_minor=local_minor,
        currency=payout_currency,
        recipient_code=recipient,
        reason=f"Jay Empire affiliate {aff.get('ref_code', '')}",
        reference=transfer_reference,
    )

    if not transfer.get("status"):
        withdrawals_col.update_one(
            {"_id": result.inserted_id},
            {"$set": {"status": "failed", "failure_reason": transfer.get("message", "Paystack transfer failed"), "processed_at": utcnow()}},
        )
        affiliates_col.update_one({"_id": aff["_id"]}, {"$inc": {"total_withdrawn": -float(requested_usd)}})
        await bot.send_message(
            chat_id=chat_id,
            text="❌ <b>Withdrawal failed</b>\n\nYour wallet balance has been restored. Please contact the admin if the problem continues.",
            parse_mode="HTML",
        )
        return

    transfer_data = transfer.get("data", {})
    withdrawals_col.update_one(
        {"_id": result.inserted_id},
        {"$set": {
            "status": transfer_data.get("status", "pending"),
            "paystack_transfer_code": transfer_data.get("transfer_code"),
            "paystack_response": {
                "status": transfer.get("status"),
                "message": transfer.get("message"),
            },
        }},
    )
    transactions_col.insert_one({
        "affiliate_id": aff["_id"],
        "type": "withdrawal",
        "amount": float(requested_usd),
        "description": f"Withdrawal to {payout_currency} ({local_amount:,.2f})",
        "reference": transfer_reference,
        "created_at": utcnow(),
    })

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ <b>Withdrawal accepted by Paystack</b>\n\n"
            f"Amount: <b>${requested_usd:,.2f}</b>\n"
            f"Payout: <b>{payout_currency} {local_amount:,.2f}</b>\n"
            f"Reference: <code>{transfer_reference}</code>\n\n"
            "You can continue using your affiliate account while the transfer is completed."
        ),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Telegram webhook / onboarding text
# ---------------------------------------------------------------------------

async def process_text_message(chat_id: int, username: str, text: str):
    state = user_states.get(chat_id)
    if not state:
        return False
    bot = Bot(BOT_TOKEN)
    step = state.get("step")

    if step == "full_name":
        if len(text.strip()) < 3 or len(text) > 100:
            await bot.send_message(chat_id=chat_id, text="Please enter a valid full name.")
            return True
        state["data"]["full_name"] = text.strip()
        state["step"] = "method"
        await bot.send_message(
            chat_id=chat_id,
            text="Step 2/5\n\nChoose your payout method:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏦 Bank Transfer", callback_data="payout_method:bank")],
                [InlineKeyboardButton("📱 Mobile Money", callback_data="payout_method:momo")],
                [InlineKeyboardButton("Cancel", callback_data="affiliate_cancel")],
            ]),
        )
        return True

    if step == "account_number":
        account = "".join(ch for ch in text.strip() if ch.isalnum())
        if not (5 <= len(account) <= 30):
            await bot.send_message(chat_id=chat_id, text="Please enter a valid account number.")
            return True
        state["data"]["account_number"] = account
        account_name = await verify_bank_account(account, state["data"]["bank_code"])
        if not account_name:
            await bot.send_message(chat_id=chat_id, text="❌ Paystack could not verify that account. Please check the number and try again.")
            return True
        state["data"]["account_name"] = account_name
        state["step"] = "confirm"
        d = state["data"]
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Step 5/5\n\n"
                f"<b>Name:</b> {html_escape(d['full_name'])}\n"
                f"<b>Bank:</b> {html_escape(d['bank_name'])}\n"
                f"<b>Account:</b> <code>{html_escape(account)}</code>\n"
                f"<b>Verified Name:</b> {html_escape(account_name)}\n\n"
                "Confirm to create your Paystack payout recipient."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm & Create", callback_data="affiliate_confirm")],
                [InlineKeyboardButton("🔄 Start Over", callback_data="affiliate_agree")],
            ]),
        )
        return True

    if step == "momo_number":
        number = text.strip().replace(" ", "")
        if not (8 <= len(number) <= 20):
            await bot.send_message(chat_id=chat_id, text="Please enter a valid Mobile Money number.")
            return True
        state["data"]["momo_number"] = number
        state["step"] = "confirm"
        d = state["data"]
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Step 5/5\n\n"
                f"<b>Name:</b> {html_escape(d['full_name'])}\n"
                f"<b>Provider:</b> {html_escape(d['momo_provider_name'])}\n"
                f"<b>Number:</b> <code>{html_escape(number)}</code>\n\n"
                "Confirm to create your Paystack Mobile Money recipient."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm & Create", callback_data="affiliate_confirm")],
                [InlineKeyboardButton("🔄 Start Over", callback_data="affiliate_agree")],
            ]),
        )
        return True

    return False


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

class InitiatePaymentRequest(BaseModel):
    telegram_id: int = Field(gt=0)
    channel_type: str
    plan_key: str
    currency: str
    ref_code: Optional[str] = None
    email: Optional[str] = None


@app.post("/api/initiate-payment")
async def initiate_payment(payload: InitiatePaymentRequest):
    if payload.channel_type not in {"gold", "fx"}:
        raise HTTPException(400, "Invalid channel")
    plan = PLANS_BY_KEY.get(payload.plan_key)
    if not plan:
        raise HTTPException(400, "Invalid plan")
    if payload.currency not in CURRENCY_RATES:
        raise HTTPException(400, "Invalid currency")
    if users_col is None:
        raise HTTPException(503, "Database unavailable")

    existing = users_col.find_one({
        "telegram_id": payload.telegram_id,
        "channel_type": payload.channel_type,
    })
    if plan["is_test"] and existing and existing.get("test_used"):
        raise HTTPException(400, "Test phase has already been used for this channel")

    is_renewal = bool(
        existing and existing.get("is_active")
        and existing.get("expires_at", datetime.min) > utcnow()
    )
    amount_minor = int(
        (Decimal(str(plan["usd"])) * Decimal(str(CURRENCY_RATES[payload.currency])) * 100)
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    reference = f"JAY-{secrets.token_hex(10).upper()}"
    email = payload.email or f"user_{payload.telegram_id}@jayempire.invalid"

    ref_code = (payload.ref_code or "").strip().upper()[:32] or None
    metadata = {
        "telegram_id": payload.telegram_id,
        "channel_type": payload.channel_type,
        "plan_key": plan["key"],
        "days": plan["days"],
        "currency": payload.currency,
        "ref_code": ref_code,
        "is_renewal": is_renewal,
        "is_test": bool(plan["is_test"]),
    }

    data = await initialize_paystack_transaction(
        email=email,
        amount_minor=amount_minor,
        currency=payload.currency,
        reference=reference,
        metadata=metadata,
    )
    if not data.get("status"):
        raise HTTPException(502, "Paystack could not initialize the payment")

    return {
        "access_code": data["data"]["access_code"],
        "reference": data["data"]["reference"],
    }


# ---------------------------------------------------------------------------
# Paystack webhook
# ---------------------------------------------------------------------------



async def fulfill_successful_payment(data: dict):
    reference = data.get("reference")
    if not reference:
        return

    verified = await verify_paystack_transaction(reference)
    if not verified or verified.get("status") != "success":
        logger.warning("Payment %s did not verify as success", reference)
        return

    if verified.get("amount") != data.get("amount"):
        logger.warning("Amount mismatch for %s", reference)
        return

    metadata = verified.get("metadata") or data.get("metadata") or {}
    try:
        tg_id = int(metadata.get("telegram_id", 0))
    except Exception:
        tg_id = 0
    if tg_id <= 0:
        logger.warning("Payment %s has invalid telegram_id", reference)
        return

    channel = metadata.get("channel_type")
    plan = PLANS_BY_KEY.get(metadata.get("plan_key"))
    if channel not in {"gold", "fx"} or not plan:
        logger.warning("Payment %s has invalid metadata", reference)
        return

    now = utcnow()
    existing = users_col.find_one({"telegram_id": tg_id, "channel_type": channel})
    if existing and existing.get("last_reference") == reference:
        return

    active_existing = bool(existing and existing.get("is_active") and existing.get("expires_at") and existing["expires_at"] > now)
    expires = (existing["expires_at"] if active_existing else now) + timedelta(days=int(plan["days"]))

    users_col.update_one(
        {"telegram_id": tg_id, "channel_type": channel},
        {"$set": {
            "telegram_id": tg_id,
            "channel_type": channel,
            "purchased_at": now,
            "expires_at": expires,
            "is_active": True,
            "reminder_sent": False,
            "last_reference": reference,
            "paystack_reference": reference,
            "amount_paid": verified.get("amount"),
            "currency": verified.get("currency"),
            "customer_email": (verified.get("customer") or {}).get("email"),
            "referred_by": metadata.get("ref_code"),
            "test_used": True if plan["is_test"] else (existing or {}).get("test_used", False),
        }},
        upsert=True,
    )

    if leads_col is not None:
        leads_col.update_one(
            {"telegram_id": tg_id},
            {"$set": {"converted": True, "converted_at": now, "converted_channel": channel}},
        )

    ref_code = metadata.get("ref_code")
    if ref_code and affiliates_col is not None and referrals_col is not None:
        affiliate = affiliates_col.find_one({"ref_code": ref_code, "is_active": True})
        if affiliate and affiliate.get("telegram_id") != tg_id:
            rate = COMMISSION_RENEWAL if bool(metadata.get("is_renewal")) else COMMISSION_FIRST_SALE
            commission_usd = money(
                (Decimal(str(verified.get("amount", 0))) / 100)
                / Decimal(str(CURRENCY_RATES.get(verified.get("currency"), 1)))
                * rate / 100
            )
            # This commission is kept in USD, independent of payment currency.
            referrals_col.update_one(
                {"affiliate_id": affiliate["_id"], "customer_telegram_id": tg_id},
                {"$setOnInsert": {
                    "affiliate_id": affiliate["_id"],
                    "ref_code": ref_code,
                    "customer_telegram_id": tg_id,
                    "customer_channel": channel,
                    "created_at": now,
                    "is_active": True,
                },
                 "$set": {
                    "last_payment": {
                        "amount": verified.get("amount"),
                        "currency": verified.get("currency"),
                        "commission_paid_usd": float(commission_usd),
                        "commission_rate": float(rate),
                        "paystack_reference": reference,
                        "paid_at": now,
                        "is_renewal": bool(metadata.get("is_renewal")),
                    }
                }},
                upsert=True,
            )
            affiliates_col.update_one(
                {"_id": affiliate["_id"]},
                {"$inc": {
                    "total_earnings": float(commission_usd),
                    "total_referrals": 0 if bool(metadata.get("is_renewal")) else 1,
                }},
            )
            transactions_col.insert_one({
                "affiliate_id": affiliate["_id"],
                "type": "commission",
                "amount": float(commission_usd),
                "description": f"{'Renewal' if bool(metadata.get('is_renewal')) else 'First sale'} commission",
                "reference": reference,
                "created_at": now,
            })

    channel_id = GOLD_CHANNEL_ID if channel == "gold" else FOREX_CHANNEL_ID
    channel_name = "JAY GOLD MASTER VIP" if channel == "gold" else "JAY FX PREMIUM SIGNALS"
    bot = Bot(BOT_TOKEN)

    if active_existing:
        await bot.send_message(
            chat_id=tg_id,
            text=(
                "✅ <b>PAYMENT VERIFIED</b>\n\n"
                f"Plan: {html_escape(plan['name'])}\n"
                f"Expires: {expires.strftime('%B %d, %Y')}\n\n"
                f"Your access to <b>{channel_name}</b> has been extended."
            ),
            parse_mode="HTML",
        )
    else:
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=channel_id,
                member_limit=1,
                expire_date=now + timedelta(minutes=30),
                name=f"paid-{secrets.token_hex(4)}",
            )
            await bot.send_message(
                chat_id=tg_id,
                text=(
                    "✅ <b>PAYMENT VERIFIED</b>\n\n"
                    f"Plan: {html_escape(plan['name'])}\n"
                    f"Expires: {expires.strftime('%B %d, %Y')}\n\n"
                    "Your private one-time access link is ready below."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"Enter {channel_name}", url=invite.invite_link)]
                ]),
            )
        except Exception:
            logger.exception("Could not create/send invite for %s", tg_id)
            await bot.send_message(
                chat_id=tg_id,
                text=f"✅ Payment verified. Please contact @{ADMIN_USERNAME} with reference {reference}.",
            )


# ---------------------------------------------------------------------------
# App and webhook routes
# ---------------------------------------------------------------------------

telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_cmd))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))


@asynccontextmanager
async def lifespan(app: FastAPI):
    require_configuration()
    init_mongodb()
    await telegram_app.initialize()
    await telegram_app.start()

    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is required for webhook mode")
    webhook_url = f"{RENDER_URL}/telegram-webhook"
    kwargs = {"url": webhook_url}
    if TELEGRAM_WEBHOOK_SECRET:
        kwargs["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    await telegram_app.bot.set_webhook(**kwargs)
    logger.info("Telegram webhook configured: %s", webhook_url)

    yield

    await telegram_app.stop()
    if mongo_client:
        mongo_client.close()


app = FastAPI(title="Jay Empire VIP Backend", version="3.0.0", lifespan=lifespan)


@app.get("/")
async def health():
    return {
        "status": "active",
        "service": "Jay Empire VIP + Affiliate",
        "mongodb": "connected" if db is not None else "disconnected",
        "timestamp": utcnow().isoformat(),
    }


@app.get("/health/db")
async def health_db():
    try:
        db.command("ping")
        return {"status": "healthy", "timestamp": utcnow().isoformat()}
    except Exception:
        return JSONResponse({"status": "unhealthy"}, status_code=503)


@app.get("/api/plans")
async def api_plans():
    # Only public pricing/configuration. Never return secrets or DB data.
    return {"plans": PLANS, "currency_rates": CURRENCY_RATES}


@app.post("/telegram-webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    if TELEGRAM_WEBHOOK_SECRET:
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token,
            TELEGRAM_WEBHOOK_SECRET,
        ):
            raise HTTPException(401, "Invalid Telegram webhook secret")

    raw = await request.body()
    try:
        data = __import__("json").loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # Let python-telegram-bot handle normal Telegram updates, but intercept
    # text messages belonging to the affiliate onboarding state machine.
    if "message" in data:
        msg = data["message"]
        if msg.get("text") and not str(msg["text"]).startswith("/"):
            chat_id = msg.get("chat", {}).get("id")
            user = msg.get("from", {})
            if chat_id and await process_text_message(
                int(chat_id),
                user.get("username", ""),
                str(msg["text"]),
            ):
                return {"status": "ok"}

    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


@app.post("/paystack-webhook")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: Optional[str] = Header(None),
):
    if not PAYSTACK_SECRET:
        raise HTTPException(500, "Paystack secret not configured")

    raw = await request.body()
    expected = hmac.new(PAYSTACK_SECRET.encode(), raw, hashlib.sha512).hexdigest()
    if not x_paystack_signature or not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(401, "Invalid Paystack signature")

    try:
        payload = __import__("json").loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event = payload.get("event")
    data = payload.get("data") or {}
    reference = data.get("reference")

    if reference and webhook_events_col is not None:
        try:
            webhook_events_col.insert_one({
                "reference": reference,
                "event": event,
                "created_at": utcnow(),
            })
        except DuplicateKeyError:
            return {"status": "already_processed"}

    if event == "charge.success":
        await fulfill_successful_payment(data)

    elif event in {"transfer.success", "transfer.failed", "transfer.reversed"}:
        transfer_code = data.get("transfer_code")
        status_map = {
            "transfer.success": "success",
            "transfer.failed": "failed",
            "transfer.reversed": "reversed",
        }
        new_status = status_map[event]
        withdrawal = withdrawals_col.find_one({"paystack_transfer_code": transfer_code}) if withdrawals_col else None
        if withdrawal:
            withdrawals_col.update_one(
                {"_id": withdrawal["_id"]},
                {"$set": {"status": new_status, "transfer_event": event, "processed_at": utcnow()}},
            )
            if new_status in {"failed", "reversed"}:
                # The USD ledger was already reserved. Restore it exactly once.
                restored = withdrawals_col.find_one_and_update(
                    {"_id": withdrawal["_id"], "balance_restored": {"$ne": True}},
                    {"$set": {"balance_restored": True}},
                    return_document=ReturnDocument.AFTER,
                )
                if restored:
                    affiliates_col.update_one(
                        {"_id": withdrawal["affiliate_id"]},
                        {"$inc": {"total_withdrawn": -float(withdrawal["amount_usd"])}},
                    )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def verify_admin(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_API_KEY:
        raise HTTPException(503, "ADMIN_API_KEY is not configured")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(401, "Unauthorized")
    return True


@app.get("/admin/dashboard")
async def admin_dashboard(_: bool = Depends(verify_admin)):
    now = utcnow()
    return {
        "subscribers": {
            "total": users_col.count_documents({}),
            "active": users_col.count_documents({"is_active": True}),
            "expired": users_col.count_documents({"is_active": False}),
        },
        "leads": {
            "total": leads_col.count_documents({}),
            "converted": leads_col.count_documents({"converted": True}),
        },
        "affiliates": {
            "total": affiliates_col.count_documents({"is_active": True}),
            "earnings_usd": sum(a.get("total_earnings", 0) for a in affiliates_col.find()),
        },
        "withdrawals": {
            "processing": withdrawals_col.count_documents({"status": {"$in": ["processing", "pending", "queued", "otp"]}}),
            "successful": withdrawals_col.count_documents({"status": "success"}),
            "failed": withdrawals_col.count_documents({"status": {"$in": ["failed", "reversed"]}}),
        },
        "timestamp": now.isoformat(),
    }


@app.get("/admin/withdrawals")
async def admin_withdrawals(_: bool = Depends(verify_admin)):
    items = []
    for w in withdrawals_col.find().sort("created_at", DESCENDING).limit(500):
        items.append({
            "id": str(w["_id"]),
            "telegram_id": w.get("telegram_id"),
            "ref_code": w.get("ref_code"),
            "amount_usd": w.get("amount_usd"),
            "payout_amount": w.get("payout_amount"),
            "payout_currency": w.get("payout_currency"),
            "payout_method": w.get("payout_method"),
            "status": w.get("status"),
            "reference": w.get("reference"),
            "paystack_transfer_code": w.get("paystack_transfer_code"),
            "created_at": w.get("created_at").isoformat() if w.get("created_at") else None,
        })
    return {"count": len(items), "withdrawals": items}


@app.post("/cron/daily-check")
async def cron_daily(_: bool = Depends(verify_admin)):
    now = utcnow()
    # Expiry/reminder job kept intentionally small and safe for Render.
    reminders = 0
    kicked = 0
    target = now + timedelta(days=3)

    for user in users_col.find({
        "is_active": True,
        "reminder_sent": False,
        "expires_at": {"$gt": now, "$lte": target},
    }).limit(500):
        try:
            days = max((user["expires_at"] - now).days, 1)
            await Bot(BOT_TOKEN).send_message(
                chat_id=user["telegram_id"],
                text=f"⏰ Your {user['channel_type'].upper()} VIP access expires in {days} day(s).",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Subscribe 📈", web_app=WebAppInfo(url=MINI_APP_URL))]
                ]),
            )
            users_col.update_one({"_id": user["_id"]}, {"$set": {"reminder_sent": True}})
            reminders += 1
        except Exception:
            logger.exception("Reminder failed")

    for user in users_col.find({"is_active": True, "expires_at": {"$lte": now}}).limit(500):
        channel_id = GOLD_CHANNEL_ID if user["channel_type"] == "gold" else FOREX_CHANNEL_ID
        try:
            await Bot(BOT_TOKEN).ban_chat_member(channel_id, user["telegram_id"])
            await Bot(BOT_TOKEN).unban_chat_member(channel_id, user["telegram_id"])
            users_col.update_one({"_id": user["_id"]}, {"$set": {"is_active": False, "kicked_at": now}})
            kicked += 1
        except Exception:
            logger.exception("Expiry removal failed")

    return {"status": "completed", "reminders": reminders, "kicked": kicked}


@app.get("/admin/affiliates")
async def admin_affiliates(_: bool = Depends(verify_admin)):
    items = []
    for a in affiliates_col.find().limit(1000):
        # Do not expose bank/MoMo numbers in this endpoint.
        items.append({
            "telegram_id": a.get("telegram_id"),
            "username": a.get("username"),
            "full_name": a.get("full_name"),
            "ref_code": a.get("ref_code"),
            "country": a.get("country_name"),
            "payout_method": a.get("payout_method"),
            "payout_currency": a.get("payout_currency"),
            "total_earnings": a.get("total_earnings", 0),
            "total_withdrawn": a.get("total_withdrawn", 0),
            "available_balance": max(0, a.get("total_earnings", 0) - a.get("total_withdrawn", 0)),
            "is_active": a.get("is_active"),
        })
    return {"count": len(items), "affiliates": items}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
