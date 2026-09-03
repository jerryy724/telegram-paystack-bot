JAY Trading Hub Telegram + Paystack Bot
System model
Paystack is used only to collect customer subscription payments into the merchant account. Affiliate commissions are calculated and recorded internally in MongoDB. Affiliate withdrawals are manual: the administrator sends the affiliate money through Mobile Money or bank, then marks the withdrawal as paid. No Paystack transfer recipient or affiliate transfer is created.
Deployment
Deploy the repository to Render as a Python web service.
Set the required environment variables from render.yaml.
Keep PAYMENT_CURRENCIES=GHS unless the merchant Paystack account is explicitly enabled for another customer-payment currency.
Set the Paystack Dashboard webhook URL to https://<RENDER_EXTERNAL_URL>/paystack-webhook.
Add the Telegram bot to both VIP channels with permission to create invite links and remove members.
Make the GitHub Pages URL in MINI_APP_URL exactly match the published Mini App URL.
Set a strong random ADMIN_API_KEY and TELEGRAM_WEBHOOK_SECRET; never commit them to GitHub.
MIN_WITHDRAWAL_GHS defaults to 50 and WITHDRAWAL_COOLDOWN_DAYS defaults to 7.
Security and accounting
Telegram Mini App initData is verified server-side.
Customer referral attribution is persisted and first-touch locked; the browser cannot choose an affiliate during checkout.
Customer payment amount, currency, plan and entitlement are locked by a server-side payment intent and rechecked against Paystack verification.
Paystack webhook signatures are verified with HMAC-SHA512.
Payment references and affiliate transaction references are protected by unique database indexes for idempotency.
Affiliate balances use integer GHS minor units.
Commission entries are written to an internal ledger before the wallet is credited, preventing duplicate webhook delivery from paying the same commission twice.
Withdrawal requests reserve funds before approval, preventing double-spending.
Admin payout confirmation is atomic: a pending withdrawal can be marked paid only once.
Rejected withdrawals release the reserved balance.
Affiliate payout details are stored for manual payout administration only.
Admin/cron routes require X-Admin-Key.
Telegram webhook requests require TELEGRAM_WEBHOOK_SECRET.
VIP invite links are one-use and expire after 30 minutes.
Sensitive technical errors are kept in server logs rather than shown to customers.
