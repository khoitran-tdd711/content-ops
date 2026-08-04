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
    STATUS_LABELS,
    Order,
    Setting,
    User,
    db,
)


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
        return render_template("calendar.html", producers=producers, platforms=PLATFORMS)

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
                    "platform": o.platform,
                    "caption": o.caption or "",
                    "scheduledAt": o.scheduled_at.strftime("%Y-%m-%dT%H:%M") if o.scheduled_at else None,
                    "projectTitle": o.title or "(untitled)",
                    "accountLabel": get_account_nickname(o.platform, o.language),
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

        The language field also accepts the special value "__all__" ("All
        available languages" in the dropdown) — instead of one task with no
        language set, this creates one task per language in LANGUAGES, all
        identical otherwise (same title/platform/pub date/etc.), so the boss
        doesn't have to create one task then duplicate it N times and set
        each language by hand."""
        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        if request.method == "POST":
            already_published = request.form.get("already_published") == "yes"
            producer_id = request.form.get("producer_id") or None
            language_input = request.form.get("language") or None
            languages_to_create = LANGUAGES if language_input == "__all__" else [language_input]
            platform = request.form.get("platform") or "instagram"
            scheduled_str = request.form.get("scheduled_at")
            scheduled_at = datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M") if scheduled_str else None

            for language in languages_to_create:
                order = Order(
                    platform=platform,
                    language=language,
                    content_type=request.form.get("content_type", "carousel"),
                    quantity=int(request.form.get("quantity", 1) or 1),
                    title=request.form.get("title", "").strip(),
                    caption=request.form.get("caption", "").strip(),
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
            if len(languages_to_create) > 1:
                flash(f"Created {len(languages_to_create)} new tasks — one per language.", "success")
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

        query = Order.query
        if q:
            query = query.filter(Order.title.ilike(f"%{q}%"))
        if language:
            query = query.filter(Order.language == language)
        if platform:
            query = query.filter(Order.platform == platform)
        if status:
            query = query.filter(Order.status == status)

        date_order = Order.date_ordered.asc() if sort == "asc" else Order.date_ordered.desc()
        orders = query.order_by(Order.date_ordered.is_(None), date_order, Order.title).all()

        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        return render_template(
            "board.html",
            orders=orders,
            platforms=PLATFORMS,
            languages=LANGUAGES,
            board_statuses=BOARD_STATUSES,
            producers=producers,
            content_types=CONTENT_TYPES,
            filters={"q": q, "language": language, "platform": platform, "status": status, "sort": sort},
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
        while the real OneUp post kept firing at its old time."""
        order = Order.query.get_or_404(order_id)
        was_live_in_oneup = order.status == "scheduled" and order.oneup_post_id
        old_post_id = order.oneup_post_id
        old_scheduled_at = order.scheduled_at

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

        oneup_note = None
        time_changed = order.scheduled_at != old_scheduled_at
        if was_live_in_oneup and order.scheduled_at and time_changed:
            api_key = get_oneup_api_key()
            try:
                oneup.delete_scheduled_post(api_key, old_post_id)
            except oneup.OneUpError as e:
                oneup_note = f"Moved locally, but couldn't cancel the old OneUp post ({e}) — check OneUp directly."
            ok, message = publish_via_oneup(order)
            if not ok:
                oneup_note = f"Moved locally and cancelled the old OneUp post, but rescheduling it failed: {message}"
        elif order.status == "scheduled" and not order.oneup_post_id and time_changed:
            oneup_note = (
                "This was scheduled before OneUp sync tracking existed, so its actual OneUp "
                "time wasn't updated — please move it in OneUp directly too."
            )

        db.session.commit()
        if wants_json():
            return {"ok": True, "oneup_note": oneup_note}
        flash(oneup_note, "error") if oneup_note else flash("Pub date updated.", "success")
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
        db.session.commit()
        if wants_json():
            return {"ok": True, "open_link": order.drive_link_list[0] if order.drive_link_list else None}
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
        runs the exact same Drive link -> image URLs logic used for the
        real publish (expand_drive_links), just without actually sending
        anything to OneUp. So what you see here is what will go out."""
        order = Order.query.get_or_404(order_id)
        if not order.drive_link_list:
            return {"images": [], "error": "No Drive link set yet for this task."}
        try:
            image_urls = expand_drive_links(order)
        except drive.DriveError as e:
            return {"images": [], "error": str(e)}
        except Exception as e:  # noqa: BLE001 - a preview must never 500
            return {"images": [], "error": f"Couldn't load a preview: {e}"}
        return {"images": image_urls}

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


def _collect_order_images(order, max_images=6):
    """Resolves an order's Drive link(s) into actual (filename, bytes)
    photo data, for the AI caption generator — a separate, read-only path
    from expand_drive_links (which hosts photos temporarily for OneUp to
    fetch); kept apart on purpose so this feature can't affect publishing.
    Capped at max_images since a caption only needs to 'see' the carousel,
    not necessarily every single slide."""
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
            for name, _mime, fid in drive.list_folder_images(service, file_id):
                if len(collected) >= max_images:
                    break
                collected.append((name, drive.download_bytes(service, fid)))
        elif drive.is_zip(meta):
            zip_bytes = drive.download_bytes(service, file_id)
            for name, data in drive.extract_images(zip_bytes):
                if len(collected) >= max_images:
                    break
                collected.append((name, data))
        elif drive.is_image(meta):
            collected.append((meta.get("name", "image.jpg"), drive.download_bytes(service, file_id)))
    return collected[:max_images]


def expand_drive_links(order):
    """Turns each of an order's Drive links into one or more direct image
    URLs for OneUp. A plain single-image link passes through unchanged
    (same as always). A link to a .zip or to a folder full of loose photos
    gets opened via the Google service account configured in Settings, and
    each photo inside gets its own short-lived public URL for OneUp to
    fetch. If no service account is configured, zip/folder links are just
    left alone (as plain image links, which will fail against OneUp with a
    clear enough error)."""
    sa_json = Setting.get("google_service_account_json")
    urls = []
    service = None
    for link in order.drive_link_list:
        file_id = drive.extract_file_id(link) if sa_json else None
        if not sa_json or not file_id:
            urls.append(oneup.normalize_drive_link(link))
            continue

        if service is None:
            service = drive.get_service(sa_json)

        meta = drive.get_file_metadata(service, file_id)

        if drive.is_folder(meta):
            folder_images = drive.list_folder_images(service, file_id)
            if not folder_images:
                raise drive.DriveError(f"'{meta.get('name')}' is a folder, but no photos were found inside it.")
            downloaded = [(name, drive.download_bytes(service, fid)) for name, _mime, fid in folder_images]
            urls.extend(_host_images_temporarily(downloaded))
            continue

        if not drive.is_zip(meta):
            urls.append(oneup.normalize_drive_link(link))
            continue

        zip_bytes = drive.download_bytes(service, file_id)
        images = drive.extract_images(zip_bytes)
        if not images:
            raise drive.DriveError(f"'{meta.get('name')}' is a zip, but no photos were found inside it.")
        urls.extend(_host_images_temporarily(images))
    return urls


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
