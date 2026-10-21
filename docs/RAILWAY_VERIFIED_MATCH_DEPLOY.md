# Verified Match checkout — Railway deploy (TEST mode)

## Current state (2026-10-15)
- Railway project: zesty-mindfulness (d2fab6f8-9f09-410e-9755-f3c68c50b930) — new project blocked by free-plan limit, so the service lives here.
  https://railway.com/project/d2fab6f8-9f09-410e-9755-f3c68c50b930
- Service: qortime-verified-match (149a60d2-da27-44ed-8a0b-9bce8410662d), source ConWan30/qortime@main, root /services/verified-match, healthcheck /health
- Volume: qortime-vm-data mounted at /data
- Domain: https://qortime-verified-match-production.up.railway.app
- Vars set: STRIPE_PRICE_ID, SITE_ORIGIN, STORE_PATH=/data/receipts.json, NODE_ENV=production, STRIPE_WEBHOOK_SECRET (from Stripe API)
- Stripe test webhook: we_1UQsBrCepyIJsHZurNt2WXvg -> /v1/webhook (checkout.session.completed, async_payment_succeeded, async_payment_failed, charge.refunded)

## Operator steps (Con)
1. Stripe Dashboard (test mode, acct_1UQVjjCepyIJsHZu) -> Developers -> API keys -> Create restricted key (rk_test_...).
   Permissions: Checkout Sessions write, Prices read, (Events/Webhook read if the service fetches sessions).
2. Railway -> qortime-verified-match -> Variables -> add STRIPE_SECRET_KEY = rk_test_... (service redeploys).
3. Follow-up PR: set apiBase in verify/verified-match.js to https://qortime-verified-match-production.up.railway.app
4. Verify: curl https://qortime-verified-match-production.up.railway.app/health ; run a test checkout with card 4242 4242 4242 4242.

Badge is NOT working until steps 2 and 3 are done.
