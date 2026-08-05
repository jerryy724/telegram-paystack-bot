import os
import httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from pymongo import MongoClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Load Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID")
GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID")
ADMIN_USERNAME = "jay_empire247"  # Support contact handle

app = FastAPI()
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
db = mongo_client["telegram_bot_db"] if mongo_client else None

# Dynamic Base USD Prices
PRICING_USD = {
    "1_month": {"label": "1 Month ($10)", "usd": 10, "days": 30},
    "6_months": {"label": "6 Months ($45)", "usd": 45, "days": 180},
    "lifetime": {"label": "Lifetime VIP ($100)", "usd": 100, "days": 36500}
}

# Regional Conversion Multipliers
EXCHANGE_RATES = {
    "GHS": {"rate": 15.20, "symbol": "GHS ", "multiplier": 100},
    "NGN": {"rate": 1600.0, "symbol": "₦", "multiplier": 100},
    "KES": {"rate": 130.0, "symbol": "KSh ", "multiplier": 100},
    "USD": {"rate": 1.0, "symbol": "$", "multiplier": 100}
}

TERMS_TEXT = (
    "<b>⚠️ TERMS & CONDITIONS / RISK DISCLAIMER</b>\n\n"
    "1. <b>Risk Warning:</b> Forex and Gold trading involve financial risk. Past results do not guarantee future profits.\n"
    "2. <b>No Refund Policy:</b> All subscriptions are final once channel access is granted.\n"
    "3. <b>Non-Transferable:</b> Invite links are single-use only. Sharing links results in an immediate lifetime ban.\n"
    "4. <b>Support Contact:</b> If you experience payment delays or cannot access your link automatically, contact support directly at <b>@jay_empire247</b>.\n\n"
    "<i>By clicking 'I Agree & Proceed', you acknowledge and accept these terms.</i>"
)

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    # 1. Start Command Handler
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

    # 2. Callback Queries
    elif "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        
        # Terms & Conditions Display
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

        # Region / Currency Selection
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

        # Package Duration Options
        elif action.startswith("plan_"):
            parts = action.split("_")
            prefix = parts[1]  # 'fx' or 'gold'
            curr = parts[2]    # 'GHS', 'NGN', 'KES', etc.
            
            channel_name = "JAY FX PREMIUM SIGNALS" if prefix == "fx" else "JAY GOLD MASTER VIP"
            
            keyboard = [
                [InlineKeyboardButton(PRICING_USD["1_month"]["label"], callback_data=f"buy_{prefix}_1_month_{curr}")],
                [InlineKeyboardButton(PRICING_USD["6_months"]["label"], callback_data=f"buy_{prefix}_6_months_{curr}")],
                [InlineKeyboardButton(PRICING_USD["lifetime"]["label"], callback_data=f"buy_{prefix}_lifetime_{curr}")],
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text=f"Select duration for <b>{channel_name}</b> ({curr}):",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # Custom Services List
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

        # Back to Main Menu
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

        # Checkout Link Generation
        elif action.startswith("buy_"):
            parts = action.split("_")
            channel_type = parts[1]        # 'fx' or 'gold'
            plan_key = f"{parts[2]}_{parts[3]}" # '1_month', '6_months', 'lifetime'
            curr = parts[4]                # 'GHS', 'NGN', etc.
            
            plan = PRICING_USD[plan_key]
            rate_info = EXCHANGE_RATES.get(curr, EXCHANGE_RATES["GHS"])
            
            channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
            user_email = f"user_{chat_id}@jayempire.com"
            
            # Subunit amount calculation
            total_local = plan["usd"] * rate_info["rate"]
            amount_subunits = int(total_local * rate_info["multiplier"])
            
            headers = {
                "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "email": user_email,
                "amount": amount_subunits,
                "currency": "GHS", # Base settlement gateway currency
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
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"<b>Checkout Summary:</b>\n"
                        f"Channel: <b>{channel_title}</b>\n"
                        f"Plan: <b>{plan['label']}</b>\n"
                        f"Estimated Local Total: <b>{rate_info['symbol']}{total_local:,.2f}</b>\n\n"
                        "Click below to make payment or reach out for manual verification:"
                    ),
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

# 3. Paystack Webhook Handler
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
            
            # Log in MongoDB Atlas
            if db is not None:
                db.subscribers.insert_one({
                    "telegram_id": telegram_id,
                    "channel": channel_type,
                    "days": days,
                    "joined_at": datetime.utcnow(),
                    "expires_at": datetime.utcnow() + timedelta(days=days)
                })
                
            # Deliver invite link to subscriber
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
