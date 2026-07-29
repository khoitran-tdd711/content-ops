"""Best-effort email notifications with two interchangeable backends:

1. Resend (HTTPS API) — used automatically if RESEND_API_KEY is set. This is
   the one that works on free hosts like Render's free web service, which
   blocks outbound SMTP ports (25/465/587).
2. Classic SMTP — used if SMTP_HOST is set instead (fine on paid hosts or
   your own server).

If neither is configured, sends are skipped and logged to stdout so the rest
of the app keeps working.
"""

import smtplib
from email.mime.text import MIMEText

import requests
from flask import current_app


def send_email(to_email, subject, body):
    if current_app.config.get("RESEND_API_KEY"):
        return _send_via_resend(to_email, subject, body)
    if current_app.config.get("SMTP_HOST"):
        return _send_via_smtp(to_email, subject, body)
    print(f"[mailer] No email backend configured, skipping email to {to_email}: {subject}")
    return False


def _send_via_resend(to_email, subject, body):
    api_key = current_app.config["RESEND_API_KEY"]
    from_email = current_app.config.get("RESEND_FROM") or "onboarding@resend.dev"
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[mailer] Resend rejected email to {to_email}: {r.status_code} {r.text[:300]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001 - never let a bad email config crash the app
        print(f"[mailer] Failed to send email via Resend to {to_email}: {e}")
        return False


def _send_via_smtp(to_email, subject, body):
    host = current_app.config["SMTP_HOST"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = current_app.config.get("SMTP_FROM")
    msg["To"] = to_email

    port = current_app.config.get("SMTP_PORT", 587)
    user = current_app.config.get("SMTP_USER")
    password = current_app.config.get("SMTP_PASSWORD")

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            if user:
                server.login(user, password)
            server.sendmail(msg["From"], [to_email], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001 - never let a bad SMTP config crash the app
        print(f"[mailer] Failed to send email via SMTP to {to_email}: {e}")
        return False


def notify_new_order(order):
    if not order.producer:
        return
    link = f"{current_app.config['BASE_URL']}/orders/{order.id}"
    body = (
        f"Hi {order.producer.name},\n\n"
        f"You have a new order:\n"
        f"  {order.quantity}x {order.content_type} for {order.platform.title()}\n"
        f"  Due: {order.due_date}\n\n"
        f"View it here: {link}\n"
    )
    send_email(order.producer.email, f"New order due {order.due_date}", body)


def notify_submitted(order):
    if not order.creator:
        return
    link = f"{current_app.config['BASE_URL']}/orders/{order.id}"
    body = (
        f"Hi {order.creator.name},\n\n"
        f"{order.producer.name if order.producer else 'The producer'} submitted work for review "
        f"on order #{order.id} ({order.platform.title()}, due {order.due_date}).\n\n"
        f"Review it here: {link}\n"
    )
    send_email(order.creator.email, f"Order #{order.id} ready for review", body)


def notify_status_change(order, message):
    """Generic notifier used for approve / request-changes / publish results."""
    recipient = None
    if order.status == "to_modify" and order.producer:
        recipient = order.producer
    elif order.producer:
        recipient = order.producer

    if not recipient:
        return
    link = f"{current_app.config['BASE_URL']}/orders/{order.id}"
    body = f"Hi {recipient.name},\n\n{message}\n\nView it here: {link}\n"
    send_email(recipient.email, f"Order #{order.id} update: {order.status_label}", body)
