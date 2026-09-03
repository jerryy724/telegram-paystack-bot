Jay Empire Telegram + Paystack Bot
Deployment
Deploy this repository to Render as a Python web service.
Set the required environment variables from render.yaml.
Keep PAYMENT_CURRENCIES=GHS unless the Paystack account is explicitly enabled for another transaction currency.
Set the Paystack Dashboard webhook URL to: https://<RENDER_EXTERNAL_URL>/paystack-webhook
Add the Telegram bot to both VIP channels with permission to create invite links and remove members.
Make the GitHub Pages URL in MINI_APP_URL exactly match the published Mini App URL.
Set a strong random ADMIN_API_KEY and TELEGRAM_WEBHOOK_SECRET; never commit them to GitHub.
Important accounting model
Affiliate balances are held in integer GHS minor units to avoid floating-point money errors. Withdrawal requests reserve funds before admin approval. Paystack transfer success/failure/reversal webhooks finalize or release those reservations.
Security model
Telegram Mini App initData is verified server-side.
Referral attribution is taken from the bot's persisted /start referral, not from a browser-supplied referral code.
Payment amount, currency, plan and entitlement are locked by a server-side payment intent and rechecked against Paystack verification.
Paystack webhook signatures are verified with HMAC-SHA512.
Admin/cron routes require X-Admin-Key.
Telegram webhook requests require TELEGRAM_WEBHOOK_SECRET.
VIP invite links are one-use and expire after 30 minutes.
Ghana bank payouts use Paystack ghipss; Ghana MoMo uses mobile_money.
Paystack transfer webhooks are handled by the same /paystack-webhook endpoint because Paystack sends multiple event types to the configured webhook endpoint.
