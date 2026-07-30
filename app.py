import io
import json
import zipfile
from datetime import date, datetime
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

import language_tracker
import mailer
import oneup
from config import Config
from models import (
    BOARD_STATUSES,
    CONTENT_TYPES,
    LANGUAGES,
    PLATFORMS,
    STATUS_LABELS,
    Order,
    Setting,
    User,
    db,
)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()

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

    @app.route("/")
    @login_required
    def calendar_view():
        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        return render_template("calendar.html", producers=producers, platforms=PLATFORMS)

    @app.route("/api/orders.json")
    @login_required
    def orders_json():
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
                    "url": url_for("order_detail", order_id=o.id),
                    "bucket": bucket,
                }
            )
        return {"events": events}

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
        Board. Pub date is optional; leave it blank and set it later."""
        producers = User.query.filter_by(role="producer").order_by(User.name).all()
        if request.method == "POST":
            already_published = request.form.get("already_published") == "yes"
            producer_id = request.form.get("producer_id") or None
            language = request.form.get("language") or None
            platform = request.form.get("platform") or "instagram"
            due_str = request.form.get("due_date")
            order = Order(
                platform=platform,
                language=language,
                content_type=request.form.get("content_type", "carousel"),
                quantity=int(request.form.get("quantity", 1) or 1),
                title=request.form.get("title", "").strip(),
                caption=request.form.get("caption", "").strip(),
                due_date=datetime.strptime(due_str, "%Y-%m-%d").date() if due_str else None,
                date_ordered=date.today(),
                producer_id=int(producer_id) if producer_id else None,
                created_by_id=g.user.id,
                drive_links=request.form.get("drive_links", "").strip(),
                status="published" if already_published else "ready",
            )
            db.session.add(order)
            db.session.commit()
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

    @app.route("/orders/<int:order_id>/set-date", methods=["POST"])
    @boss_required
    def order_set_date(order_id):
        order = Order.query.get_or_404(order_id)
        due_str = request.form.get("due_date")
        order.due_date = datetime.strptime(due_str, "%Y-%m-%d").date() if due_str else None
        platform = request.form.get("platform")
        if platform:
            order.platform = platform
        db.session.commit()
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
            flash("Status updated.", "success")
        else:
            flash("Not a valid status.", "error")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-drive-link", methods=["POST"])
    @boss_required
    def order_set_drive_link(order_id):
        order = Order.query.get_or_404(order_id)
        order.drive_links = request.form.get("drive_links", "").strip()
        db.session.commit()
        flash("Drive link updated.", "success")
        return redirect(request.referrer or url_for("board_view"))

    @app.route("/orders/<int:order_id>/set-producer", methods=["POST"])
    @boss_required
    def order_set_producer(order_id):
        order = Order.query.get_or_404(order_id)
        producer_id = request.form.get("producer_id")
        order.producer_id = int(producer_id) if producer_id else None
        db.session.commit()
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
        flash("Platform updated.", "success")
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
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/publish-now", methods=["POST"])
    @boss_required
    def order_publish_now(order_id):
        """Quick action from the Calendar popup — schedules straight through
        OneUp's API using the pub date already set, no form needed."""
        order = Order.query.get_or_404(order_id)
        if not order.due_date:
            flash("This task has no pub date set yet — set one on the Board or Calendar first.", "error")
            return redirect(url_for("calendar_view"))

        if not order.scheduled_at:
            order.scheduled_at = datetime.combine(order.due_date, datetime.min.time().replace(hour=12))
            db.session.commit()

        api_key = get_oneup_api_key()
        if not api_key:
            flash("OneUp isn't connected yet — add your API key in Settings first.", "error")
            return redirect(url_for("calendar_view"))

        success, message = publish_via_oneup(order)
        if success:
            mailer.notify_status_change(order, f"Scheduled for {order.scheduled_at} via OneUp.")
        flash(message, "success" if success else "error")
        return redirect(url_for("calendar_view"))

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
        return render_template(
            "settings.html",
            api_key=api_key,
            mapping=mapping,
            category_mapping=category_mapping,
            platforms=PLATFORMS,
            languages=LANGUAGES,
            language_mapping=language_mapping,
        )

    @app.route("/settings/save", methods=["POST"])
    @boss_required
    def settings_save():
        Setting.set("oneup_api_key", request.form.get("api_key", "").strip())
        for p in PLATFORMS:
            Setting.set(f"oneup_social_id_{p}", request.form.get(f"social_id_{p}", "").strip())
            Setting.set(f"oneup_category_id_{p}", request.form.get(f"category_id_{p}", "").strip())
            for l in LANGUAGES:
                Setting.set(
                    f"oneup_social_id_{p}_{l}",
                    request.form.get(f"social_id_{p}_{l}", "").strip(),
                )
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

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

    image_urls = [oneup.normalize_drive_link(link) for link in order.drive_link_list]
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
        db.session.commit()
        return True, "Sent to OneUp and scheduled."
    except oneup.OneUpError as e:
        order.status = "failed"
        order.oneup_response = str(e)
        db.session.commit()
        return False, f"OneUp rejected this post: {e}"


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
