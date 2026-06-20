#!/usr/bin/env python3
"""Send a plain-text email via Gmail SMTP. Reads credentials from .env."""

import argparse
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path


def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print("ERROR: .env file not found. Copy .env.example to .env and fill in your credentials.", file=sys.stderr)
        sys.exit(1)
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def send(to: str, subject: str, body: str) -> None:
    load_env()
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_app_password:
        print("ERROR: GMAIL_USER or GMAIL_APP_PASSWORD not set in .env", file=sys.stderr)
        sys.exit(1)

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
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
    send(args.to, args.subject, args.body)
