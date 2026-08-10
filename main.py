import os
import asyncio
import httpx
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from pymongo import MongoClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID")
GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID")
ADMIN_USERNAME = "jay_empire247"
BOT_USERNAME = "JayEmpire_bot"

# RENDER LIVE DOMAIN FOR AUTOMATIC TELEGRAM WEBHOOK REGISTRATION
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://jay-empire-bot.onrender.com")

# PRICING CONFIGURATION (USD BASE)
PRICING_USD = {
    "1_day_test": {"label": "🧪 1-Day Test (0.10 GHS)", "usd": 0.01, "days": 1, "is_test": True},
    "1_month": {"label": "1 Month ($15)", "usd": 15, "days": 30, "is_test": False},
    "6_months": {"label": "6 Months ($45)", "usd": 45, "days": 180, "is_test": False},
    "1_year": {"label": "1 Year ($100)", "usd": 100, "days": 365, "is_test": False},
    "lifetime": {"label": "Lifetime VIP ($250)", "usd": 250, "days": 36500, "is_test": False}
}

# LIVE EXCHANGE RATE STORAGE (FALLBACK DEFAULTS INCLUDED)
LIVE_EXCHANGE_RATES = {
    "GHS": {"rate": 15.50, "symbol": "GHS ", "multiplier": 100},
    "NGN": {"rate": 1600.0, "symbol": "₦", "multiplier": 100},
    "KES": {"rate": 130.0, "symbol": "KSh ", "multiplier": 100},
    "ZAR": {"rate": 18.50, "symbol": "R ", "multiplier": 100},
    "XOF": {"rate": 600.0, "symbol": "CFA ", "multiplier": 100},
    "RWF": {"rate": 1350.0, "symbol": "FRw ", "multiplier": 100},
    "EGP": {"rate": 48.50, "symbol": "E£ ", "multiplier": 100},
    "USD": {"rate": 1.0, "symbol": "$", "multiplier": 100}
}

TERMS_TEXT = (
    "<b>⚠️ TERMS & CONDITIONS / RISK DISCLAIMER</b>\n\n"
    "1. <b>Risk Warning:</b> Forex and Gold trading involve financial risk. Past results do not guarantee future profits.\n"
    "2. <b>No Refund Policy:</b> All subscriptions are final once channel access is granted.\n"
    "3. <b>Non-Transferable:</b> Invite links are single-use only. Sharing links results in an immediate lifetime ban.\n"
    "4. <b>Expiry Notification:</b> You will be notified 3 days before your subscription ends so you can renew without losing access.\n"
    "5. <b>Support Contact:</b> If you experience payment delays or cannot access your link automatically, contact support directly at <b>@jay_empire247</b>.\n\n"
    "<i>By clicking 'I Agree & Proceed', you acknowledge and accept these terms.</i>"
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

# MONGO CLIENT WITH ROBUST SSL HANDSHAKE FIX
mongo_client = MongoClient(
    MONGO_URI,
    tls=True,
    tlsAllowInvalidCertificates=True,
    serverSelectionTimeoutMS=5000
) if MONGO_URI else None

db = mongo_client["telegram_bot_db"] if mongo_client else None

# ==============================================================================
# 1. LIVE FOREX RATE FETCHING TASK
# ==============================================================================
async def update_live_exchange_rates():
    """Fetches real-time conversion rates for all Paystack African markets hourly."""
    global LIVE_EXCHANGE_RATES
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get("https://open.er-api.com/v6/latest/USD", timeout=10.0)
            if res.status_code == 200:
                rates = res.json().get("rates", {})
                for curr in ["GHS", "NGN", "KES", "ZAR", "XOF", "RWF", "EGP"]:
                    if curr in rates:
                        LIVE_EXCHANGE_RATES[curr]["rate"] = rates[curr]
                print("✅ Successfully updated live African currency exchange rates.")
        except Exception as e:
            print(f"Failed to fetch live forex rates: {e}")

# ==============================================================================
# 2. AUTOMATED SUBSCRIPTION LIFECYCLE TASK
# ==============================================================================
async def auto_subscription_checker():
    """Runs continuously in background for live rates, reminders, and user removal."""
    while True:
        try:
            await update_live_exchange_rates()
            
            if db is not None and bot is not None:
                now = datetime.utcnow()
                three_days_from_now = now + timedelta(days=3)
                
                # 1. Send 3-Day Renewal Warnings
                impending_expirations = db.subscribers.find({
                    "expires_at": {"$lte": three_days_from_now, "$gt": now},
                    "reminder_sent": {"$ne": True},
                    "is_active": True
                })
                
                for sub in impending_expirations:
                    user_id = sub["telegram_id"]
                    channel_title = "JAY FX PREMIUM SIGNALS" if sub["channel"] == "fx" else "JAY GOLD MASTER VIP"
                    try:
                        renew_btn = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Renew Subscription", callback_data="back_main")]])
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"⚠️ <b>Reminder:</b> Your access to <b>{channel_title}</b> expires in 3 days. Send /start to renew your plan!",
                            parse_mode="HTML",
                            reply_markup=renew_btn
                        )
                        db.subscribers.update_one({"_id": sub["_id"]}, {"$set": {"reminder_sent": True}})
                    except Exception as e:
                        print(f"Reminder error for {user_id}: {e}")

                # 2. Auto-Kick Expired Subscribers
                expired_users = db.subscribers.find({"expires_at": {"$lte": now}, "is_active": True})
                for sub in expired_users:
                    user_id = sub["telegram_id"]
                    channel_type = sub["channel"]
                    target_channel = FOREX_CHANNEL_ID if channel_type == "fx" else GOLD_CHANNEL_ID
                    channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
                    
                    try:
                        await bot.ban_chat_member(chat_id=target_channel, user_id=user_id)
                        await bot.unban_chat_member(chat_id=target_channel, user_id=user_id)
                        
                        renew_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Rejoin VIP Channel", callback_data="back_main")]])
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"🔴 Your subscription for <b>{channel_title}</b> has officially ended. Click below anytime to rejoin.",
                            parse_mode="HTML",
                            reply_markup=renew_btn
                        )
                    except Exception as e:
                        print(f"Kick error for {user_id}: {e}")
                    
                    db.subscribers.update_one({"_id": sub["_id"]}, {"$set": {"is_active": False}})

        except Exception as err:
            print(f"Error in background task loop: {err}")
            
        await asyncio.sleep(3600)

# ==============================================================================
# FASTAPI LIFESPAN
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot:
        webhook_url = f"{RENDER_EXTERNAL_URL}/telegram-webhook"
        try:
            await bot.set_webhook(url=webhook_url)
            print(f"✅ Telegram Webhook registered to: {webhook_url}")
        except Exception as e:
            print(f"❌ Failed to set Telegram Webhook: {e}")
            
    task = asyncio.create_task(auto_subscription_checker())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# 3. TELEGRAM WEBHOOK HANDLER
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        
        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"].get("text", "")
            
            if text.startswith("/start"):
                if "success" in text:
                    sub = db.subscribers.find_one({"telegram_id": chat_id, "is_active": True}) if db is not None else None
                    
                    if sub and sub.get("invite_link"):
                        channel_title = "JAY FX PREMIUM SIGNALS" if sub["channel"] == "fx" else "JAY GOLD MASTER VIP"
                        join_btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Join {channel_title}", url=sub["invite_link"])]])
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🎉 <b>CONGRATULATIONS! PAYMENT CONFIRMED!</b>\n\nYou have successfully subscribed to <b>{channel_title}</b>. Click below to enter immediately:",
                            parse_mode="HTML",
                            reply_markup=join_btn
                        )
                        return {"status": "ok"}
                    
                    check_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh My Access Link", callback_data="check_active_sub")]])
                    await bot.send_message(
                        chat_id=chat_id,
                        text="🎉 <b>Welcome back! Payment Received.</b>\n\nYour account is activating. Click below to retrieve your direct invite link:",
                        parse_mode="HTML",
                        reply_markup=check_btn
                    )
                    return {"status": "ok"}

                # Standard /start command main menu
                keyboard = [
                    [InlineKeyboardButton("📈 JAY FX PREMIUM SIGNALS", callback_data="terms:fx")],
                    [InlineKeyboardButton("🪙 JAY GOLD MASTER VIP", callback_data="terms:gold")],
                    [InlineKeyboardButton("🛠️ Request a Service", callback_data="menu_services")]
                ]
                await bot.send_message(
                    chat_id=chat_id,
                    text="<b>Welcome to Jay Empire! 🚀</b>\n\nSelect an option below to proceed:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        elif "callback_query" in data:
            query = data["callback_query"]
            query_id = query["id"]
            chat_id = query["message"]["chat"]["id"]
            action = query["data"]
            
            try:
                await bot.answer_callback_query(callback_query_id=query_id)
            except Exception:
                pass
            
            if action == "check_active_sub":
                sub = db.subscribers.find_one({"telegram_id": chat_id, "is_active": True}) if db is not None else None
                if sub and sub.get("invite_link"):
                    channel_title = "JAY FX PREMIUM SIGNALS" if sub["channel"] == "fx" else "JAY GOLD MASTER VIP"
                    join_btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Join {channel_title}", url=sub["invite_link"])]])
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"🎉 <b>Subscription Active!</b>\n\nWelcome to <b>{channel_title}</b>:",
                        parse_mode="HTML",
                        reply_markup=join_btn
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏳ Payment confirmation in progress with Paystack. Please wait 5 seconds and tap the button again.",
                        parse_mode="HTML"
                    )

            elif action.startswith("terms:"):
                target_prefix = action.split(":")[1]
                keyboard = [
                    [InlineKeyboardButton("✅ I Agree & Proceed", callback_data=f"curr:{target_prefix}")],
                    [InlineKeyboardButton("❌ Decline / Back", callback_data="back_main")]
                ]
                await bot.send_message(chat_id=chat_id, text=TERMS_TEXT, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

            elif action.startswith("curr:"):
                prefix = action.split(":")[1]
                keyboard = [
                    [InlineKeyboardButton("🇬🇭 Ghana (GHS / MoMo / Card)", callback_data=f"plan:{prefix}:GHS")],
                    [InlineKeyboardButton("🇳🇬 Nigeria (NGN / Transfer / Card)", callback_data=f"plan:{prefix}:NGN")],
                    [InlineKeyboardButton("🇰🇪 Kenya (KES / M-Pesa / Card)", callback_data=f"plan:{prefix}:KES")],
                    [InlineKeyboardButton("🇿🇦 South Africa (ZAR / Card / EFT)", callback_data=f"plan:{prefix}:ZAR")],
                    [InlineKeyboardButton("🇨🇮 Côte d'Ivoire (XOF / MoMo / Card)", callback_data=f"plan:{prefix}:XOF")],
                    [InlineKeyboardButton("🇷🇼 Rwanda (RWF / MoMo / Card)", callback_data=f"plan:{prefix}:RWF")],
                    [InlineKeyboardButton("🇪🇬 Egypt (EGP / Card / Wallets)", callback_data=f"plan:{prefix}:EGP")],
                    [InlineKeyboardButton("🌍 Rest of Africa / Global (USD Cards)", callback_data=f"plan:{prefix}:USD")],
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
                ]
                await bot.send_message(chat_id=chat_id, text="<b>Select your billing region or currency:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

            elif action.startswith("plan:"):
                _, prefix, curr = action.split(":")
                channel_name = "JAY FX PREMIUM SIGNALS" if prefix == "fx" else "JAY GOLD MASTER VIP"
                
                keyboard = [
                    [InlineKeyboardButton(PRICING_USD["1_day_test"]["label"], callback_data=f"buy:{prefix}:1_day_test:{curr}")],
                    [InlineKeyboardButton(PRICING_USD["1_month"]["label"], callback_data=f"buy:{prefix}:1_month:{curr}")],
                    [InlineKeyboardButton(PRICING_USD["6_months"]["label"], callback_data=f"buy:{prefix}:6_months:{curr}")],
                    [InlineKeyboardButton(PRICING_USD["1_year"]["label"], callback_data=f"buy:{prefix}:1_year:{curr}")],
                    [InlineKeyboardButton(PRICING_USD["lifetime"]["label"], callback_data=f"buy:{prefix}:lifetime:{curr}")],
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
                ]
                await bot.send_message(chat_id=chat_id, text=f"Select duration for <b>{channel_name}</b> ({curr}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

            elif action == "menu_services":
                profile_url = f"https://t.me/{ADMIN_USERNAME}"
                keyboard = [
                    [InlineKeyboardButton("🎨 Graphic Design", url=profile_url)],
                    [InlineKeyboardButton("🎬 Video Editing", url=profile_url)],
                    [InlineKeyboardButton("📱 Bulk SMS Marketing", url=profile_url)],
                    [InlineKeyboardButton("🧠 Brand Digital Mentorship", url=profile_url)],
                    [InlineKeyboardButton("❓ Others / Custom Inquiries", url=profile_url)],
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
                ]
                await bot.send_message(chat_id=chat_id, text="Choose a service to chat directly with support:", reply_markup=InlineKeyboardMarkup(keyboard))

            elif action == "back_main":
                keyboard = [
                    [InlineKeyboardButton("📈 JAY FX PREMIUM SIGNALS", callback_data="terms:fx")],
                    [InlineKeyboardButton("🪙 JAY GOLD MASTER VIP", callback_data="terms:gold")],
                    [InlineKeyboardButton("🛠️ Request a Service", callback_data="menu_services")]
                ]
                await bot.send_message(chat_id=chat_id, text="<b>Jay Empire Main Menu:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

            elif action.startswith("buy:"):
                _, channel_type, plan_key, curr = action.split(":")
                
                plan = PRICING_USD[plan_key]
                rate_info = LIVE_EXCHANGE_RATES.get(curr, LIVE_EXCHANGE_RATES["GHS"])
                
                channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
                user_email = f"user_{chat_id}@jayempire.com"
                
                if plan.get("is_test"):
                    amount_subunits = 10
                    total_local = 0.10
                else:
                    total_local = plan["usd"] * rate_info["rate"]
                    amount_subunits = int(total_local * rate_info["multiplier"])
                
                headers = {
                    "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "email": user_email,
                    "amount": amount_subunits,
                    "currency": "GHS",
                    "callback_url": f"https://t.me/{BOT_USERNAME}?start=success",
                    "metadata": {
                        "telegram_id": chat_id,
                        "channel_type": channel_type,
                        "days": int(plan["days"])  # ENFORCED INTEGER CONVERSION
                    }
                }
                
                async with httpx.AsyncClient() as client:
                    res = await client.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers, timeout=15.0)
                    res_data = res.json()
                    
                if res_data.get("status"):
                    pay_url = res_data["data"]["authorization_url"]
                    btn = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Complete Payment", url=pay_url)],
                        [InlineKeyboardButton("💬 Manual Verification / Contact Support", url=f"https://t.me/{ADMIN_USERNAME}")]
                    ])
                    
                    checkout_text = (
                        f"<b>Checkout Summary:</b>\n"
                        f"Channel: <b>{channel_title}</b>\n"
                        f"Plan: <b>{plan['label']}</b>\n"
                        f"Local Amount: <b>{rate_info['symbol']}{total_local:,.2f}</b>\n\n"
                        f"📌 <b>IMPORTANT:</b> After tapping 'Complete Payment', click the <b>three dots (⋮)</b> in the top right corner and select <b>'Open in Browser'</b> before paying to ensure clean redirection back to Telegram!"
                    )
                    await bot.send_message(chat_id=chat_id, text=checkout_text, parse_mode="HTML", reply_markup=btn)
                else:
                    error_msg = res_data.get("message", "Payment initialization failed.")
                    await bot.send_message(chat_id=chat_id, text=f"❌ Payment Error: <i>{error_msg}</i>", parse_mode="HTML")

    except Exception as global_err:
        print(f"Error processing Telegram update: {global_err}")

    return {"status": "ok"}

# ==============================================================================
# 4. PAYSTACK WEBHOOK HANDLER
# ==============================================================================
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    try:
        payload = await request.json()
        
        if payload.get("event") == "charge.success":
            meta = payload["data"].get("metadata", {})
            telegram_id = meta.get("telegram_id")
            channel_type = meta.get("channel_type")
            days_raw = meta.get("days", 30)
            
            # CONVERT TO INTEGER TO PREVENT TIMEDELTA CRASHES
            try:
                days = int(days_raw)
            except Exception:
                days = 30
            
            if telegram_id and channel_type:
                target_channel = FOREX_CHANNEL_ID if channel_type == "fx" else GOLD_CHANNEL_ID
                channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
                
                expire_timestamp = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
                created_invite = await bot.create_chat_invite_link(
                    chat_id=target_channel,
                    member_limit=1,
                    expire_date=expire_timestamp
                )
                invite_url = created_invite.invite_link
                
                if db is not None:
                    db.subscribers.update_one(
                        {"telegram_id": int(telegram_id), "channel": channel_type},
                        {"$set": {
                            "telegram_id": int(telegram_id),
                            "channel": channel_type,
                            "days": days,
                            "invite_link": invite_url,
                            "joined_at": datetime.utcnow(),
                            "expires_at": datetime.utcnow() + timedelta(days=days),
                            "is_active": True,
                            "reminder_sent": False
                        }},
                        upsert=True
                    )
                    
                join_btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Join {channel_title}", url=invite_url)]])
                await bot.send_message(
                    chat_id=telegram_id,
                    text=f"🎉 <b>CONGRATULATIONS! PAYMENT CONFIRMED!</b>\n\nClick below to enter <b>{channel_title}</b>:\n<i>(Link expires in 24 hours)</i>",
                    parse_mode="HTML",
                    reply_markup=join_btn
                )
    except Exception as webhook_err:
        print(f"Webhook processing error: {webhook_err}")
        
    return {"status": "success"}

@app.get("/")
def home():
    return {"status": "Jay Empire Bot Active"}
