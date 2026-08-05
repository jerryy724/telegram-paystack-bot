import os
import httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from pymongo import MongoClient
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Load Configuration from Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

FOREX_CHANNEL_ID = os.getenv("FOREX_CHANNEL_ID")
GOLD_CHANNEL_ID = os.getenv("GOLD_CHANNEL_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").replace("@", "")

app = FastAPI()
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
db = mongo_client["telegram_bot_db"] if mongo_client else None

# PRICING CONFIGURATION (Amounts in lowest currency unit, e.g., 1000 = 10.00)
PRICING = {
    "1_month": {"label": "1 Month (10)", "amount": 1000, "days": 30},
    "6_months": {"label": "6 Months (45)", "amount": 4500, "days": 180},
    "lifetime": {"label": "Lifetime VIP (100)", "amount": 10000, "days": 36500}
}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    # 1. Handle Start Command
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        
        if text.startswith("/start"):
            keyboard = [
                [InlineKeyboardButton("📈 JAY FX PREMIUM SIGNALS", callback_data="menu_forex")],
                [InlineKeyboardButton("🪙 JAY GOLD MASTER VIP", callback_data="menu_gold")],
                [InlineKeyboardButton("🛠️ Request a Service", callback_data="menu_services")]
            ]
            welcome_text = (
                "<b>Welcome to Jay Empire! 🚀</b>\n\n"
                "Glad you are here. Use the buttons below to join our premium signal channels "
                "or request a custom professional service."
            )
            await bot.send_message(
                chat_id=chat_id,
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    # 2. Handle Button Clicks
    elif "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        action = query["data"]
        
        # Signal Channels Tier Selection
        if action in ["menu_forex", "menu_gold"]:
            channel_name = "JAY FX PREMIUM SIGNALS" if action == "menu_forex" else "JAY GOLD MASTER VIP"
            prefix = "fx" if action == "menu_forex" else "gold"
            
            keyboard = [
                [InlineKeyboardButton(PRICING["1_month"]["label"], callback_data=f"buy_{prefix}_1_month")],
                [InlineKeyboardButton(PRICING["6_months"]["label"], callback_data=f"buy_{prefix}_6_months")],
                [InlineKeyboardButton(PRICING["lifetime"]["label"], callback_data=f"buy_{prefix}_lifetime")],
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text=f"Select your subscription duration for <b>{channel_name}</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # Custom Services Menu (Redirects to Telegram Profile)
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
                text="Choose a service below to chat directly with Jay Empire support:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # Back to Main Menu
        elif action == "back_main":
            keyboard = [
                [InlineKeyboardButton("📈 JAY FX PREMIUM SIGNALS", callback_data="menu_forex")],
                [InlineKeyboardButton("🪙 JAY GOLD MASTER VIP", callback_data="menu_gold")],
                [InlineKeyboardButton("🛠️ Request a Service", callback_data="menu_services")]
            ]
            await bot.send_message(
                chat_id=chat_id,
                text="<b>Jay Empire Main Menu:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # Generate Paystack Checkout Link
        elif action.startswith("buy_"):
            parts = action.split("_")
            channel_type = parts[1]  # 'fx' or 'gold'
            plan_key = "_".join(parts[2:]) # '1_month', '6_months', or 'lifetime'
            
            plan = PRICING[plan_key]
            channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
            user_email = f"user_{chat_id}@jayempire.com"
            
            headers = {
                "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "email": user_email,
                "amount": plan["amount"],
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
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Complete Payment via Paystack", url=pay_url)]])
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Click below to proceed to checkout for <b>{channel_title} ({plan['label']})</b>:",
                    parse_mode="HTML",
                    reply_markup=btn
                )
            else:
                error_msg = res_data.get("message", "Payment initialization failed.")
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Unable to generate payment link: <i>{error_msg}</i>",
                    parse_mode="HTML"
                )

    return {"status": "ok"}

# Paystack Webhook Handler
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    payload = await request.json()
    
    if payload.get("event") == "charge.success":
        meta = payload["data"]["metadata"]
        telegram_id = meta["telegram_id"]
        channel_type = meta["channel_type"]
        days = meta["days"]
        
        target_channel = FOREX_CHANNEL_ID if channel_type == "fx" else GOLD_CHANNEL_ID
        channel_title = "JAY FX PREMIUM SIGNALS" if channel_type == "fx" else "JAY GOLD MASTER VIP"
        
        # Create single-use invite link expiring in 24 hours
        expire_timestamp = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        invite_link = await bot.create_chat_invite_link(
            chat_id=target_channel,
            member_limit=1,
            expire_date=expire_timestamp
        )
        
        # Store subscriber in MongoDB Atlas
        if db is not None:
            db.subscribers.insert_one({
                "telegram_id": telegram_id,
                "channel": channel_type,
                "days": days,
                "joined_at": datetime.utcnow(),
                "expires_at": datetime.utcnow() + timedelta(days=days)
            })
            
        # Send join link directly to subscriber
        await bot.send_message(
            chat_id=telegram_id,
            text=f"✅ <b>Payment Confirmed!</b>\nHere is your single-use link to join <b>{channel_title}</b>:\n{invite_link.invite_link}\n\n<i>(This link expires in 24 hours and can only be used once)</i>",
            parse_mode="HTML"
        )
        
    return {"status": "success"}

@app.get("/")
def home():
    return {"status": "Jay Empire Bot Operational"}
