# Deploying without installing anything locally

No Python, no terminal, no command line. Everything below happens in a browser. Total cost: Render's Starter plan, $7/month (needed for persistent storage — the free tier wipes the database on every restart).

## 1. Put the code on GitHub (no git needed)

1. Unzip `social-ops.zip` on your computer so you have a plain `social-ops` folder.
2. Go to [github.com](https://github.com) and sign up / log in (free).
3. Click the **+** in the top right → **New repository**. Name it `content-ops`, keep it Private, click **Create repository**.
4. On the new repo's page, click **"uploading an existing file"**.
5. Open the `social-ops` folder on your computer, select everything inside it (not the folder itself), and drag it into the browser window.
6. Scroll down, click **Commit changes**.

Your code is now on GitHub — no installs required.

## 2. Create the web service on Render

1. Go to [render.com](https://render.com) and sign up / log in (you can sign up with your GitHub account, which makes the next step easier).
2. Click **New +** → **Web Service**.
3. Connect your GitHub account if asked, then select the `content-ops` repo.
4. Render should detect the included `render.yaml` and pre-fill most settings (runtime: Python, build command, start command, a 1GB persistent disk). If it doesn't, fill in by hand:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
   - **Plan:** Starter ($7/month — required for the persistent disk)
   - Under **Disks**, add one: name `content-ops-data`, mount path `/var/data`, size 1GB.
5. Under **Environment**, add these variables:
   - `SECRET_KEY` → any random string (Render can auto-generate one)
   - `DATABASE_URL` → `sqlite:////var/data/social_ops.db`
   - `BASE_URL` → leave blank for now, you'll fill it in after the first deploy
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` → your email provider's SMTP details (optional — leave blank to skip email notifications for now)
   - `ONEUP_API_KEY` → optional, can also be set later from inside the app's Settings page
6. Click **Create Web Service**. Render will build and deploy — takes a couple of minutes.

## 3. First login

1. Once deployed, Render shows you a URL like `https://content-ops-xxxx.onrender.com`. Open it.
2. Go back into Render's environment variables and set `BASE_URL` to that exact URL (so email notification links work), then let it redeploy.
3. Visit the site — since no accounts exist yet, you'll land on a one-time **setup page**. Enter your name, email, and a password to create your account.
4. You're in. Go to **Team** to add your producers, and **Settings** to connect OneUp whenever you're ready.

## Updating the app later

Any time you want to change something: edit the files on GitHub directly (click a file → pencil icon → edit → commit), and Render will automatically redeploy. No local tools needed for that either.

## If you'd rather not pay $7/month

The free tier on most hosts wipes SQLite on every restart, which breaks this app. Two free-ish alternatives if that's a blocker:
- Ask someone technical to help you point `DATABASE_URL` at a free hosted Postgres database instead (e.g. Supabase's free tier) — the app code would need a small tweak to use `psycopg2` instead of SQLite, since Postgres isn't a drop-in file swap.
- Run it on a spare always-on machine you already have (an old laptop, a Raspberry Pi, an office server) instead of a cloud host.
