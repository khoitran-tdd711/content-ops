"""Best-effort SMTP email notifications. If SMTP isn't configured, calls are
silently skipped (logged to stdout) so the rest of the app keeps working."""

import smtplib
from email.mime.text import MIMEText

from flask import current_app


def send_email(to_email, subject, body):
    host = current_app.config.get("SMTP_HOST")
    if not host:
        print(f"[mailer] SMTP not configured, skipping email to {to_email}: {subject}")
        return False

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
        print(f"[mailer] Failed to send email to {to_email}: {e}")
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
