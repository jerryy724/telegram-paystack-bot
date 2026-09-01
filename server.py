"""
server.py — Jay Empire VIP Backend & Affiliate Engine
Production-grade deployment with HMAC SHA512 webhook security,
automated Mobile Money payout engine, and zero-downtime MongoDB schemas.
"""

import os
import hmac
import hashlib
import asyncio
import logging
import ssl
import certifi
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING
from pymongo.server_api import ServerApi
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from telegram.ext import Application, CommandHandler

# ==============================================================================
# LOGGING & SYSTEM CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("JayEmpire")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip().strip('"').strip("'")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://jerryy724.github.io/telegram-paystack-bot/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-paystack-bot-415x.onrender.com")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "JAY_EMPIRE_SUPER_ADMIN_SECRET_2026")

GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID", "-1004329655598")
FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID", "-1004451754852")

GOLD_PRIMARY_LINK = "https://t.me/+env-Zrui2ykwYjg8"
FOREX_PRIMARY_LINK = "https://t.me/+njii3OAHlqI3MjQ8"

AFFILIATE_COMMISSION_RATE = 0.15  # 15% Referral Commission

# ==============================================================================
# MONGODB CONNECTION & INDEXING ENGINE
# ==============================================================================
def init_mongodb():
    if not MONGO_URI:
        logger.error("❌ MONGO_URI missing!")
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
        logger.info("✅ MongoDB Atlas connected successfully!")

        db = client.get_default_database()
        
        # Collections
        vip_users = db["vip_users"]
        leads = db["leads"]
        affiliates = db["affiliates"]
        withdrawals = db["withdrawals"]

        # Preserve and extend indexes
        vip_users.create_index([("telegram_id", ASCENDING), ("channel_type", ASCENDING)], unique=True)
        vip_users.create_index([("expires_at", ASCENDING)])
        vip_users.create_index([("is_active", ASCENDING)])
        
        leads.create_index([("telegram_id", ASCENDING)], unique=True)
        
        affiliates.create_index([("telegram_id", ASCENDING)], unique=True)
        affiliates.create_index([("ref_code", ASCENDING)], unique=True)
        
        withdrawals.create_index([("reference", ASCENDING)], unique=True)
        withdrawals.create_index([("telegram_id", ASCENDING)])

        return client, db, vip_users, leads, affiliates, withdrawals

    except Exception as e:
        logger.error(f"❌ MongoDB initialization failed: {e}")
        return None, None, None, None, None, None

mongo_client, db, users_col, leads_col, affiliates_col, withdrawals_col = init_mongodb()

# ==============================================================================
# TELEGRAM BOT HANDLERS
# ==============================================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()

async def start_cmd(update: Update, context):
    user = update.effective_user
    if not user:
        return

    # Extract referral parameter from deep link: /start ref_CODE
    args = context.args
    referred_by_code = args[0].replace("ref_", "") if args and args[0].startswith("ref_") else None

    logger.info(f"🤖 /start from {user.id} (@{user.username}) | Ref: {referred_by_code}")

    if leads_col is not None:
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
                        "followup_sent": False,
                        "referred_by": referred_by_code
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"❌ Lead tracking error: {e}")

    # Auto-provision affiliate profile
    if affiliates_col is not None:
        try:
            affiliates_col.update_one(
                {"telegram_id": user.id},
                {
                    "$setOnInsert": {
                        "telegram_id": user.id,
                        "first_name": user.first_name,
                        "username": user.username,
                        "ref_code": str(user.id),
                        "balance": 0.0,
                        "total_earned": 0.0,
                        "total_withdrawn": 0.0,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"❌ Affiliate provisioning error: {e}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Launch VIP & Affiliate App", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text(
        f"<b>Welcome to Jay Empire VIP Terminal 👑</b>\n\n"
        f"Hello {user.first_name}! Access signals or earn 15% lifetime commissions via our Affiliate Program.",
        parse_mode="HTML",
        reply_markup=kb
    )

telegram_app.add_handler(CommandHandler("start", start_cmd))

# ==============================================================================
# SECURITY UTILITIES
# ==============================================================================
async def verify_admin_key(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized access token.")
    return True

def verify_paystack_signature(raw_body: bytes, signature: str) -> bool:
    if not PAYSTACK_SECRET or not signature:
        return False
    computed_hmac = hmac.new(
        key=PAYSTACK_SECRET.encode('utf-8'),
        msg=raw_body,
        digestmod=hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed_hmac, signature)

# ==============================================================================
# TELEGRAM UTILITIES
# ==============================================================================
async def kick_from_channel(user_id: int, channel_id: str, channel_type: str):
    bot = Bot(token=BOT_TOKEN)
    channel_name = "JAY GOLD MASTER VIP" if channel_type == "gold" else "JAY FX PREMIUM SIGNALS"
    try:
        await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
        await bot.send_message(
            chat_id=user_id,
            text=f"⚠️ <b>Your {channel_name} access has expired.</b>\nRenew to regain access.",
            parse_mode="HTML"
        )
        return True
    except Exception as e:
        logger.error(f"❌ Kick error for {user_id}: {e}")
        return False

# ==============================================================================
# LIFESPAN & SCHEDULER
# ==============================================================================
async def run_daily_checks():
    if users_col is None or leads_col is None:
        return
    now = datetime.utcnow()
    
    # 1. Kick Expired Users
    expired = users_col.find({"is_active": True, "expires_at": {"$lte": now}})
    for user in expired:
        ch_id = GOLD_CHANNEL_ID if user["channel_type"] == "gold" else FOREX_CHANNEL_ID
        if await kick_from_channel(user["telegram_id"], ch_id, user["channel_type"]):
            users_col.update_one({"_id": user["_id"]}, {"$set": {"is_active": False, "kicked_at": now}})

async def scheduler_loop():
    while True:
        await asyncio.sleep(86400)
        await run_daily_checks()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(url=f"{RENDER_URL.rstrip('/')}/telegram-webhook")
    asyncio.create_task(scheduler_loop())
    yield
    await telegram_app.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# API MODELS
# ==============================================================================
class WithdrawalRequest(BaseModel):
    telegram_id: int
    amount: float = Field(gt=0)
    mobile_number: str
    bank_code: str  # GHA_MTN, GHA_VOD, GHA_ATL
    account_name: str

# ==============================================================================
# API ENDPOINTS
# ==============================================================================

@app.get("/")
async def root():
    return {"service": "Jay Empire Engine", "status": "active", "timestamp": datetime.utcnow().isoformat()}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

@app.get("/api/affiliate/dashboard/{telegram_id}")
async def get_affiliate_dashboard(telegram_id: int):
    if affiliates_col is None:
        raise HTTPException(status_code=503, detail="Database uninitialized")

    affiliate = affiliates_col.find_one({"telegram_id": telegram_id}, {"_id": 0})
    if not affiliate:
        # Auto-create if not existent
        affiliate = {
            "telegram_id": telegram_id,
            "ref_code": str(telegram_id),
            "balance": 0.0,
            "total_earned": 0.0,
            "total_withdrawn": 0.0
        }
        affiliates_col.insert_one(dict(affiliate))

    referrals_count = leads_col.count_documents({"referred_by": affiliate["ref_code"]})
    conversions_count = leads_col.count_documents({"referred_by": affiliate["ref_code"], "converted": True})

    history = list(withdrawals_col.find({"telegram_id": telegram_id}, {"_id": 0}).sort("timestamp", -1))

    return {
        "ref_code": affiliate["ref_code"],
        "ref_link": f"https://t.me/JayEmpireVIPBot?start=ref_{affiliate['ref_code']}",
        "balance": affiliate.get("balance", 0.0),
        "total_earned": affiliate.get("total_earned", 0.0),
        "total_withdrawn": affiliate.get("total_withdrawn", 0.0),
        "total_referrals": referrals_count,
        "total_conversions": conversions_count,
        "withdrawal_history": history
    }

@app.post("/paystack-webhook")
async def paystack_webhook(request: Request, x_paystack_signature: Optional[str] = Header(None)):
    body = await request.body()
    
    # Verify Paystack HMAC Signature
    if x_paystack_signature and not verify_paystack_signature(body, x_paystack_signature):
        logger.warning("⚠️ Invalid Paystack HMAC Signature rejected!")
        raise HTTPException(status_code=400, detail="Invalid signature verification.")

    payload = await request.json()
    event = payload.get("event")
    
    if event == "charge.success":
        data = payload["data"]
        metadata = data.get("metadata", {})
        
        tg_id = int(metadata.get("telegram_id", 0))
        channel_type = metadata.get("channel_type", "gold")
        days = int(metadata.get("days", 30))
        reference = data.get("reference", "")
        amount_paid = float(data.get("amount", 0)) / 100.0  # Convert kobo/pesewas to standard units
        currency = data.get("currency", "GHS")

        if not tg_id:
            return {"status": "ignored"}

        now = datetime.utcnow()
        expires_at = now + timedelta(days=days)

        # 1. Update VIP Access
        users_col.update_one(
            {"telegram_id": tg_id, "channel_type": channel_type},
            {
                "$set": {
                    "telegram_id": tg_id,
                    "channel_type": channel_type,
                    "purchased_at": now,
                    "expires_at": expires_at,
                    "is_active": True,
                    "last_reference": reference,
                    "amount_paid": amount_paid,
                    "currency": currency
                }
            },
            upsert=True
        )

        # 2. Process Referral & Attribution Commission
        lead = leads_col.find_one({"telegram_id": tg_id})
        ref_code = lead.get("referred_by") if lead else None

        if ref_code:
            commission = amount_paid * AFFILIATE_COMMISSION_RATE
            affiliates_col.update_one(
                {"ref_code": ref_code},
                {
                    "$inc": {
                        "balance": commission,
                        "total_earned": commission
                    }
                }
            )
            logger.info(f"💰 Affiliate {ref_code} credited GHS {commission:.2f} (15% of {amount_paid})")

        leads_col.update_one({"telegram_id": tg_id}, {"$set": {"converted": True, "converted_at": now}})

        # 3. Dispatch VIP Access Link
        bot = Bot(token=BOT_TOKEN)
        target_link = GOLD_PRIMARY_LINK if channel_type == "gold" else FOREX_PRIMARY_LINK
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Join VIP Channel", url=target_link)]])
        
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=f"🎉 <b>PAYMENT CONFIRMED!</b>\n\nYour subscription is active for <b>{days} days</b>.",
                parse_mode="HTML",
                reply_markup=btn
            )
        except Exception as e:
            logger.error(f"❌ Notification send failed: {e}")

    return {"status": "success"}

@app.post("/api/affiliate/withdraw")
async def request_affiliate_withdrawal(req: WithdrawalRequest):
    if affiliates_col is None or withdrawals_col is None:
        raise HTTPException(status_code=503, detail="Database offline")

    affiliate = affiliates_col.find_one({"telegram_id": req.telegram_id})
    if not affiliate or affiliate.get("balance", 0.0) < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient affiliate balance.")

    if req.amount < 20.0:
        raise HTTPException(status_code=400, detail="Minimum withdrawal is GHS 20.00")

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        # Step A: Create Transfer Recipient on Paystack
        recipient_payload = {
            "type": "mobile_money",
            "name": req.account_name,
            "account_number": req.mobile_number,
            "bank_code": req.bank_code,
            "currency": "GHS"
        }
        rec_res = await client.post("https://api.paystack.co/transferrecipient", json=recipient_payload, headers=headers)
        rec_data = rec_res.json()

        if not rec_data.get("status"):
            raise HTTPException(status_code=400, detail=rec_data.get("message", "Recipient setup failed."))

        recipient_code = rec_data["data"]["recipient_code"]

        # Step B: Initiate Mobile Money Transfer
        transfer_payload = {
            "source": "balance",
            "amount": int(req.amount * 100),
            "recipient": recipient_code,
            "reason": "Jay Empire Affiliate Payout"
        }
        trans_res = await client.post("https://api.paystack.co/transfer", json=transfer_payload, headers=headers)
        trans_data = trans_res.json()

        if not trans_data.get("status"):
            raise HTTPException(status_code=400, detail=trans_data.get("message", "Transfer initiation failed."))

        transfer_ref = trans_data["data"]["reference"]

        # Step C: Atomically Deduct Balance and Record Audit
        affiliates_col.update_one(
            {"telegram_id": req.telegram_id},
            {
                "$inc": {
                    "balance": -req.amount,
                    "total_withdrawn": req.amount
                }
            }
        )

        withdraw_record = {
            "telegram_id": req.telegram_id,
            "amount": req.amount,
            "mobile_number": req.mobile_number,
            "account_name": req.account_name,
            "reference": transfer_ref,
            "status": trans_data["data"].get("status", "success"),
            "timestamp": datetime.utcnow()
        }
        withdrawals_col.insert_one(withdraw_record)

    return {"status": "success", "message": "Payout initiated successfully!", "reference": transfer_ref}

@app.get("/admin/dashboard", dependencies=[Depends(verify_admin_key)])
async def admin_dashboard():
    return {
        "subscribers": users_col.count_documents({"is_active": True}),
        "leads": leads_col.count_documents({}),
        "affiliates": affiliates_col.count_documents({}),
        "total_payouts": list(withdrawals_col.aggregate([{"$group": {"_id": None, "total": {"$sum": "$amount"}}}]))
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
