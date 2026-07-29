# Deploying for free (no Python, no terminal, $0/month)

Everything below happens in a browser: three free sign-ups (GitHub, Neon, Render — plus Resend for email), no installs.

**The honest tradeoffs of free:**
- The app goes to sleep after 15 minutes with no visitors; the next person to open it waits about a minute for it to wake up.
- Email notifications need Resend (see step 3) instead of plain email, and to send to your teammates' real addresses you'll need to verify your company's domain there (a one-time DNS step — 10 minutes if you can log into wherever your domain's DNS is managed).
- If none of that works for you, `render-paid.yaml` in this folder is a simpler $7/month path (SQLite, plain SMTP, no external database) — ask me and I'll walk you through that version instead.

## 1. Free database — Neon

1. Go to [neon.tech](https://neon.tech) → sign up (no credit card needed).
2. Create a new project (any name, e.g. `content-ops`).
3. On the project dashboard, find the **connection string** — it looks like:
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`
4. Copy it somewhere — you'll paste it into Render in step 4.

## 2. Free email — Resend

1. Go to [resend.com](https://resend.com) → sign up (no credit card needed).
2. Go to **API Keys** → create one → copy it.
3. Go to **Domains** → add your company's domain (e.g. `yourcompany.com`) → Resend gives you a few DNS records (TXT/DKIM) to add.
4. Add those records wherever your domain's DNS lives (Cloudflare, GoDaddy, Namecheap, Google Domains, etc. — whoever you bought the domain through or your IT admin). This step is what lets the app email your actual teammates; it can take a few minutes to a few hours to verify.
5. Once verified, decide the "from" address, e.g. `orders@yourcompany.com` — you'll use this in step 4.

*Don't control your company's DNS? Skip this for now — leave the Resend fields blank in step 4 and the app still works, it just won't send emails until you come back to this later.*

## 3. Put the code on GitHub (no git needed)

1. Unzip `content-ops.zip` on your computer so you have a plain `social-ops` folder.
2. Go to [github.com](https://github.com) → sign up / log in.
3. Click **+** (top right) → **New repository**. Name it `content-ops`, keep it Private → **Create repository**.
4. On the repo's page, click **"uploading an existing file"**.
5. Open the `social-ops` folder, select everything inside it, drag it into the browser window.
6. Scroll down → **Commit changes**.

## 4. Create the free web service on Render

1. Go to [render.com](https://render.com) → sign up (using "Sign up with GitHub" is easiest).
2. Click **New +** → **Web Service** → select your `content-ops` repo.
3. Render should read `render.yaml` and pre-fill the free plan, build command, and start command. If it doesn't, set by hand:
   - **Runtime:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
   - **Plan:** Free
4. Under **Environment**, add:
   - `SECRET_KEY` → any random text (Render can auto-generate)
   - `DATABASE_URL` → paste your Neon connection string from step 1
   - `RESEND_API_KEY` → your key from step 2 (leave blank if you skipped it)
   - `RESEND_FROM` → e.g. `orders@yourcompany.com` (leave blank if you skipped step 2)
   - `BASE_URL` → leave blank for now
5. Click **Create Web Service**. Wait a couple of minutes for the build.

## 5. First login

1. Render gives you a URL like `https://content-ops-xxxx.onrender.com`. Open it (first load may take ~30-60 seconds since it's waking up).
2. Go back to Render's environment variables, set `BASE_URL` to that exact URL, save (it redeploys).
3. Reopen the URL. No accounts exist yet, so you'll land on a one-time **setup page** — enter your name, email, and a password.
4. You're in. Go to **Team** to add producers, and **Settings** to connect OneUp whenever you're ready.

## Updating the app later

Edit files directly on GitHub (click a file → pencil icon → edit → commit) — Render redeploys automatically. No local tools needed for that either.
