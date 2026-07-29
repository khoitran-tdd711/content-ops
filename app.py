import json
from datetime import datetime
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

import mailer
import oneup
from config import Config
from models import CONTENT_TYPES, PLATFORMS, STATUS_LABELS, Order, Setting, User, db


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
    @app.route("/login", methods=["GET", "POST"])
    def login():
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
            events.append(
                {
                    "id": o.id,
                    "title": f"{o.quantity}x {o.content_type} • {o.platform.title()}"
                    f"{' → ' + o.producer.name if o.producer else ''}",
                    "start": o.due_date.isoformat(),
                    "color": o.status_color,
                    "url": url_for("order_detail", order_id=o.id),
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
        order.status = "approved"
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

        category_id = Setting.get("oneup_category_id")
        social_id_raw = Setting.get(f"oneup_social_id_{order.platform}")
        if not category_id or not social_id_raw:
            flash(
                f"No OneUp account is mapped for {order.platform.title()} yet. "
                "Set it up in Settings first.",
                "error",
            )
            return redirect(url_for("order_detail", order_id=order.id))

        image_urls = [oneup.normalize_drive_link(link) for link in order.drive_link_list]
        if not image_urls:
            flash("This order has no media links to publish.", "error")
            return redirect(url_for("order_detail", order_id=order.id))

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
            mailer.notify_status_change(
                order, f"Scheduled for {order.scheduled_at} via OneUp."
            )
            flash("Sent to OneUp and scheduled.", "success")
        except oneup.OneUpError as e:
            order.status = "failed"
            order.oneup_response = str(e)
            db.session.commit()
            flash(f"OneUp rejected this post: {e}", "error")

        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/mark-published", methods=["POST"])
    @boss_required
    def order_mark_published(order_id):
        """Used to close out the loop after a manual OneUp upload."""
        order = Order.query.get_or_404(order_id)
        order.status = "scheduled"
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
        return render_template(
            "settings.html",
            api_key=api_key,
            category_id=Setting.get("oneup_category_id", ""),
            mapping=mapping,
            platforms=PLATFORMS,
        )

    @app.route("/settings/save", methods=["POST"])
    @boss_required
    def settings_save():
        Setting.set("oneup_api_key", request.form.get("api_key", "").strip())
        Setting.set("oneup_category_id", request.form.get("category_id", "").strip())
        for p in PLATFORMS:
            Setting.set(f"oneup_social_id_{p}", request.form.get(f"social_id_{p}", "").strip())
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


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
