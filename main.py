import os
import asyncio
import httpx
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from pymongo import MongoClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Load Environment Variables from Render Secrets
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID")
GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID")
ADMIN_USERNAME = "jay_empire247"  # Support contact handle

# CORRECTED BOT USERNAME (Matched from screenshot 1)
BOT_USERNAME = "JayEmpire_bot"

# PRICING CONFIGURATION
PRICING_USD = {
    "1_month": {"label": "1 Month ($1)", "usd": 1, "days": 30},
    "6_months": {"label": "6 Months ($45)", "usd": 45, "days": 180},
    "1_year": {"label": "1 Year ($100)", "usd": 100, "days": 365},
    "lifetime": {"label": "Lifetime VIP ($250)", "usd": 250, "days": 36500}
}

# REGIONAL EXCHANGE RATES
EXCHANGE_RATES = {
    "GHS": {"rate": 1.0, "symbol": "GHS ", "multiplier": 100},
    "NGN": {"rate": 1600.0, "symbol": "₦", "multiplier": 100},
    "KES": {"rate": 130.0, "symbol": "KSh ", "multiplier": 100},
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
mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
db = mongo_client["telegram_bot_db"] if mongo_client else None

# ==============================================================================
# AUTOMATED EXPIRATION & 3-DAY REMINDER BACKGROUND TASK
# ==============================================================================
async def auto_subscription_checker():
    """Runs every hour to send 3-day reminders and remove expired subscribers."""
    while True:
        try:
            if db is not None and bot is not None:
                now = datetime.utcnow()
                three_days_from_now = now + timedelta(days=3)
                
                # 1. SEND 3-DAY REMINDER MESSAGES
                impending_expirations = db.subscribers.find({
                    "expires_at": {"$lte": three_days_from_now, "$gt": now},
                    "reminder_sent": {"$ne": True},
                    "is_active": True
                })
                
                for sub in impending_expirations:
                    user_id = sub["telegram_id"]
                    channel_title = "JAY FX PREMIUM SIGNALS" if sub["channel"] == "fx" else "JAY GOLD MASTER VIP"
                    
                    try:
                        renew_btn = InlineKeyboardMarkup([[
                            InlineKeyboardButton("💳 Renew Subscription", callback_data="back_main")
                        ]])
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ <b>Subscription Expiry Reminder</b>\n\n"
                                f"Your access to <b>{channel_title}</b> will expire in <b>3 days</b>.\n"
                                f"Click below to renew your plan and avoid losing signal access!"
                            ),
                            parse_mode="HTML",
                            reply_markup=renew_btn
                        )
                        db.subscribers.update_one({"_id": sub["_id"]}, {"$set": {"reminder_sent": True}})
                    except Exception as e:
                        print(f"Failed to send 3-day reminder to {user_id}: {e}")

                # 2. AUTOMATICALLY REMOVE EXPIRED MEMBERS
                expired_users = db.subscribers.find({
                    "expires_at": {"$lte": now},
                    "is_active": True
                })
                
                for sub in expired_users:
                    user_id = sub["telegram_id"]
                    channel_type = sub["channel"]
                    target_channel = FOREX_CHANNEL_ID if channel_type == "fx" else GOLD_CHANNEL_ID
                    channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
                    
                    try:
                        await bot.ban_chat_member(chat_id=target_channel, user_id=user_id)
                        await bot.unban_chat_member(chat_id=target_channel, user_id=user_id)
                        
                        renew_btn = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 Rejoin VIP Channel", callback_data="back_main")
                        ]])
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🔴 <b>Subscription Expired</b>\n\n"
                                f"Your access period for <b>{channel_title}</b> has officially ended and you have been removed from the channel.\n"
                                f"Thank you for trading with Jay Empire! Click below anytime to rejoin."
                            ),
                            parse_mode="HTML",
                            reply_markup=renew_btn
                        )
                    except Exception as e:
                        print(f"Failed to kick/notify expired user {user_id}: {e}")
                    
                    db.subscribers.update_one({"_id": sub["_id"]}, {"$set": {"is_active": False}})

        except Exception as err:
            print(f"Error in background task loop: {err}")
            
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(auto_subscription_checker())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# TELEGRAM WEBHOOK HANDLER
# ==============================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        
        if text.startswith("/start"):
            keyboard = [
                [InlineKeyboardButton("📈 JAY FX PREMIUM SIGNALS", callback_data="terms_fx")],
                [InlineKeyboardButton("🪙 JAY GOLD MASTER VIP", callback_data="terms_gold")],
                [InlineKeyboardButton("🛠️ Request a Service", callback_data="menu_services")]
            ]
            welcome_text = (
                "<b>Welcome to Jay Empire! 🚀</b>\n\n"
                "Glad you are here. Select an option below to access premium signal channels "
                "or request a custom service."
            )
            await bot.send_message(
                chat_id=chat_id,
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        
        if action in ["terms_fx", "terms_gold"]:
            target_prefix = "fx" if action == "terms_fx" else "gold"
            keyboard = [
                [InlineKeyboardButton("✅ I Agree & Proceed", callback_data=f"curr_{target_prefix}")],
                [InlineKeyboardButton("❌ Decline / Back", callback_data="back_main")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text=TERMS_TEXT,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif action in ["curr_fx", "curr_gold"]:
            prefix = "fx" if action == "curr_fx" else "gold"
            keyboard = [
                [InlineKeyboardButton("🇬🇭 Ghana (GHS / MoMo / Card)", callback_data=f"plan_{prefix}_GHS")],
                [InlineKeyboardButton("🇳🇬 Nigeria (NGN / Transfer / Card)", callback_data=f"plan_{prefix}_NGN")],
                [InlineKeyboardButton("🇰🇪 Kenya (KES / M-Pesa / Card)", callback_data=f"plan_{prefix}_KES")],
                [InlineKeyboardButton("🌍 International (USD / Cards)", callback_data=f"plan_{prefix}_GHS")],
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text="<b>Select your billing region or currency:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif action.startswith("plan_"):
            parts = action.split("_")
            prefix = parts[1]
            curr = parts[2]
            
            channel_name = "JAY FX PREMIUM SIGNALS" if prefix == "fx" else "JAY GOLD MASTER VIP"
            
            keyboard = [
                [InlineKeyboardButton(PRICING_USD["1_month"]["label"], callback_data=f"buy_{prefix}_1_month_{curr}")],
                [InlineKeyboardButton(PRICING_USD["6_months"]["label"], callback_data=f"buy_{prefix}_6_months_{curr}")],
                [InlineKeyboardButton(PRICING_USD["1_year"]["label"], callback_data=f"buy_{prefix}_1_year_{curr}")],
                [InlineKeyboardButton(PRICING_USD["lifetime"]["label"], callback_data=f"buy_{prefix}_lifetime_{curr}")],
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text=f"Select duration for <b>{channel_name}</b> ({curr}):",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

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
            await bot.send_message(
                chat_id=chat_id,
                text="Choose a service to chat directly with support:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif action == "back_main":
            keyboard = [
                [InlineKeyboardButton("📈 JAY FX PREMIUM SIGNALS", callback_data="terms_fx")],
                [InlineKeyboardButton("🪙 JAY GOLD MASTER VIP", callback_data="terms_gold")],
                [InlineKeyboardButton("🛠️ Request a Service", callback_data="menu_services")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text="<b>Jay Empire Main Menu:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif action.startswith("buy_"):
            parts = action.split("_")
            channel_type = parts[1]
            plan_key = f"{parts[2]}_{parts[3]}"
            curr = parts[4]
            
            plan = PRICING_USD[plan_key]
            rate_info = EXCHANGE_RATES.get(curr, EXCHANGE_RATES["GHS"])
            
            channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
            user_email = f"user_{chat_id}@jayempire.com"
            
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
                "callback_url": f"https://t.me/{BOT_USERNAME}",
                "metadata": {
                    "telegram_id": chat_id,
                    "channel_type": channel_type,
                    "days": plan["days"]
                }
            }
            
            async with httpx.AsyncClient() as client:
                res = await client.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers)
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
                    f"Estimated Local Total: <b>{rate_info['symbol']}{total_local:,.2f}</b>\n\n"
                    f"📌 <b>IMPORTANT PAYMENT INSTRUCTION:</b>\n"
                    f"After tapping <b>'💳 Complete Payment'</b> below, click the <b>three dots (⋮)</b> in the top right corner of your screen and select <b>'Open in Browser'</b> (Chrome, Safari, etc.) before completing payment to ensure immediate redirection back to Telegram!\n\n"
                    f"Click below to proceed:"
                )
                
                await bot.send_message(
                    chat_id=chat_id,
                    text=checkout_text,
                    parse_mode="HTML",
                    reply_markup=btn
                )
            else:
                error_msg = res_data.get("message", "Payment initialization failed.")
                support_btn = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact Support (@jay_empire247)", url=f"https://t.me/{ADMIN_USERNAME}")]])
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Payment Error: <i>{error_msg}</i>\nContact support if issues persist.",
                    parse_mode="HTML",
                    reply_markup=support_btn
                )

    return {"status": "ok"}

# ==============================================================================
# PAYSTACK WEBHOOK HANDLER
# ==============================================================================
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    payload = await request.json()
    
    if payload.get("event") == "charge.success":
        meta = payload["data"].get("metadata", {})
        telegram_id = meta.get("telegram_id")
        channel_type = meta.get("channel_type")
        days = meta.get("days", 30)
        
        if telegram_id and channel_type:
            target_channel = FOREX_CHANNEL_ID if channel_type == "fx" else GOLD_CHANNEL_ID
            channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
            
            # Generate single-use invite link expiring in 24 hours
            expire_timestamp = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
            invite_link = await bot.create_chat_invite_link(
                chat_id=target_channel,
                member_limit=1,
                expire_date=expire_timestamp
            )
            
            # Save or update subscriber record in MongoDB Atlas database
            if db is not None:
                db.subscribers.update_one(
                    {"telegram_id": telegram_id, "channel": channel_type},
                    {"$set": {
                        "telegram_id": telegram_id,
                        "channel": channel_type,
                        "days": days,
                        "joined_at": datetime.utcnow(),
                        "expires_at": datetime.utcnow() + timedelta(days=days),
                        "is_active": True,
                        "reminder_sent": False
                    }},
                    upsert=True
                )
                
            # Send single-use link directly to user in their DM with @JayEmpire_bot
            await bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"✅ <b>Payment Confirmed!</b>\n\n"
                    f"Here is your single-use access link for <b>{channel_title}</b>:\n"
                    f"{invite_link.invite_link}\n\n"
                    f"<i>(This link expires in 24 hours and can only be used once)</i>\n"
                    f"For support: @jay_empire247"
                ),
                parse_mode="HTML"
            )
        
    return {"status": "success"}

@app.get("/")
def home():
    return {"status": "Jay Empire Bot Active"}
