#!/usr/bin/env python3
"""Send an email (plain text, plus optional HTML) via Gmail SMTP. Reads credentials from .env."""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path


class EmailNotConfigured(RuntimeError):
    pass


def load_env():
    """Read .env when it exists. On a host like Render the variables are set directly instead."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def send(to: str, subject: str, body: str, html: str | None = None) -> None:
    load_env()
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_app_password:
        raise EmailNotConfigured("GMAIL_USER or GMAIL_APP_PASSWORD is not set")

    # multipart/alternative: mail apps show the HTML part, and fall back to plain text.
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{os.environ.get('EMAIL_FROM_NAME', 'Shopping Assistant')} <{gmail_user}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, to, msg.as_string())

    print(f"Sent: {subject} → {to}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    args = parser.parse_args()
    try:
        send(args.to, args.subject, args.body)
    except EmailNotConfigured as error:
        print(f"ERROR: {error}. Copy .env.example to .env and fill in your credentials.", file=sys.stderr)
        sys.exit(1)
