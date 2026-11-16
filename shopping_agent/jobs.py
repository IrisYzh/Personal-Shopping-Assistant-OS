"""Background jobs for the pilot server.

  * Every 6 hours: check price and stock for every watched item of every user.
  * Every minute:  email the changes each person asked to hear about right away.
  * Every day at 9 AM (PILOT_TIMEZONE): the daily summary, for people who turned it on.
  * Saturday 9 AM: the weekly summary.

Emails go only to the address each person typed in Settings. Every email and every check
is written to the analytics log, so the metrics report can count them.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .behavior_intent import parse_time
from .digest import build_weekly_digest, instant_alert
from .orchestrator import utc_now
from .page_reader import fetch_page, observation_from, parse_html
from .tracking import link_maker

log = logging.getLogger(__name__)

CHECK_EVERY_HOURS = 6
DIGEST_HOUR = 9
WEEKLY_DAY = 5  # Saturday (Monday is 0)
INSTANT_MAX_AGE_HOURS = 48  # Do not email a change that is older than this (for example after downtime).
ALERT_TO_OPTION = {"restock": "size_back", "price_drop": "price_drop", "promotion": "sale_start"}

Mailer = Callable[[str, str, str, str], None]


def default_mailer(to: str, subject: str, body: str, html: str) -> None:
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from send_email import send

    send(to, subject, body, html)


def pilot_timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("PILOT_TIMEZONE", "America/Los_Angeles"))


class PilotJobs:
    def __init__(self, api, *, mailer: Mailer | None = None, fetcher: Callable[[str], str] | None = None,
                 state_path: str | Path | None = None):
        self.api = api
        self.mailer = mailer or default_mailer
        self.fetcher = fetcher or fetch_page
        self.state_path = Path(state_path or Path(api.data_dir) / "jobs_state.json")
        self._stop = threading.Event()

    # ----- people ---------------------------------------------------------------

    def users(self) -> list[str]:
        names = set(self.api.pilot_keys.values()) if self.api.pilot_keys else {"me"}
        return sorted(names)

    def email_for(self, user: str) -> str | None:
        prefs = self.api.context(user).repository.get_preferences()
        address = (prefs.get("email_address") or "").strip()
        if not address and user == "me":
            address = os.environ.get("DIGEST_TO_EMAIL", "").strip()
        return address or None

    def email_choices(self, user: str) -> dict[str, bool]:
        from .server import DEFAULT_EMAIL

        return {**DEFAULT_EMAIL, **self.api.context(user).repository.get_preferences().get("email", {})}

    # ----- price and stock checks ---------------------------------------------------

    def check_all(self) -> dict[str, int]:
        totals = {"checked": 0, "ok": 0, "changes": 0}
        for user in self.users():
            ctx = self.api.context(user)
            with ctx.lock:
                items = [item for item in ctx.repository.list_items() if item.status == "watching"]
            for item in items:
                result = self.check_one(user, item)
                totals["checked"] += 1
                totals["ok"] += int(result["ok"])
                totals["changes"] += len(result["alerts"])
                time.sleep(float(os.environ.get("CHECK_PAUSE_SECONDS", "2")))  # Be gentle with the shops.
        log.info("Price and stock check: %s", totals)
        return totals

    def check_one(self, user: str, item) -> dict:
        checked_at = utc_now()
        try:
            product = parse_html(self.fetcher(item.url))
            error = None if product else "No product data found on the page"
        except Exception as exc:  # A blocked or slow site is a normal outcome, not a crash.
            product, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
        observation = observation_from(product, size=item.size, color=item.color, checked_at=checked_at, error=error)
        return self.api.apply_observation(user, item.id, observation, via="scheduled_check")

    # ----- emails -------------------------------------------------------------------

    def send_instant_alerts(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        sent = 0
        for user in self.users():
            address = self.email_for(user)
            choices = self.email_choices(user)
            if not address:
                continue
            ctx = self.api.context(user)
            with ctx.lock:
                done = ctx.repository.emailed_alert_ids()
                alerts = ctx.repository.snapshot().get("alerts", [])
                items = {item.id: item for item in ctx.repository.list_items() if item.status == "watching"}
            for alert in alerts:
                option = ALERT_TO_OPTION.get(alert.get("type"))
                if (
                    alert["id"] in done
                    or alert.get("item_id") not in items
                    or not option
                    or not choices.get(option)
                    or now - parse_time(alert["created_at"]) > timedelta(hours=INSTANT_MAX_AGE_HOURS)
                ):
                    continue
                email_id = str(uuid4())
                email = instant_alert(items[alert["item_id"]], alert, link_maker(user, email_id, f"instant_{option}"))
                if email and self._deliver(user, address, email.subject, email.body, email.html,
                                           email_id=email_id, kind=f"instant_{option}", alert_ids=[alert["id"]]):
                    sent += 1
        return sent

    def send_digests(self, kind: str, now: str | None = None) -> int:
        """kind is "weekly_digest" or "daily_digest". No change in the window means no email."""
        window = 7 if kind == "weekly_digest" else 1
        sent = 0
        for user in self.users():
            address = self.email_for(user)
            if not address or not self.email_choices(user).get(kind):
                continue
            email_id = str(uuid4())
            ctx = self.api.context(user)
            with ctx.lock:
                digest = build_weekly_digest(ctx.repository, now=now, window_days=window,
                                             link_for=link_maker(user, email_id, kind))
            if digest and self._deliver(user, address, digest.subject, digest.body, digest.html,
                                        email_id=email_id, kind=kind, alert_ids=[], items=digest.change_count):
                sent += 1
        return sent

    def _deliver(self, user, address, subject, body, html, *, email_id, kind, alert_ids, items=1) -> bool:
        try:
            self.mailer(address, subject, body, html)
        except Exception as exc:
            log.warning("Email to %s failed: %s", user, exc)
            self.api.analytics.log("email_failed", user, kind=kind, error=type(exc).__name__)
            return False
        ctx = self.api.context(user)
        with ctx.lock:
            ctx.repository.append_email_record(
                {"email_id": email_id, "kind": kind, "alert_ids": alert_ids, "sent_at": utc_now()}
            )
        self.api.analytics.log("email_sent", user, email_id=email_id, kind=kind, items=items)
        return True

    # ----- schedule -------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def tick(self, now: datetime | None = None) -> dict:
        """Run whatever is due. Safe to call often; the state file stops repeats after a restart."""
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(pilot_timezone())
        state = self._load_state()
        ran: dict = {}

        last_check = state.get("last_check")
        if not last_check or now - parse_time(last_check) >= timedelta(hours=CHECK_EVERY_HOURS):
            state["last_check"] = now.isoformat()
            self._save_state(state)
            ran["check"] = self.check_all()

        ran["instant"] = self.send_instant_alerts(now)

        today = local.date().isoformat()
        if local.hour >= DIGEST_HOUR and state.get("last_daily") != today:
            state["last_daily"] = today
            self._save_state(state)
            ran["daily"] = self.send_digests("daily_digest", now.isoformat())
        if local.weekday() == WEEKLY_DAY and local.hour >= DIGEST_HOUR and state.get("last_weekly") != today:
            state["last_weekly"] = today
            self._save_state(state)
            ran["weekly"] = self.send_digests("weekly_digest", now.isoformat())
        return ran

    def run_forever(self, every_seconds: int = 60) -> None:
        log.info("Background jobs on: checks every %sh, summaries at %s:00 %s",
                 CHECK_EVERY_HOURS, DIGEST_HOUR, pilot_timezone().key)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Background job failed")
            self._stop.wait(every_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="pilot-jobs", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

