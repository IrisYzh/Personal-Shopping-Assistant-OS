"""Signed links for email click tracking.

Every product link in an email points to {PUBLIC_URL}/r/<token>. The token is signed with
TRACKING_SECRET, so nobody can forge a click or turn the server into an open redirect: the
redirect always goes to the saved item's own URL, never to a URL inside the token.

There is no open-tracking pixel on purpose. Pixels are a strong spam signal, and most mail
apps block them anyway, so open counts would be wrong.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os


def _secret() -> bytes:
    return (os.environ.get("TRACKING_SECRET") or "local-dev-secret-change-me").encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(user: str, item_id: str, email_id: str, kind: str) -> str:
    payload = _b64(json.dumps({"u": user, "i": item_id, "e": email_id, "k": kind}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest()[:16])
    return f"{payload}.{signature}"


def read_token(token: str) -> dict[str, str] | None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest()[:16])
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        return {"user": data["u"], "item_id": data["i"], "email_id": data["e"], "kind": data["k"]}
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def public_url() -> str:
    return (os.environ.get("PUBLIC_URL") or "").rstrip("/")


def link_maker(user: str, email_id: str, kind: str):
    """Return link_for(item) for the email builders. Without PUBLIC_URL, links go straight to the shop."""
    base = public_url()

    def link_for(item) -> str:
        if not base:
            return item.url
        return f"{base}/r/{make_token(user, item.id, email_id, kind)}"

    return link_for
