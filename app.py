import io
import json
import os
import secrets
import tempfile
import zipfile
from datetime import date, datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    has_request_context,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import captions
import drive
import language_tracker
import mailer
import oneup
from config import Config

MEDIA_CACHE_DIR = os.path.join(tempfile.gettempdir(), "content_ops_media")
os.makedirs(MEDIA_CACHE_DIR, exist_ok=True)
from models import (
    BOARD_STATUSES,
    CONTENT_TYPES,
    LANGUAGE_FLAGS,
    LANGUAGES,
    PLATFORMS,
    STATUS_COLORS,
    STATUS_LABELS,
    STATUS_TEXT_COLORS,
    Order,
    OrderMedia,
    Setting,
    User,
    db,
)

# Boss-uploaded photos (Calendar publish popup's "+ Add photo" control) --
# kept modest since these live as raw bytes in Postgres, not on disk.
ALLOWED_UPLOAD_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # same ceiling as the Claude vision API elsewhere

# Best-guess country for OneUp's "trending sounds" chart, picked from the
# order's own language so the boss doesn't have to pick a country by hand
# every time. Falls back to "US" (the broadest/most active chart) for any
# language not listed here or when the order has no language set.
LANGUAGE_TO_TIKTOK_COUNTRY = {
    "English": "US",
    "Spanish": "ES",
    "Italian": "IT",
    "French": "FR",
    "Dutch": "NL",
    "German": "DE",
    "Polish": "PL",
    "Portuguese": "PT",
    "Romanian": "RO",
    "Greek": "GR",
}

# The curated genre subset the sound picker's dropdown offers (OneUp
# supports ~100+ genre values, way too many for a useful dropdown) --
# whatever the client sends is validated against this list server-side,
# falling back to "ALL" for anything else.
TIKTOK_SOUND_GENRES = {
    "ALL", "POP", "HIP_HOP_RAP", "R_B_SOUL", "ELECTRONIC", "ROCK",
    "LATIN", "COUNTRY", "ALTERNATIVE_INDIE", "K_POP",
}


PARIS_TZ = ZoneInfo("Europe/Paris")


def auto_advance_scheduled_posts():
    """OneUp doesn't call us back to confirm a post actually went out, so
    this is how 'Scheduled' becomes 'Published' on its own: anything
    marked scheduled whose pub time has already passed gets flipped over.
    Runs whenever the Calendar/Board is loaded and on the keep-alive ping
    (every ~5 min), so it's never off by more than a few minutes.

    scheduled_at is stored as a plain wall-clock time with no timezone
    attached (whatever was typed into the date/time picker) — the team
    schedules in Paris time, so 'now' is computed in Europe/Paris and its
    timezone stripped before comparing, rather than comparing against the
    server's own clock (which runs in UTC on Render). This also
    automatically accounts for CET/CEST daylight saving changes."""
    now_paris = datetime.now(PARIS_TZ).replace(tzinfo=None)
    db.session.query(Order).filter(
        Order.status == "scheduled",
        Order.scheduled_at.isnot(None),
        Order.scheduled_at <= now_paris,
    ).update({"status": "published"}, synchronize_session=False)
    db.session.commit()


def wants_json():
    """True when a Board row's JS sent this request via fetch() for an
    instant in-place update, rather than a normal browser form submit that
    expects a full-page redirect."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _ensure_column(table, column, ddl_type):
    """Lightweight, no-Alembic column migration. This app has no migration
    tool, and Flask-SQLAlchemy's create_all() only creates missing tables —
    it never adds a missing column to a table that already exists. So every
    time a new Order column gets added, it needs one of these calls, or the
    already-running production database would crash with 'column does not
    exist' the moment the app tries to read/write it. Safe to call on every
    startup: does nothing once the column is already there."""
    inspector = db.inspect(db.engine)
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text(f'ALTER TABLE "{table}" ADD COLUMN {column} {ddl_type}'))


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    # Render (like most hosts) terminates HTTPS at its edge and forwards plain
    # HTTP to this app, setting X-Forwarded-Proto/Host. Without this, Flask
    # thinks every request is plain http:// and generated links (like the
    # temporary photo URLs OneUp fetches) come out wrong.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _ensure_column("order", "oneup_post_id", "INTEGER")
        _ensure_column("order", "media_order", "TEXT")
        _ensure_column("order", "first_comment", "TEXT")
        _ensure_column("order", "tiktok_sound", "TEXT")
        _ensure_column("order", "instagram_sound", "TEXT")

    register_context(app)
    register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def register_context(app):
    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("user_id")
        if uid:
            g.user = User.query.get(uid)

    @app.context_processor
    def inject_globals():
        return {"current_user": g.get("user"), "STATUS_LABELS": STATUS_LABELS}

    @app.errorhandler(Exception)
    def handle_unexpected_error(e):
        """Belt-and-suspenders: individual routes already handle their own
        known failure modes, but this catches anything unforeseen so the
        boss always sees a plain-English flash instead of a raw 500 page —
        important once we're publishing a lot of posts a day."""
        if isinstance(e, HTTPException):
            return e
        app.logger.exception("Unhandled error")
        if wants_json():
            return {"ok": False, "error": str(e)}, 500
        flash(f"Something went wrong processing that request: {e}", "error")
        return redirect(request.referrer or url_for("calendar_view"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def boss_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("login", next=request.path))
        if not g.user.is_boss:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_routes(app):
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """First-run, browser-only way to create the first boss account.
        Disabled automatically once at least one account exists, so it can't
        be used to take over an already-running instance."""
        if User.query.count() > 0:
            return redirect(url_for("login"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or not email or len(password) < 6:
                flash("Please fill in all fields (password: 6+ characters).", "error")
                return render_template("setup.html")
            user = User(name=name, email=email, role="boss")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            session["user_id"] = user.id
            flash("Account created. Add your producers under Team.", "success")
            return redirect(url_for("calendar_view"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if User.query.count() == 0:
            return redirect(url_for("setup"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                session["user_id"] = user.id
                nxt = request.args.get("next") or url_for("calendar_view")
                return redirect(nxt)
            flash("Invalid email or password.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        """No-login ping target — hitting this every few minutes keeps
        Render's free web service and Neon's free database from going idle,
        so the next real click doesn't have to pay the wake-up cost."""
        db.session.execute(db.text("SELECT 1"))
        auto_advance_scheduled_posts()
        return {"ok": True}

    @app.route("/")
    @login_required
    def calendar_view():
        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        # Handed to the page's JS as a plain object so the custom date-range
        # summary bar can render a label/color-matched pill per status
        # without a server round trip — same colors as the Board and the
        # legend above the grid.
        status_meta = {
            s: {"label": STATUS_LABELS[s], "color": STATUS_COLORS[s], "textColor": STATUS_TEXT_COLORS[s]}
            for s in STATUS_LABELS
        }
        return render_template(
            "calendar.html",
            producers=producers,
            platforms=PLATFORMS,
            board_statuses=BOARD_STATUSES,
            status_meta_json=json.dumps(status_meta),
        )

    @app.route("/api/orders.json")
    @login_required
    def orders_json():
        auto_advance_scheduled_posts()
        orders = Order.query.all()
        events = []
        for o in orders:
            if not o.due_date:
                continue  # no pub date yet -> lives on the Board only, not the calendar
            label_bits = [f"{o.quantity}x {o.content_type}"]
            if o.platform:
                label_bits.append(o.platform.title())
            if o.language:
                label_bits.append(o.language)
            title = " • ".join(label_bits)
            if o.producer:
                title += f" → {o.producer.name}"
            if o.status == "published":
                bucket = "published"
            elif o.status in ("scheduled", "sent_manually"):
                bucket = "scheduled"
            else:
                bucket = "planned"
            events.append(
                {
                    "id": o.id,
                    "title": title,
                    "start": o.due_date.isoformat(),
                    "color": o.status_color,
                    "textColor": o.status_text_color,
                    "url": url_for("order_detail", order_id=o.id),
                    "bucket": bucket,
                    "status": o.status,
                    "platform": o.platform,
                    "caption": o.caption or "",
                    "firstComment": o.first_comment or "",
                    "scheduledAt": o.scheduled_at.strftime("%Y-%m-%dT%H:%M") if o.scheduled_at else None,
                    "projectTitle": o.title or "(untitled)",
                    "accountLabel": get_account_nickname(o.platform, o.language),
                    "tiktokSound": o.tiktok_sound_dict,
                    "contentType": o.content_type or "carousel",
                    "instagramSound": o.instagram_sound_dict,
                    "videoUrl": o.drive_link_list[0] if o.drive_link_list else None,
                }
            )
        return {"events": events}

    @app.route("/media/<token>/<filename>")
    def serve_temp_media(token, filename):
        """Briefly hosts photos we've just unzipped from a Drive .zip so
        OneUp's API can fetch them by URL. No login check on purpose —
        OneUp fetches this anonymously. Token is a random per-publish
        string, so nothing here is guessable."""
        folder = os.path.join(MEDIA_CACHE_DIR, secure_filename(token))
        return send_from_directory(folder, secure_filename(filename))

    # -- Orders ------------------------------------------------------------

    @app.route("/orders/new", methods=["GET", "POST"])
    @boss_required
    def order_new():
        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        if request.method == "POST":
            order = Order(
                platform=request.form["platform"],
                content_type=request.form.get("content_type", "carousel"),
                quantity=int(request.form.get("quantity", 1) or 1),
                title=request.form.get("title", "").strip(),
                caption=request.form.get("caption", "").strip(),
                due_date=datetime.strptime(request.form["due_date"], "%Y-%m-%d").date(),
                date_ordered=date.today(),
                producer_id=int(request.form["producer_id"]),
                created_by_id=g.user.id,
                status="ordered",
            )
            db.session.add(order)
            db.session.commit()
            mailer.notify_new_order(order)
            flash("Order created and producer notified.", "success")
            return redirect(url_for("order_detail", order_id=order.id))
        return render_template(
            "order_new.html", producers=producers, platforms=PLATFORMS, content_types=CONTENT_TYPES
        )

    @app.route("/orders/import", methods=["GET", "POST"])
    @boss_required
    def order_import():
        """Create a new task/order — used by the "+ New task" modal on the
        Board. Pub date is optional; leave it blank and set it later.

        Platform is now a checkbox group (field name "platforms", one value
        per box checked) rather than a single dropdown, so the boss can pick
        any specific combination -- e.g. just Instagram + TikTok -- not only
        one platform or literally every platform. The language field still
        also accepts the special value "__all__" ("All available languages"
        in its dropdown). Either way, this creates one task per combination
        (every platform checked x every language selected), all identical
        otherwise (same title/pub date/etc.), so the boss doesn't have to
        create one task then duplicate it N times and set each
        platform/language by hand."""
        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        if request.method == "POST":
            already_published = request.form.get("already_published") == "yes"
            producer_id = request.form.get("producer_id") or None
            language_input = request.form.get("language") or None
            languages_to_create = LANGUAGES if language_input == "__all__" else [language_input]

            platform_inputs = [p for p in request.form.getlist("platforms") if p]
            if not platform_inputs:
                # Back-compat fallback: nothing checked (or an older caller
                # still posting the single "platform" field) -- same default
                # this route has always used rather than silently creating a
                # platform-less task.
                legacy = request.form.get("platform")
                platform_inputs = [legacy] if legacy else ["instagram"]
            if "__all__" in platform_inputs:
                platforms_to_create = list(PLATFORMS)
            else:
                seen = set()
                platforms_to_create = [
                    p for p in platform_inputs if p in PLATFORMS and not (p in seen or seen.add(p))
                ]
                if not platforms_to_create:
                    platforms_to_create = ["instagram"]

            scheduled_str = request.form.get("scheduled_at")
            scheduled_at = datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M") if scheduled_str else None

            for platform in platforms_to_create:
                for language in languages_to_create:
                    order = Order(
                        platform=platform,
                        language=language,
                        content_type=request.form.get("content_type", "carousel"),
                        quantity=int(request.form.get("quantity", 1) or 1),
                        title=request.form.get("title", "").strip(),
                        caption=request.form.get("caption", "").strip(),
                        first_comment=request.form.get("first_comment", "").strip() or None,
                        due_date=scheduled_at.date() if scheduled_at else None,
                        scheduled_at=scheduled_at,
                        date_ordered=date.today(),
                        producer_id=int(producer_id) if producer_id else None,
                        created_by_id=g.user.id,
                        drive_links=request.form.get("drive_links", "").strip(),
                        status="published" if already_published else "ordered",
                    )
                    db.session.add(order)
            db.session.commit()
            total_created = len(platforms_to_create) * len(languages_to_create)
            if total_created > 1:
                flash(f"Created {total_created} new tasks — one per platform/language combination.", "success")
            else:
                flash("New order created.", "success")
            return redirect(url_for("board_view"))
        return render_template(
            "order_import.html",
            producers=producers,
            platforms=PLATFORMS,
            content_types=CONTENT_TYPES,
            languages=LANGUAGES,
        )

    @app.route("/orders/import-tracker", methods=["GET", "POST"])
    @boss_required
    def order_import_tracker():
        """Bulk-import from the Language Coverage & Publish Tracker sheet
        export (upload the .zip from Google Sheets' Download > Web page, or
        just the Tracker.html file directly)."""
        if request.method == "POST":
            f = request.files.get("tracker_file")
            if not f or not f.filename:
                flash("Choose a file first.", "error")
                return redirect(url_for("order_import_tracker"))

            raw = f.read()
            html_content = None
            filename = f.filename.lower()

            if filename.endswith(".zip") or zipfile.is_zipfile(io.BytesIO(raw)):
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    candidates = [n for n in zf.namelist() if n.lower().endswith(".html")]
                    # Prefer a file literally called Tracker.html; else take the biggest html.
                    tracker_name = next((n for n in candidates if "tracker" in n.lower()), None)
                    if not tracker_name and candidates:
                        tracker_name = max(candidates, key=lambda n: zf.getinfo(n).file_size)
                    if not tracker_name:
                        flash("No .html file found inside that zip.", "error")
                        return redirect(url_for("order_import_tracker"))
                    html_content = zf.read(tracker_name).decode("utf-8", errors="ignore")
            else:
                html_content = raw.decode("utf-8", errors="ignore")

            projects = language_tracker.parse_tracker_html(html_content)
            if not projects:
                flash("Couldn't find a project table in that file.", "error")
                return redirect(url_for("order_import_tracker"))

            # One bulk lookup instead of a query per project/language (was
            # ~1,300 round-trips for a 650-row sheet -> request timeouts).
            titles = [p["project"] for p in projects]
            existing_rows = Order.query.filter(Order.title.in_(titles)).all()
            existing_map = {(o.title, o.language): o for o in existing_rows}

            created, updated, skipped = 0, 0, 0
            new_orders = []
            for proj in projects:
                for lang, info in proj["languages"].items():
                    if not info["ready"]:
                        continue
                    status = "published" if info["published"] else "ready"
                    existing = existing_map.get((proj["project"], lang))
                    if existing:
                        changed = False
                        if existing.status != status:
                            existing.status = status
                            changed = True
                        if not existing.date_ordered and proj["folder_created"]:
                            existing.date_ordered = proj["folder_created"]
                            changed = True
                        if changed:
                            updated += 1
                        else:
                            skipped += 1
                        continue
                    order = Order(
                        platform="instagram",
                        language=lang,
                        content_type="carousel",
                        quantity=1,
                        title=proj["project"],
                        due_date=None,  # no pub date yet — set it later on the Calendar
                        date_ordered=proj["folder_created"],
                        created_by_id=g.user.id,
                        status=status,
                    )
                    new_orders.append(order)
                    created += 1

            db.session.bulk_save_objects(new_orders)
            db.session.commit()
            flash(
                f"Import done: {created} added, {updated} updated, {skipped} already up to date.",
                "success",
            )
            return redirect(url_for("calendar_view"))

        return render_template("order_import_tracker.html")

    @app.route("/board")
    @login_required
    def board_view():
        """Flat tracker of every order/carousel — the source of truth."""
        auto_advance_scheduled_posts()
        q = request.args.get("q", "").strip()
        language = request.args.get("language", "").strip()
        platform = request.args.get("platform", "").strip()
        status = request.args.get("status", "").strip()
        sort = request.args.get("sort", "desc").strip()
        from_date_str = request.args.get("from", "").strip()
        to_date_str = request.args.get("to", "").strip()

        query = Order.query
        if q:
            query = query.filter(Order.title.ilike(f"%{q}%"))
        if language:
            query = query.filter(Order.language == language)
        if platform:
            query = query.filter(Order.platform == platform)
        if status:
            query = query.filter(Order.status == status)
        # "From"/"To" filter by pub date (due_date), same field the
        # Calendar places things by — a custom window like "10/7 - 5/8"
        # answers "what's happening (at every stage) in this stretch,"
        # not "what was created in this stretch." Tasks with no pub date
        # yet (most freshly-"ordered" ones) simply don't have a position
        # in any date window, so they're excluded once either bound is set
        # — same as they're excluded from the Calendar today.
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
                query = query.filter(Order.due_date >= from_date)
            except ValueError:
                from_date_str = ""
        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, "%Y-%m-%d").date()
                query = query.filter(Order.due_date <= to_date)
            except ValueError:
                to_date_str = ""

        date_order = Order.date_ordered.asc() if sort == "asc" else Order.date_ordered.desc()
        orders = query.order_by(Order.date_ordered.is_(None), date_order, Order.title).all()

        # Tallied over this same filtered set, so switching any filter
        # (status/platform/language/search/date range) updates the counts
        # right along with the rows below them.
        status_counts = {}
        for o in orders:
            status_counts[o.status] = status_counts.get(o.status, 0) + 1

        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        return render_template(
            "board.html",
            orders=orders,
            platforms=PLATFORMS,
            languages=LANGUAGES,
            board_statuses=BOARD_STATUSES,
            producers=producers,
            content_types=CONTENT_TYPES,
            status_counts=status_counts,
            status_colors=STATUS_COLORS,
            status_text_colors=STATUS_TEXT_COLORS,
            filters={
                "q": q,
                "language": language,
                "platform": platform,
                "status": status,
                "sort": sort,
                "from": from_date_str,
                "to": to_date_str,
            },
        )

    @app.route("/orders/find-drive-links")
    @boss_required
    def orders_find_drive_links():
        """Board's 'Find Drive links' button — scans tasks with no Drive
        link yet, searches Drive by title, and returns suggested matches
        for the boss to accept one by one (never saved automatically)."""
        sa_json = Setting.get("google_service_account_json")
        if not sa_json:
            return {"error": "Set up the Google service account in Settings first."}, 400
        try:
            service = drive.get_service(sa_json)
        except drive.DriveError as e:
            return {"error": str(e)}, 400

        root_folder_id = drive.resolve_folder_id(Setting.get("drive_projects_root"))

        missing = (
            Order.query.filter(db.or_(Order.drive_links.is_(None), Order.drive_links == ""))
            .filter(Order.title.isnot(None), Order.title != "")
            .order_by(Order.date_ordered.is_(None), Order.date_ordered.desc())
            .limit(50)
            .all()
        )

        candidates = []
        for o in missing:
            try:
                match = drive.find_by_name(service, o.title, root_folder_id)
            except drive.DriveError:
                match = None
            if not match:
                continue
            is_folder = match.get("mimeType") == drive.FOLDER_MIME_TYPE
            link = (
                f"https://drive.google.com/drive/folders/{match['id']}"
                if is_folder
                else f"https://drive.google.com/file/d/{match['id']}/view"
            )
            candidates.append(
                {
                    "order_id": o.id,
                    "title": o.title,
                    "match_name": match["name"],
                    "match_link": link,
                    "match_type": "folder" if is_folder else "zip",
                }
            )

        return {
            "candidates": candidates,
            "scanned": len(missing),
            "note": "Only the 50 most recent tasks without a Drive link are scanned per click — click again for more."
            if len(missing) == 50
            else None,
        }

    @app.route("/orders/auto-fill-drive-links", methods=["POST"])
    @boss_required
    def orders_auto_fill_drive_links():
        """Fills in Drive links automatically using the boss's fixed folder
        convention: one 'mother' Drive folder contains one sub-folder per
        project, named exactly like the task's title. Somewhere inside that
        project folder is a .zip per language: '<Title>.zip' for the base
        language and '<Title>-<code>.zip' for the rest (see
        drive.LANGUAGE_ZIP_SUFFIX). Scoped to exactly the tasks selected on
        the Board (order_ids, from the bulk bar), or to one status at a
        time, or every status if left blank/"all" — an explicit order_ids
        selection always wins over the status filter.

        Overwrite behavior differs by scope on purpose: a broad status-based
        scan never touches a task that already has a Drive link, since
        clobbering something a producer already set correctly across the
        whole board would be worse than leaving a few blanks. But an
        explicit order_ids selection *does* overwrite — picking specific
        rows and hitting "scan selected" is a clear signal the boss wants
        those exact rows re-matched (e.g. after changing a task's language,
        so the wrong-language zip it was first filled with gets replaced)."""
        sa_json = Setting.get("google_service_account_json")
        if not sa_json:
            return {"error": "Set up the Google service account in Settings first."}, 400

        order_ids_raw = request.form.getlist("order_ids")
        selected_ids = []
        if order_ids_raw:
            try:
                selected_ids = [int(x) for x in order_ids_raw]
            except ValueError:
                return {"error": "Invalid task selection."}, 400

        status_filter = (request.form.get("status") or "").strip()
        if status_filter and status_filter not in BOARD_STATUSES:
            return {"error": f"'{status_filter}' isn't a valid status to scan."}, 400

        mother_raw = request.form.get("mother_folder") or Setting.get("drive_projects_root")
        mother_folder_id = drive.resolve_folder_id(mother_raw)
        if not mother_folder_id:
            return {
                "error": "No mother folder set. Paste its Drive link into Settings → "
                "'Project folders root' first."
            }, 400

        try:
            service = drive.get_service(sa_json)
            project_folders = drive.list_child_folders(service, mother_folder_id)
        except drive.DriveError as e:
            return {"error": str(e)}, 400

        project_map = {}
        for f in project_folders:
            project_map.setdefault(drive.normalize_name(f["name"]), f)

        order_filters = [Order.title.isnot(None), Order.title != ""]
        if selected_ids:
            # An explicit selection may re-match tasks that already have a
            # (possibly wrong-language) link — see the overwrite note above.
            order_filters.append(Order.id.in_(selected_ids))
        else:
            order_filters.append(db.or_(Order.drive_links.is_(None), Order.drive_links == ""))
            if status_filter:
                order_filters.append(Order.status == status_filter)
        orders = Order.query.filter(*order_filters).all()

        zip_cache = {}  # project folder id -> list of zip file dicts found inside it
        filled, skipped = [], []
        for o in orders:
            project = project_map.get(drive.normalize_name(o.title))
            if not project:
                skipped.append({"order_id": o.id, "title": o.title, "reason": "No matching project folder found."})
                continue
            try:
                if project["id"] not in zip_cache:
                    zip_cache[project["id"]] = drive.find_all_zips(service, project["id"])
                match = drive.find_project_zip(zip_cache[project["id"]], o.language)
            except drive.DriveError as e:
                skipped.append({"order_id": o.id, "title": o.title, "reason": str(e)})
                continue
            if not match:
                skipped.append(
                    {
                        "order_id": o.id,
                        "title": o.title,
                        "reason": f"Found project folder '{project['name']}' but no zip matched "
                        f"language '{o.language or '(none)'}'.",
                    }
                )
                continue
            link = f"https://drive.google.com/file/d/{match['id']}/view"
            o.drive_links = link
            # A matched link means the content is actually there and ready
            # to review/schedule — bump it out of the default "ordered"
            # bucket automatically so it doesn't just sit there looking
            # untouched. Only ever moves *out of* "ordered" specifically;
            # anything already further along (in_production, submitted,
            # scheduled, etc.) is left exactly where it is.
            if o.status == "ordered":
                o.status = "ready"
            filled.append(
                {
                    "order_id": o.id,
                    "title": o.title,
                    "language": o.language,
                    "link": link,
                    "matched_name": match["name"],
                }
            )

        db.session.commit()
        return {
            "status_scanned": status_filter or "all",
            "selected_count": len(selected_ids),
            "filled": filled,
            "filled_count": len(filled),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    @app.route("/orders/find-new-drive-projects")
    @boss_required
    def orders_find_new_drive_projects():
        """Scans the mother Drive folder for project folders that don't have
        a matching Board task yet at all — the reverse direction of 'Auto-fill
        Drive links'. Read-only: it only reads Drive and reads existing task
        titles, and never modifies, links, or otherwise touches any task
        that's already on the Board, correctly linked or not."""
        sa_json = Setting.get("google_service_account_json")
        if not sa_json:
            return {"error": "Set up the Google service account in Settings first."}, 400

        mother_folder_id = drive.resolve_folder_id(Setting.get("drive_projects_root"))
        if not mother_folder_id:
            return {
                "error": "No mother folder set. Paste its Drive link into Settings → "
                "'Project folders root' first."
            }, 400

        try:
            service = drive.get_service(sa_json)
            project_folders = drive.list_child_folders(service, mother_folder_id)
        except drive.DriveError as e:
            return {"error": str(e)}, 400

        existing_titles = {
            drive.normalize_name(t)
            for (t,) in db.session.query(Order.title)
            .filter(Order.title.isnot(None), Order.title != "")
            .distinct()
        }

        new_projects = [
            {
                "name": f["name"],
                "folder_id": f["id"],
                "link": f"https://drive.google.com/drive/folders/{f['id']}",
            }
            for f in project_folders
            if drive.normalize_name(f["name"]) not in existing_titles
        ]
        new_projects.sort(key=lambda p: p["name"].lower())

        return {"new_projects": new_projects, "scanned_folders": len(project_folders)}

    @app.route("/orders/quick-add-from-drive", methods=["POST"])
    @boss_required
    def orders_quick_add_from_drive():
        """Creates a bare Board task from a flagged new Drive project folder
        — title only, plus an optional language/platform. No Drive link is
        set yet (the project folder itself isn't the right link — the real
        link is one specific language's zip inside it); run 'Auto-fill
        Drive links' afterward, or duplicate this row per language."""
        title = request.form.get("title", "").strip()
        if not title:
            return {"ok": False, "error": "Missing title."}, 400
        language = request.form.get("language") or None
        platform = request.form.get("platform") or "instagram"
        order = Order(
            platform=platform,
            language=language,
            content_type="carousel",
            quantity=1,
            title=title,
            date_ordered=date.today(),
            created_by_id=g.user.id,
            status="ordered",
        )
        db.session.add(order)
        db.session.commit()
        return {"ok": True, "order_id": order.id}

    @app.route("/orders/<int:order_id>/set-date", methods=["POST"])
    @boss_required
    def order_set_date(order_id):
        """Sets a task's pub date/time — used by the Board's date field AND
        by dragging a card to a new day/time on the Calendar.

        If this task is already actively scheduled in OneUp (status
        "scheduled" with a tracked oneup_post_id), changing the time here
        also re-syncs the real OneUp post: OneUp's API has no "just change
        the time" endpoint for a post that's already scheduled, so the fix
        is to cancel the old one and reschedule a fresh one at the new
        time. Without this, dragging a card only updated our own database
        while the real OneUp post kept firing at its old time.

        Whenever that re-sync can't be confirmed (can't cancel the old
        post, can't reschedule the new one, or this task predates OneUp
        sync tracking so there's no post_id to act on at all), the date
        change is rolled back entirely rather than left half-applied —
        showing a new date here that doesn't match what OneUp will
        actually do would be worse than just refusing the move and saying
        so. The Calendar reflects this by snapping the card back to where
        it started."""
        order = Order.query.get_or_404(order_id)
        was_live_in_oneup = order.status == "scheduled" and order.oneup_post_id
        old_post_id = order.oneup_post_id
        old_scheduled_at = order.scheduled_at
        old_due_date = order.due_date

        scheduled_str = request.form.get("scheduled_at")
        if scheduled_str:
            try:
                order.scheduled_at = datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                if wants_json():
                    return {"ok": False, "error": "That date/time doesn't look right."}, 400
                flash("That date/time doesn't look right.", "error")
                return redirect(request.referrer or url_for("board_view"))
            order.due_date = order.scheduled_at.date()
        else:
            order.scheduled_at = None
            order.due_date = None
        platform = request.form.get("platform")
        if platform:
            order.platform = platform

        error = None
        time_changed = order.scheduled_at != old_scheduled_at
        if was_live_in_oneup and order.scheduled_at and time_changed:
            api_key = get_oneup_api_key()
            try:
                oneup.delete_scheduled_post(api_key, old_post_id)
            except oneup.OneUpError as e:
                # Couldn't even cancel the old post — do NOT also create a
                # new one (that would leave two live posts in OneUp).
                error = f"Couldn't cancel the old OneUp post ({e}) — nothing was moved. Check OneUp directly."
            else:
                ok, message = publish_via_oneup(order)
                if not ok:
                    # The old post is already cancelled at this point, so
                    # regardless of *why* publish_via_oneup came back False
                    # (an actual API error, or an early "not configured"
                    # return that never touches the order at all) — nothing
                    # is currently scheduled in OneUp for this task, and
                    # that has to be reflected here explicitly rather than
                    # assumed from publish_via_oneup's own side effects.
                    order.status = "failed"
                    order.oneup_post_id = None
                    error = f"Cancelled the old OneUp post, but rescheduling it failed: {message}. Nothing is currently scheduled in OneUp for this task — please re-publish it."
        elif order.status == "scheduled" and not order.oneup_post_id and time_changed:
            error = (
                "This was scheduled before OneUp sync tracking existed, so there's no way to "
                "find and move its real OneUp post automatically — nothing was moved. Please "
                "change it directly in OneUp."
            )

        if error:
            order.scheduled_at = old_scheduled_at
            order.due_date = old_due_date

        db.session.commit()
        if wants_json():
            if error:
                return {"ok": False, "error": error}, 409
            return {"ok": True}
        if error:
            flash(error, "error")
        else:
            flash("Pub date updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-status", methods=["POST"])
    @boss_required
    def order_set_status(order_id):
        order = Order.query.get_or_404(order_id)
        new_status = request.form.get("status")
        if new_status in BOARD_STATUSES:
            order.status = new_status
            db.session.commit()
            if wants_json():
                return {
                    "ok": True,
                    "status": order.status,
                    "label": order.status_label,
                    "color": order.status_color,
                    "text_color": order.status_text_color,
                }
            flash("Status updated.", "success")
        else:
            if wants_json():
                return {"ok": False, "error": "Not a valid status."}, 400
            flash("Not a valid status.", "error")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-drive-link", methods=["POST"])
    @boss_required
    def order_set_drive_link(order_id):
        order = Order.query.get_or_404(order_id)
        order.drive_links = request.form.get("drive_links", "").strip()
        # Same "a link showed up, so this is ready to review" rule as the
        # Auto-fill Drive links scan — covers both this row's own inline
        # edit and the "Find Drive links" suggestion-accept flow, since
        # both save through this same endpoint. Only ever moves out of
        # the default "ordered" status, and only when an actual link got
        # set (not when the field's being cleared back to blank).
        if order.drive_links and order.status == "ordered":
            order.status = "ready"
        db.session.commit()
        if wants_json():
            return {
                "ok": True,
                "open_link": order.drive_link_list[0] if order.drive_link_list else None,
                # Always included (not just when it changed) so the status
                # dropdown in the next cell over stays in sync either way —
                # harmless to "update" it to the value it already had.
                "status": order.status,
                "color": order.status_color,
                "text_color": order.status_text_color,
            }
        flash("Drive link updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-producer", methods=["POST"])
    @boss_required
    def order_set_producer(order_id):
        order = Order.query.get_or_404(order_id)
        producer_id = request.form.get("producer_id")
        order.producer_id = int(producer_id) if producer_id else None
        db.session.commit()
        if wants_json():
            return {"ok": True}
        flash("POC updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-platform", methods=["POST"])
    @boss_required
    def order_set_platform(order_id):
        order = Order.query.get_or_404(order_id)
        platform = request.form.get("platform")
        if platform:
            order.platform = platform
        db.session.commit()
        if wants_json():
            return {"ok": True}
        flash("Platform updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-language", methods=["POST"])
    @boss_required
    def order_set_language(order_id):
        order = Order.query.get_or_404(order_id)
        order.language = request.form.get("language") or None
        db.session.commit()
        if wants_json():
            return {"ok": True}
        flash("Language updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-note", methods=["POST"])
    @boss_required
    def order_set_note(order_id):
        order = Order.query.get_or_404(order_id)
        order.feedback_note = request.form.get("feedback_note", "").strip()
        db.session.commit()
        if wants_json():
            return {"ok": True, "note": order.feedback_note}
        flash("Note updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/bulk-delete", methods=["POST"])
    @boss_required
    def orders_bulk_delete():
        ids = [i for i in request.form.getlist("order_ids") if i]
        if ids:
            Order.query.filter(Order.id.in_(ids)).delete(synchronize_session=False)
            db.session.commit()
            flash(f"Deleted {len(ids)} task(s).", "success")
        else:
            flash("No tasks were selected.", "error")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/bulk-status", methods=["POST"])
    @boss_required
    def orders_bulk_status():
        ids = [i for i in request.form.getlist("order_ids") if i]
        new_status = request.form.get("status")
        if ids and new_status in BOARD_STATUSES:
            Order.query.filter(Order.id.in_(ids)).update(
                {"status": new_status}, synchronize_session=False
            )
            db.session.commit()
            flash(f"Updated status for {len(ids)} task(s).", "success")
        else:
            flash("Pick at least one task and a valid status.", "error")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/bulk-set-date", methods=["POST"])
    @boss_required
    def orders_bulk_set_date():
        """Board bulk bar's 'Apply date' — sets the same pub date/time on
        every selected task in one go (e.g. the same carousel translated
        into 6 languages, all going out at once)."""
        ids = [i for i in request.form.getlist("order_ids") if i]
        scheduled_str = request.form.get("scheduled_at")
        if not ids or not scheduled_str:
            flash("Pick at least one task and a date/time.", "error")
            return redirect(request.referrer or url_for("board_view"))
        try:
            scheduled_at = datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("That date/time doesn't look right.", "error")
            return redirect(request.referrer or url_for("board_view"))
        Order.query.filter(Order.id.in_(ids)).update(
            {"scheduled_at": scheduled_at, "due_date": scheduled_at.date()},
            synchronize_session=False,
        )
        db.session.commit()
        flash(f"Set the pub date for {len(ids)} task(s).", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/duplicate", methods=["POST"])
    @boss_required
    def order_duplicate(order_id):
        order = Order.query.get_or_404(order_id)
        copy = Order(
            platform=order.platform,
            language=order.language,
            content_type=order.content_type,
            quantity=order.quantity,
            title=order.title,
            caption=order.caption,
            due_date=order.due_date,
            scheduled_at=order.scheduled_at,
            date_ordered=order.date_ordered,
            producer_id=order.producer_id,
            created_by_id=g.user.id,
            drive_links=order.drive_links,
            status=order.status,
        )
        db.session.add(copy)
        db.session.commit()
        flash("Duplicated — edit the copy (e.g. change platform) as needed.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/delete", methods=["POST"])
    @boss_required
    def order_delete(order_id):
        order = Order.query.get_or_404(order_id)
        db.session.delete(order)
        db.session.commit()
        flash("Task deleted.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>")
    @login_required
    def order_detail(order_id):
        order = Order.query.get_or_404(order_id)
        oneup_configured = bool(get_oneup_api_key())
        return render_template("order_detail.html", order=order, oneup_configured=oneup_configured)

    @app.route("/orders/<int:order_id>/submit", methods=["POST"])
    @login_required
    def order_submit(order_id):
        order = Order.query.get_or_404(order_id)
        if not (g.user.is_boss or g.user.id == order.producer_id):
            abort(403)
        order.drive_links = request.form.get("drive_links", "").strip()
        if request.form.get("caption"):
            order.caption = request.form.get("caption").strip()
        if "first_comment" in request.form:
            order.first_comment = request.form.get("first_comment", "").strip() or None
        order.status = "submitted"
        db.session.commit()
        mailer.notify_submitted(order)
        flash("Submitted for review. Boss has been notified.", "success")
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/approve", methods=["POST"])
    @boss_required
    def order_approve(order_id):
        order = Order.query.get_or_404(order_id)
        order.status = "ready"
        order.feedback_note = ""
        db.session.commit()
        mailer.notify_status_change(order, "Your submission was approved. It's ready to be scheduled.")
        flash("Order approved.", "success")
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/request-changes", methods=["POST"])
    @boss_required
    def order_request_changes(order_id):
        order = Order.query.get_or_404(order_id)
        order.status = "to_modify"
        order.feedback_note = request.form.get("feedback_note", "").strip()
        db.session.commit()
        mailer.notify_status_change(
            order, f"Changes requested: {order.feedback_note}"
        )
        flash("Sent back to producer for changes.", "success")
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/schedule", methods=["POST"])
    @boss_required
    def order_schedule(order_id):
        order = Order.query.get_or_404(order_id)
        try:
            scheduled_at_str = request.form.get("scheduled_at")  # 'YYYY-MM-DDTHH:MM' from <input type=datetime-local>
            order.scheduled_at = datetime.strptime(scheduled_at_str, "%Y-%m-%dT%H:%M")
            if not order.platform and request.form.get("platform"):
                order.platform = request.form.get("platform")
            db.session.commit()

            api_key = get_oneup_api_key()
            if not api_key:
                # Manual fallback: no OneUp key configured.
                order.status = "sent_manually"
                db.session.commit()
                flash(
                    "OneUp isn't connected yet (see Settings). Marked as 'sent to OneUp' — "
                    "open OneUp and upload the media manually.",
                    "warning",
                )
                return redirect(url_for("order_detail", order_id=order.id))

            success, message = publish_via_oneup(order)
            if success:
                mailer.notify_status_change(order, f"Scheduled for {order.scheduled_at} via OneUp.")
            flash(message, "success" if success else "error")
        except Exception as e:  # noqa: BLE001 - a boss publishing many posts a day should never see a raw 500
            db.session.rollback()
            order = Order.query.get_or_404(order_id)
            order.status = "failed"
            order.oneup_response = str(e)
            db.session.commit()
            flash(f"Something went wrong scheduling this post: {e}", "error")
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/publish-now", methods=["POST"])
    @boss_required
    def order_publish_now(order_id):
        """Confirm & Publish from the Calendar popup — takes the (possibly
        edited) date/time and description from that form, saves them, then
        schedules straight through OneUp's API. Responds with JSON when the
        Calendar's JS posts it via fetch, so the page never reloads — that
        used to bounce the boss back to month view and clear the search box
        after every publish, even from Week view."""
        order = Order.query.get_or_404(order_id)

        try:
            scheduled_str = request.form.get("scheduled_at")
            if scheduled_str:
                order.scheduled_at = datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M")
                order.due_date = order.scheduled_at.date()
            elif not order.scheduled_at:
                if not order.due_date:
                    msg = "This task has no pub date set yet — set one on the Board or Calendar first."
                    if wants_json():
                        return {"ok": False, "error": msg}, 400
                    flash(msg, "error")
                    return redirect(url_for("calendar_view"))
                order.scheduled_at = datetime.combine(order.due_date, datetime.min.time().replace(hour=12))

            if "caption" in request.form:
                order.caption = request.form.get("caption", "").strip()
            if "first_comment" in request.form:
                order.first_comment = request.form.get("first_comment", "").strip() or None

            db.session.commit()

            api_key = get_oneup_api_key()
            if not api_key:
                msg = "OneUp isn't connected yet — add your API key in Settings first."
                if wants_json():
                    return {"ok": False, "error": msg}, 400
                flash(msg, "error")
                return redirect(url_for("calendar_view"))

            success, message = publish_via_oneup(order)
            if success:
                mailer.notify_status_change(order, f"Scheduled for {order.scheduled_at} via OneUp.")
            if wants_json():
                return {"ok": success, "error": None if success else message}
            flash(message, "success" if success else "error")
        except Exception as e:  # noqa: BLE001 - a boss publishing many posts a day should never see a raw 500
            db.session.rollback()
            order = Order.query.get_or_404(order_id)
            order.status = "failed"
            order.oneup_response = str(e)
            db.session.commit()
            msg = f"Something went wrong scheduling this post: {e}"
            if wants_json():
                return {"ok": False, "error": msg}, 500
            flash(msg, "error")
        return redirect(url_for("calendar_view"))

    @app.route("/orders/<int:order_id>/media-preview")
    @login_required
    def order_media_preview(order_id):
        """Powers the thumbnail preview in the Calendar's publish popup —
        runs the exact same Drive link + uploaded-photo -> image URLs
        logic used for the real publish (expand_drive_links), just
        without actually sending anything to OneUp. So what you see here
        is what will go out."""
        order = Order.query.get_or_404(order_id)
        if not order.drive_link_list and not order.uploaded_media:
            return {"images": [], "error": None}
        try:
            image_urls = expand_drive_links(order)
        except drive.DriveError as e:
            return {"images": [], "error": str(e)}
        except Exception as e:  # noqa: BLE001 - a preview must never 500
            return {"images": [], "error": f"Couldn't load a preview: {e}"}
        return {"images": image_urls}

    @app.route("/orders/<int:order_id>/set-media-order", methods=["POST"])
    @boss_required
    def order_set_media_order(order_id):
        """Calendar publish popup's drag-to-reorder / click-to-remove photo
        strip auto-saves here on every change. Body is JSON:
        {"filenames": [...], "visible": [...]} — "filenames" is the kept
        photos in the order they should be published (the safe keys
        embedded in each preview thumbnail's URL, its last path segment);
        "visible" is every key that was showing right before this change
        (so removed ones can be told apart from ones that simply don't
        exist yet, e.g. a photo not uploaded until later — see
        _select_and_order's "known" handling). An empty/missing
        "filenames" means "reset," i.e. drop all customization and go back
        to the natural full order. This only ever writes order.media_order;
        expand_drive_links() (used by both the preview and the real OneUp
        publish) is what actually applies it, so saving here immediately
        changes what the next publish will send."""
        order = Order.query.get_or_404(order_id)
        payload = request.get_json(silent=True) or {}
        filenames = payload.get("filenames")
        if not isinstance(filenames, list) or not filenames:
            order.media_order = None
            db.session.commit()
            return {"ok": True}

        cleaned = [str(f) for f in filenames if isinstance(f, (str, int))]
        visible = payload.get("visible")
        visible_cleaned = (
            [str(f) for f in visible if isinstance(f, (str, int))] if isinstance(visible, list) else cleaned
        )

        previous_known = []
        if order.media_order:
            try:
                saved = json.loads(order.media_order)
                if isinstance(saved, dict):
                    previous_known = saved.get("known") or []
                elif isinstance(saved, list):
                    previous_known = saved
            except (ValueError, TypeError):
                previous_known = []

        known = list(dict.fromkeys(previous_known + visible_cleaned + cleaned))
        order.media_order = json.dumps({"kept": cleaned, "known": known})
        db.session.commit()
        return {"ok": True}

    @app.route("/orders/<int:order_id>/upload-media", methods=["POST"])
    @boss_required
    def order_upload_media(order_id):
        """Calendar publish popup's "+ Add photo" control — lets the boss
        add extra photos straight into Content Ops for this one task, on
        top of whatever's already pulled from its Drive link(s). Saved to
        Postgres (see OrderMedia) rather than local disk, since Render's
        free web dyno's filesystem doesn't survive a restart/redeploy —
        this needs to still be there whenever the task actually gets
        published, not just for the current browser session."""
        order = Order.query.get_or_404(order_id)
        files = request.files.getlist("photos")
        if not files or all(not f.filename for f in files):
            return {"ok": False, "error": "No file selected."}, 400

        next_sort = OrderMedia.query.filter_by(order_id=order.id).count()
        added = 0
        for f in files:
            if not f.filename:
                continue
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                return {"ok": False, "error": f"'{f.filename}' isn't a supported photo type (jpg/png/webp/gif)."}, 400
            data = f.read()
            if not data:
                continue
            if len(data) > MAX_UPLOAD_BYTES:
                return {"ok": False, "error": f"'{f.filename}' is too large (max 8MB per photo)."}, 400
            db.session.add(
                OrderMedia(
                    order_id=order.id,
                    filename=secure_filename(f.filename) or "photo.jpg",
                    mime_type=f.mimetype or "image/jpeg",
                    data=data,
                    sort_order=next_sort,
                )
            )
            next_sort += 1
            added += 1
        if not added:
            return {"ok": False, "error": "No file selected."}, 400
        db.session.commit()
        return {"ok": True, "added": added}

    @app.route("/orders/<int:order_id>/uploaded-media/<int:media_id>/<filename>")
    def serve_uploaded_media(order_id, media_id, filename):
        """Serves a boss-uploaded photo's raw bytes so OneUp (and the
        Calendar preview) can fetch it by URL. No login check on purpose —
        OneUp fetches this anonymously, same as the temp-hosted Drive
        photos at /media/<token>/<filename>."""
        media = OrderMedia.query.filter_by(id=media_id, order_id=order_id).first_or_404()
        return Response(media.data, mimetype=media.mime_type or "image/jpeg")

    @app.route("/orders/<int:order_id>/tiktok-trending-sounds")
    @boss_required
    def order_tiktok_trending_sounds(order_id):
        """Calendar publish popup's "Browse trending sounds" control, for
        TikTok tasks only — looks up OneUp's currently-trending sound chart
        so the boss can pick one right here, instead of hunting a sound_id
        down in OneUp or TikTok directly. country_code is guessed from the
        task's language (see LANGUAGE_TO_TIKTOK_COUNTRY). OneUp has no
        keyword search for TikTok sounds, so an optional ?genre= override
        (validated against the curated list the picker's dropdown offers)
        is the closest thing to narrowing results down before the boss
        filters further client-side."""
        order = Order.query.get_or_404(order_id)
        api_key = get_oneup_api_key()
        if not api_key:
            return {"ok": False, "error": "OneUp isn't connected yet (see Settings)."}, 400
        social_account_id = _resolve_tiktok_social_account_id(order)
        if not social_account_id:
            return {"ok": False, "error": "No OneUp TikTok account is mapped yet — set it up in Settings first."}, 400
        country_code = LANGUAGE_TO_TIKTOK_COUNTRY.get(order.language, "US")
        genre = request.args.get("genre") or "ALL"
        if genre not in TIKTOK_SOUND_GENRES:
            genre = "ALL"
        try:
            sounds = oneup.get_tiktok_trending_sound(api_key, social_account_id, country_code=country_code, genre=genre)
        except oneup.OneUpError as e:
            return {"ok": False, "error": f"OneUp couldn't fetch trending sounds: {e}"}, 502
        return {"ok": True, "country_code": country_code, "genre": genre, "sounds": sounds}

    @app.route("/orders/<int:order_id>/set-tiktok-sound", methods=["POST"])
    @boss_required
    def order_set_tiktok_sound(order_id):
        """Saves (or clears) the trending sound the boss picked for this
        TikTok task. Body is JSON: {"sound": {...}} using the same field
        names get_tiktok_trending_sound() returns, or {} / no "sound" key to
        remove a previously-picked sound. Only re-published by the next
        actual publish — picking a sound here doesn't touch OneUp until
        then."""
        order = Order.query.get_or_404(order_id)
        payload = request.get_json(silent=True) or {}
        sound = payload.get("sound")
        if sound and isinstance(sound, dict):
            cleaned = {
                "music_title": str(sound.get("music_title") or "")[:200],
                "music_author": str(sound.get("music_author") or "")[:200],
                "music_sound_id": str(sound.get("music_sound_id") or "")[:200],
                "music_url": str(sound.get("music_url") or "")[:500],
                "music_thumbnail": str(sound.get("music_thumbnail") or "")[:500],
            }
            order.tiktok_sound = json.dumps(cleaned)
        else:
            order.tiktok_sound = None
        db.session.commit()
        return {"ok": True, "sound": order.tiktok_sound_dict}

    @app.route("/orders/<int:order_id>/instagram-trending-sounds")
    @boss_required
    def order_instagram_trending_sounds(order_id):
        """Calendar publish popup's Instagram sound search, for video/Reel
        Instagram tasks only. Unlike TikTok's endpoint, this one supports a
        real keyword search (?q=) -- leave it blank for general trending
        sounds instead."""
        order = Order.query.get_or_404(order_id)
        api_key = get_oneup_api_key()
        if not api_key:
            return {"ok": False, "error": "OneUp isn't connected yet (see Settings)."}, 400
        social_account_id = _resolve_instagram_social_account_id(order)
        if not social_account_id:
            return {"ok": False, "error": "No OneUp Instagram account is mapped yet — set it up in Settings first."}, 400
        search_query = (request.args.get("q") or "").strip() or None
        try:
            sounds = oneup.get_instagram_trending_sound(api_key, social_account_id, search_query=search_query)
        except oneup.OneUpError as e:
            return {"ok": False, "error": f"OneUp couldn't fetch sounds: {e}"}, 502
        return {"ok": True, "sounds": sounds}

    @app.route("/orders/<int:order_id>/set-instagram-sound", methods=["POST"])
    @boss_required
    def order_set_instagram_sound(order_id):
        """Saves (or clears) the trending sound picked for this Instagram
        video/Reel task. Body is JSON: {"sound": {...}} using the field
        names get_instagram_trending_sound() returns, or {} / no "sound"
        key to remove a previously-picked sound."""
        order = Order.query.get_or_404(order_id)
        payload = request.get_json(silent=True) or {}
        sound = payload.get("sound")
        if sound and isinstance(sound, dict):
            cleaned = {
                "music_title": str(sound.get("music_title") or "")[:200],
                "music_sound_id": str(sound.get("music_sound_id") or "")[:200],
                "music_url": str(sound.get("music_url") or "")[:500],
            }
            order.instagram_sound = json.dumps(cleaned)
        else:
            order.instagram_sound = None
        db.session.commit()
        return {"ok": True, "sound": order.instagram_sound_dict}

    @app.route("/orders/<int:order_id>/generate-caption", methods=["POST"])
    @boss_required
    def order_generate_caption(order_id):
        """Calendar's 'Generate caption' button — writes a draft caption
        from this task's actual photos via Claude, following Akka's
        caption rules (hook first, facts-based, translated CTA with the
        right @handle for the task's language, up to 5 SEO hashtags).
        Never saves anything; the boss reviews/edits before publishing."""
        order = Order.query.get_or_404(order_id)
        api_key = Setting.get("anthropic_api_key")
        if not api_key:
            return {"ok": False, "error": "Add your Anthropic API key in Settings first."}, 400
        if not order.drive_link_list:
            return {"ok": False, "error": "No Drive link set yet for this task."}, 400

        # Deliberately does NOT fall back to the English handle for a task
        # that has a language set — mentioning @akka.app in a Greek caption
        # (say) just because Greek isn't configured yet would be worse than
        # a clear error asking to add it.
        if order.language:
            handle = Setting.get(f"caption_handle_{order.language}")
        else:
            handle = Setting.get("caption_handle_English")
        if not handle:
            who = order.language or "English (main)"
            return {
                "ok": False,
                "error": f"No account handle set for {who} yet — add it in Settings under "
                "'Account handle per language'.",
            }, 400

        try:
            images = _collect_order_images(order)
            caption = captions.generate_caption(api_key, images, order.language, handle)
        except drive.DriveError as e:
            return {"ok": False, "error": str(e)}, 400
        except captions.CaptionError as e:
            return {"ok": False, "error": str(e)}, 400
        except Exception as e:  # noqa: BLE001 - must never 500
            return {"ok": False, "error": f"Couldn't generate a caption: {e}"}, 500
        return {"ok": True, "caption": caption}

    @app.route("/orders/<int:order_id>/mark-published", methods=["POST"])
    @boss_required
    def order_mark_published(order_id):
        """Used to close out the loop after a manual OneUp upload."""
        order = Order.query.get_or_404(order_id)
        order.status = "published"
        db.session.commit()
        flash("Marked as published.", "success")
        return redirect(url_for("order_detail", order_id=order.id))

    # -- Users (boss/admin) -------------------------------------------------

    @app.route("/users", methods=["GET", "POST"])
    @boss_required
    def users():
        if request.method == "POST":
            user = User(
                name=request.form["name"].strip(),
                email=request.form["email"].strip().lower(),
                role=request.form["role"],
            )
            user.set_password(request.form["password"])
            db.session.add(user)
            db.session.commit()
            flash(f"Created {user.role} account for {user.name}.", "success")
            return redirect(url_for("users"))
        all_users = User.query.order_by(User.role, User.name).all()
        return render_template("users.html", users=all_users)

    # -- Settings (OneUp connection) -----------------------------------------

    @app.route("/settings", methods=["GET"])
    @boss_required
    def settings():
        api_key = get_oneup_api_key()
        mapping = {p: Setting.get(f"oneup_social_id_{p}") for p in PLATFORMS}
        category_mapping = {p: Setting.get(f"oneup_category_id_{p}") for p in PLATFORMS}
        language_mapping = {
            p: {l: Setting.get(f"oneup_social_id_{p}_{l}") for l in LANGUAGES} for p in PLATFORMS
        }
        nickname_mapping = {p: Setting.get(f"oneup_social_nickname_{p}") for p in PLATFORMS}
        language_nickname_mapping = {
            p: {l: Setting.get(f"oneup_social_nickname_{p}_{l}") for l in LANGUAGES} for p in PLATFORMS
        }
        caption_handles = {l: Setting.get(f"caption_handle_{l}") for l in LANGUAGES}
        return render_template(
            "settings.html",
            api_key=api_key,
            mapping=mapping,
            category_mapping=category_mapping,
            platforms=PLATFORMS,
            languages=LANGUAGES,
            language_flags=LANGUAGE_FLAGS,
            language_mapping=language_mapping,
            nickname_mapping=nickname_mapping,
            language_nickname_mapping=language_nickname_mapping,
            google_service_account_json=Setting.get("google_service_account_json") or "",
            drive_projects_root=Setting.get("drive_projects_root") or "",
            anthropic_api_key=Setting.get("anthropic_api_key") or "",
            caption_handles=caption_handles,
        )

    @app.route("/settings/save", methods=["POST"])
    @boss_required
    def settings_save():
        Setting.set("oneup_api_key", request.form.get("api_key", "").strip())
        for p in PLATFORMS:
            Setting.set(f"oneup_social_id_{p}", request.form.get(f"social_id_{p}", "").strip())
            Setting.set(f"oneup_category_id_{p}", request.form.get(f"category_id_{p}", "").strip())
            Setting.set(
                f"oneup_social_nickname_{p}", request.form.get(f"social_nickname_{p}", "").strip()
            )
            for l in LANGUAGES:
                Setting.set(
                    f"oneup_social_id_{p}_{l}",
                    request.form.get(f"social_id_{p}_{l}", "").strip(),
                )
                Setting.set(
                    f"oneup_social_nickname_{p}_{l}",
                    request.form.get(f"social_nickname_{p}_{l}", "").strip(),
                )
        Setting.set(
            "google_service_account_json",
            request.form.get("google_service_account_json", "").strip(),
        )
        Setting.set("drive_projects_root", request.form.get("drive_projects_root", "").strip())
        Setting.set("anthropic_api_key", request.form.get("anthropic_api_key", "").strip())
        for l in LANGUAGES:
            Setting.set(f"caption_handle_{l}", request.form.get(f"caption_handle_{l}", "").strip())
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

    @app.route("/settings/drive/test")
    @boss_required
    def settings_drive_test():
        sa_json = Setting.get("google_service_account_json")
        if not sa_json:
            return {"error": "No Google service account key saved yet."}, 400
        try:
            service = drive.get_service(sa_json)
            about = service.about().get(fields="user").execute()
            email = about.get("user", {}).get("emailAddress", "unknown")
            return {
                "ok": True,
                "service_account_email": email,
                "note": "Share your Drive folders/zips with this exact email address.",
            }
        except drive.DriveError as e:
            return {"error": str(e)}, 400
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}, 502

    @app.route("/settings/oneup/accounts")
    @boss_required
    def settings_oneup_accounts():
        api_key = get_oneup_api_key()
        if not api_key:
            return {"error": "No OneUp API key set yet."}, 400
        try:
            return oneup.list_social_accounts(api_key)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}, 502

    @app.route("/settings/oneup/categories")
    @boss_required
    def settings_oneup_categories():
        api_key = get_oneup_api_key()
        if not api_key:
            return {"error": "No OneUp API key set yet."}, 400
        try:
            return oneup.list_categories(api_key)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}, 502


def get_oneup_api_key():
    return Setting.get("oneup_api_key") or Config.ONEUP_API_KEY_DEFAULT


def _resolve_tiktok_social_account_id(order):
    """Same per-language-then-per-platform account lookup publish_via_oneup()
    uses, but just for TikTok, and returning a single social_network_id —
    that's all gettiktoktrendingsound() needs (one specific connected TikTok
    account to read its trending chart for, not the whole list a post might
    actually go out to). Returns None if nothing's mapped yet."""
    social_id_raw = None
    if order.language:
        social_id_raw = Setting.get(f"oneup_social_id_tiktok_{order.language}")
    if not social_id_raw:
        social_id_raw = Setting.get("oneup_social_id_tiktok")
    if not social_id_raw:
        return None
    try:
        ids = json.loads(social_id_raw) if social_id_raw.startswith("[") else [social_id_raw]
    except (ValueError, TypeError):
        return None
    return ids[0] if ids else None


def _resolve_instagram_social_account_id(order):
    """Same per-language-then-per-platform account lookup as
    _resolve_tiktok_social_account_id, but for Instagram -- used by the
    Instagram trending-sound browse route (only needs one account id, not
    the full list a post might go out to)."""
    social_id_raw = None
    if order.language:
        social_id_raw = Setting.get(f"oneup_social_id_instagram_{order.language}")
    if not social_id_raw:
        social_id_raw = Setting.get("oneup_social_id_instagram")
    if not social_id_raw:
        return None
    try:
        ids = json.loads(social_id_raw) if social_id_raw.startswith("[") else [social_id_raw]
    except (ValueError, TypeError):
        return None
    return ids[0] if ids else None


def get_account_nickname(platform, language):
    """Friendly label for the account a task will publish to (e.g. 'Akka
    App' instead of a raw account ID) — shown on the Calendar. Falls back
    to Platform/Language if no nickname has been set yet in Settings."""
    if not platform:
        return None
    nickname = None
    if language:
        nickname = Setting.get(f"oneup_social_nickname_{platform}_{language}")
    if not nickname:
        nickname = Setting.get(f"oneup_social_nickname_{platform}")
    if nickname:
        return nickname
    return platform.title() + (f" · {language}" if language else "")


def save_temp_media(token, filename, data):
    folder = os.path.join(MEDIA_CACHE_DIR, token)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, filename), "wb") as f:
        f.write(data)


def _host_images_temporarily(images):
    """Saves a list of (filename, bytes) to the temp media cache under one
    shared random token and returns their short-lived public URLs, built
    from the real incoming request's host (not the BASE_URL config value,
    which defaults to http://localhost:5000 and is unreachable from
    OneUp's servers unless set correctly on Render)."""
    if has_request_context():
        base = request.host_url.rstrip("/")
    else:
        base = Config.BASE_URL.rstrip("/")
    token = secrets.token_urlsafe(16)
    urls = []
    for fname, data in images:
        safe_name = secure_filename(fname) or "image.jpg"
        save_temp_media(token, safe_name, data)
        urls.append(f"{base}/media/{token}/{safe_name}")
    return urls


def _uploaded_media_url(media):
    """Builds the permanent, publicly-fetchable URL for a boss-uploaded
    photo (see serve_uploaded_media) — built from the real incoming
    request's host, same reasoning as _host_images_temporarily."""
    if has_request_context():
        base = request.host_url.rstrip("/")
    else:
        base = Config.BASE_URL.rstrip("/")
    return f"{base}/orders/{media.order_id}/uploaded-media/{media.id}/{media.media_key}"


def _select_and_order(order, keyed_items):
    """Applies the boss's saved photo selection/order (order.media_order)
    over a list of (key, item) pairs — shared by both the real publish
    path (expand_drive_links) and the caption generator's photo feed
    (_apply_media_order below). 'key' is a stable identifier for a photo
    (a Drive photo's secure_filename, or an uploaded photo's own
    media_key) — the same thing that ends up as the last path segment of
    that photo's preview URL, which is how the Calendar's drag-to-reorder
    UI reads it back off each thumbnail with no separate lookup.

    order.media_order is stored as {"kept": [...], "known": [...]}: "kept"
    is the exact order to show (and publish) whatever survived; "known" is
    every key the boss has ever been shown for this order, so that a key
    that's simply never been seen before (e.g. a photo just uploaded)
    gets appended at the end automatically, while a key that HAS been
    seen but was deliberately left out of "kept" (removed) stays hidden —
    both would otherwise look identical (just "not in kept").

    Older saves from before this "known" tracking existed stored a bare
    list instead — treated here as a strict, exact filter with no
    auto-append (the original behavior), so an order customized before
    this update keeps behaving exactly the same rather than having
    previously-removed photos unexpectedly reappear. The very next save
    through /set-media-order upgrades it to the new dict format, after
    which auto-append for new photos (e.g. uploads) kicks in normally.

    Falls back to the natural incoming order if nothing's customized yet,
    if the save is unparsable, or if nothing at all survives — this
    should never silently end up publishing zero photos."""
    natural = [v for _, v in keyed_items]
    if not order.media_order:
        return natural
    try:
        saved = json.loads(order.media_order)
    except (ValueError, TypeError):
        return natural

    by_key = {}
    for k, v in keyed_items:
        by_key.setdefault(k, v)

    if isinstance(saved, list):
        ordered = [by_key[k] for k in saved if k in by_key]
        return ordered if ordered else natural

    if not isinstance(saved, dict):
        return natural

    kept = saved.get("kept") or []
    known_set = set(saved.get("known") or kept)
    ordered = [by_key[k] for k in kept if k in by_key]
    for k, v in keyed_items:
        if k not in known_set and k not in kept:
            ordered.append(v)  # never-seen-before item (e.g. just uploaded) -> tack on at the end
    return ordered if ordered else natural


def _apply_media_order(order, images):
    """_select_and_order, specialized for a plain list of (filename,
    bytes) tuples — used by the caption generator (_collect_order_images),
    which only ever deals in Drive-derived photos, not uploads."""
    keyed = [(secure_filename(fname) or "image.jpg", (fname, data)) for fname, data in images]
    return _select_and_order(order, keyed)


def _collect_order_images(order, max_images=6):
    """Resolves an order's Drive link(s) into actual (filename, bytes)
    photo data, for the AI caption generator — a separate, read-only path
    from expand_drive_links (which hosts photos temporarily for OneUp to
    fetch); kept apart on purpose so this feature can't affect publishing.
    Respects the boss's saved photo selection/order (order.media_order) the
    same way expand_drive_links does, so the caption generator "sees" the
    same set of photos that will actually get published. Each source
    (folder/zip) is fully collected and filtered before capping at
    max_images, so removing an early photo doesn't accidentally bump a
    later one out of view."""
    sa_json = Setting.get("google_service_account_json")
    if not sa_json:
        return []
    service = None
    collected = []
    for link in order.drive_link_list:
        if len(collected) >= max_images:
            break
        file_id = drive.extract_file_id(link)
        if not file_id:
            continue
        if service is None:
            service = drive.get_service(sa_json)
        meta = drive.get_file_metadata(service, file_id)
        if drive.is_folder(meta):
            folder_images = [
                (name, drive.download_bytes(service, fid))
                for name, _mime, fid in drive.list_folder_images(service, file_id)
            ]
            collected.extend(_apply_media_order(order, folder_images))
        elif drive.is_zip(meta):
            zip_bytes = drive.download_bytes(service, file_id)
            zip_images = drive.extract_images(zip_bytes)
            collected.extend(_apply_media_order(order, zip_images))
        elif drive.is_image(meta):
            collected.append((meta.get("name", "image.jpg"), drive.download_bytes(service, file_id)))
    return collected[:max_images]


def _resolve_order_media(order):
    """Builds this order's full *natural* photo list — everything pulled
    from its Drive link(s), plus anything manually uploaded straight into
    Content Ops for this task (see the OrderMedia model) — as a single
    ordered list of (key, item) pairs, before any of the boss's
    remove/reorder customization gets applied.

    'key' is a stable identifier: a Drive photo's secure_filename, or an
    uploaded photo's own media_key. Both are also literally the last path
    segment of that photo's eventual preview URL, so the browser can read
    a photo's key straight off its <img> src.

    'item' is either a raw (filename, bytes) tuple — a Drive folder/zip
    photo, downloaded but not yet hosted — or a ready-to-use URL string —
    a plain single-image Drive link (never downloaded, same as always),
    or an uploaded photo's own permanent serving URL."""
    sa_json = Setting.get("google_service_account_json")
    items = []
    service = None
    for i, link in enumerate(order.drive_link_list):
        file_id = drive.extract_file_id(link) if sa_json else None
        if not sa_json or not file_id:
            items.append((f"link-{i}", oneup.normalize_drive_link(link)))
            continue

        if service is None:
            service = drive.get_service(sa_json)

        meta = drive.get_file_metadata(service, file_id)

        if drive.is_folder(meta):
            folder_images = drive.list_folder_images(service, file_id)
            if not folder_images:
                raise drive.DriveError(f"'{meta.get('name')}' is a folder, but no photos were found inside it.")
            for name, _mime, fid in folder_images:
                data = drive.download_bytes(service, fid)
                items.append((secure_filename(name) or "image.jpg", (name, data)))
            continue

        if not drive.is_zip(meta):
            items.append((f"link-{i}", oneup.normalize_drive_link(link)))
            continue

        zip_bytes = drive.download_bytes(service, file_id)
        images = drive.extract_images(zip_bytes)
        if not images:
            raise drive.DriveError(f"'{meta.get('name')}' is a zip, but no photos were found inside it.")
        for name, data in images:
            items.append((secure_filename(name) or "image.jpg", (name, data)))

    for media in order.uploaded_media:
        items.append((media.media_key, _uploaded_media_url(media)))

    return items


def _finalize_media_urls(selected):
    """'selected' is this order's final, already-ordered photo list, each
    entry either a ready URL string (a plain Drive link, or an uploaded
    photo's permanent URL) or a raw (filename, bytes) tuple still needing
    to be hosted (a Drive folder/zip photo). Hosts every raw tuple
    together under one shared temp token — preserving their relative
    order — and splices the results back into their original positions,
    so temp-hosting can't disturb the sequence the boss chose."""
    bytes_positions = [i for i, v in enumerate(selected) if isinstance(v, tuple)]
    if bytes_positions:
        hosted = _host_images_temporarily([selected[i] for i in bytes_positions])
        for i, url in zip(bytes_positions, hosted):
            selected[i] = url
    return selected


def expand_drive_links(order):
    """Turns an order's Drive link(s) *and* any manually uploaded photos
    into the final, ordered list of direct image URLs OneUp will actually
    be given — respecting the boss's saved remove/reorder customization
    (order.media_order) across the combined set, not just within one
    source, so an uploaded photo can be dragged in anywhere relative to
    the Drive ones. This also powers the read-only Calendar preview, so
    what's shown there is exactly what gets published."""
    items = _resolve_order_media(order)
    selected = _select_and_order(order, items)
    return _finalize_media_urls(selected)


def publish_via_oneup(order):
    """Schedules `order` through OneUp's API. Returns (success, message).
    Looks up a per-language account mapping first (e.g. Instagram/English ->
    a specific IG account), falling back to the plain per-platform mapping."""
    api_key = get_oneup_api_key()
    if not api_key:
        return False, "OneUp isn't connected yet (see Settings)."
    if not order.platform:
        return False, "This order has no platform set yet — edit it before scheduling."

    category_id = Setting.get(f"oneup_category_id_{order.platform}") or Setting.get("oneup_category_id")
    social_id_raw = None
    if order.language:
        social_id_raw = Setting.get(f"oneup_social_id_{order.platform}_{order.language}")
    if not social_id_raw:
        social_id_raw = Setting.get(f"oneup_social_id_{order.platform}")

    if not category_id or not social_id_raw:
        who = order.platform.title() + (f" / {order.language}" if order.language else "")
        return False, f"No OneUp account is mapped for {who} yet. Set it up in Settings first."

    # Video/Reel/Story orders go through OneUp's separate video endpoint
    # (schedulevideopost) instead of the image/carousel path below --
    # that's the only way to attach Instagram sound (Instagram audio is
    # video-only on OneUp's end), and it also lets TikTok sound work the
    # same way for a real TikTok video as it already does for a TikTok
    # photo-mode post. The video file itself is just the order's own Drive
    # link, passed straight through -- OneUp accepts a normal Drive share
    # link directly for video_url, no unzip/re-hosting needed like photos.
    if (order.content_type or "carousel") in ("video", "reel", "story"):
        video_url = order.drive_link_list[0] if order.drive_link_list else None
        if not video_url:
            return False, "This is a video/Reel task but has no Drive link to the video file yet — add one on the Board first."
        try:
            social_ids = json.loads(social_id_raw) if social_id_raw.startswith("[") else [social_id_raw]
            result = oneup.schedule_video_post(
                api_key=api_key,
                category_id=category_id,
                social_network_ids=social_ids,
                scheduled_date_time=order.scheduled_at.strftime("%Y-%m-%d %H:%M"),
                content=order.caption or "",
                video_url=video_url,
                title=order.title or None,
                first_comment=order.first_comment or None,
                instagram_music=order.instagram_sound_dict if order.platform == "instagram" else None,
                tiktok_music=order.tiktok_sound_dict if order.platform == "tiktok" else None,
            )
            order.status = "scheduled"
            order.oneup_response = json.dumps(result)
            scheduled_time_str = order.scheduled_at.strftime("%Y-%m-%d %H:%M")
            order.oneup_post_id = oneup.find_scheduled_post_id(api_key, order.caption or "", scheduled_time_str)
            db.session.commit()
            return True, "Sent to OneUp and scheduled."
        except oneup.OneUpError as e:
            order.status = "failed"
            order.oneup_response = str(e)
            order.oneup_post_id = None
            db.session.commit()
            return False, f"OneUp rejected this post: {e}"
        except Exception as e:  # noqa: BLE001 - final safety net (network blips, etc.) -- never 500
            order.status = "failed"
            order.oneup_response = str(e)
            order.oneup_post_id = None
            db.session.commit()
            return False, f"Something went wrong sending this to OneUp: {e}"

    try:
        image_urls = expand_drive_links(order)
    except drive.DriveError as e:
        order.status = "failed"
        order.oneup_response = str(e)
        db.session.commit()
        return False, str(e)
    except Exception as e:  # noqa: BLE001 - a bad link/zip must never 500 the request
        order.status = "failed"
        order.oneup_response = str(e)
        db.session.commit()
        return False, f"Couldn't prepare the photos for this post: {e}"

    if not image_urls:
        return False, "This order has no Drive link(s) set yet — add one on the Board first."

    try:
        social_ids = json.loads(social_id_raw) if social_id_raw.startswith("[") else [social_id_raw]
        result = oneup.schedule_image_post(
            api_key=api_key,
            category_id=category_id,
            social_network_ids=social_ids,
            scheduled_date_time=order.scheduled_at.strftime("%Y-%m-%d %H:%M"),
            content=order.caption or "",
            image_urls=image_urls,
            title=order.title or None,
            first_comment=order.first_comment or None,
            tiktok_music=order.tiktok_sound_dict if order.platform == "tiktok" else None,
        )
        order.status = "scheduled"
        order.oneup_response = json.dumps(result)
        # scheduleimagepost's own response never includes the post_id it
        # just created, so it's looked up right after via the scheduled
        # queue and stashed here — needed later to cancel/redo this exact
        # post if its date gets dragged to a new time on the Calendar.
        scheduled_time_str = order.scheduled_at.strftime("%Y-%m-%d %H:%M")
        order.oneup_post_id = oneup.find_scheduled_post_id(api_key, order.caption or "", scheduled_time_str)
        db.session.commit()
        return True, "Sent to OneUp and scheduled."
    except oneup.OneUpError as e:
        order.status = "failed"
        order.oneup_response = str(e)
        order.oneup_post_id = None  # any old post_id is stale once this attempt failed
        db.session.commit()
        return False, f"OneUp rejected this post: {e}"
    except Exception as e:  # noqa: BLE001 - final safety net (network blips, etc.) — never 500
        order.status = "failed"
        order.oneup_response = str(e)
        order.oneup_post_id = None
        db.session.commit()
        return False, f"Something went wrong sending this to OneUp: {e}"


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
