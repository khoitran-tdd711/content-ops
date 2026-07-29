# Content Ops

Internal tool for ordering, producing, approving, and scheduling social content — no third-party project-management tool required.

**No Python or terminal on your computer? See [DEPLOY.md](DEPLOY.md)** for a free, browser-only path (GitHub + Render + Neon + Resend) that needs nothing installed locally. The instructions below assume you *do* have Python and want to run it yourself (locally, or on your own server).

## What it does

1. **Order**: a boss creates an order (platform, content type, quantity, due date) and assigns it to a producer.
2. **Notify**: the producer gets an email the moment the order is created.
3. **Produce**: the producer drops their Google Drive link(s) on the order and submits it. The boss is emailed automatically.
4. **Review**: the boss approves or sends it back with feedback ("to modify"). The producer is emailed either way.
5. **Schedule/Publish**: once approved, the boss opens the order card, picks a date/time, and clicks Schedule. If OneUp (oneupapp.io) is connected, this calls OneUp's API directly and the post goes out automatically. If not connected yet, it falls back to a "send to OneUp" step where the boss uploads manually.

Everything is visible on a Google-Calendar-style board on the home page, color-coded by status.

## Why OneUp for publishing

Instagram, TikTok, and LinkedIn each require their own business app-review process (weeks of lead time) before you can post via their raw APIs. OneUp already has those integrations approved, and exposes a simple REST API — so this tool hands the actual publishing step to OneUp instead of re-doing that approval process from scratch. You still own the whole ordering/production/approval workflow; OneUp is just the pipe to the social networks.

If you'd rather not use OneUp at all, leave its API key blank in Settings — every order will just pause at a "send to OneUp manually" step (or you can wire in a different publishing backend later; see `oneup.py`).

## Setup

```bash
cd social-ops
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # edit values (see below)
python app.py           # runs on http://localhost:5000
```

Open http://localhost:5000 — since no accounts exist yet, you'll land on a one-time **setup page** to create your account in the browser (no need to run anything else). `seed.py` does the same thing from the terminal if you prefer that.

Then go to **Team** to add your producers, and **Settings** to connect OneUp (optional but recommended).

### .env values

- `SECRET_KEY` — any random string.
- `BASE_URL` — where this app is reachable (used to build links in emails).
- `SMTP_*` — your email provider's SMTP details. Leave blank to disable email notifications (the app still works, notifications are just skipped and logged to the console).
- `ONEUP_API_KEY` — optional. Get it from https://www.oneupapp.io/api-access. You can also paste it into the in-app Settings page instead of the `.env` file.

### Connecting OneUp

1. Create a OneUp account and connect your Instagram/LinkedIn/Facebook/TikTok accounts there (this is where the platform-specific OAuth/app-review lives — OneUp has already done this legwork).
2. In this app's **Settings** page, paste your OneUp API key, click "Look up my categories" and "Look up my connected accounts" to see the raw IDs, then paste the right `category_id` and `social_network_id` for each platform into the mapping fields.
3. Save. New orders scheduled from an "Approved" state will now publish through OneUp automatically.

### Media links

Producers paste Google Drive share links (one per line for a multi-image carousel). The app rewrites Drive share links into direct-content URLs before sending them to OneUp. The Drive files need to be shared as "Anyone with the link can view" for OneUp to fetch them. If Drive links prove unreliable for you, swap in OneUp's own Upload Media endpoint (documented in `oneup.py`) to push files into OneUp's storage first.

## Deployment

For team use it needs to run somewhere always-on and reachable by everyone, not just on one person's laptop. See **[DEPLOY.md](DEPLOY.md)** for a no-install, browser-only, **free** path (GitHub + Render's free tier + Neon Postgres + Resend for email — `render.yaml`). There's also a simpler $7/month path with SQLite and plain SMTP if you'd rather skip the free-tier caveats (`render-paid.yaml`). If you'd rather use your own server, run it with a process manager (e.g. `gunicorn app:app`) behind HTTPS, since it handles login passwords and an API key.

## Extending

- `models.py` — data model (Users, Orders, Settings) and status flow.
- `oneup.py` — all OneUp API calls live here; swap or extend for other publishing backends.
- `mailer.py` — notification emails; swap for Slack/Teams webhooks if you'd rather not use email.
- `templates/` — all pages; styling is one plain CSS file (`static/style.css`), no build step.
