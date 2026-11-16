# Browser extension and backend

This is the pilot setup from the PRD: a Chrome extension for Lululemon, SKIMS, Nordstrom and
Sephora, one shared backend, a Google Sheet copy of every wishlist, and a weekly email.

## How the pieces fit

```
Chrome extension  ->  backend API (shopping_agent/server.py)  ->  JSON state per user
   content.js           POST /events, /confirm                       data/users/<user>/state.json
   popup.html           GET /wishlist, PATCH and DELETE /wishlist/<id>
   background.js        PUT /preferences, DELETE /history
                                  |
                                  +-> Google Sheet (Apps Script web app, optional)
                                  +-> weekly digest email (python -m shopping_agent weekly-digest)
```

* A page view counts after 8 seconds of visible time on a product page.
* Add to cart or wishlist saves the item right away, with no popup.
* The 3rd view in 7 days across 2 sessions shows the in-page card. Each product is asked about
  once, and at most one card shows per day.
* Browsing history stays on the backend for 30 days and never goes to the sheet.

## 1. Run the backend

Local, for yourself:

```bash
python -m shopping_agent serve            # http://127.0.0.1:8787, no key needed
```

Hosted, for the pilot. Any small Python host works (Render, Railway, Fly.io, a VPS). Give each
friend a name and a long random key, then start the server on a public address:

```bash
export PILOT_KEYS="iris:$(openssl rand -hex 16),amy:$(openssl rand -hex 16)"
python -m shopping_agent serve --host 0.0.0.0 --port 8787 --data-dir data/users
```

The server refuses to listen on a public address without `PILOT_KEYS`. Put it behind HTTPS
(most hosts do this for you), because Chrome only lets the extension reach `https://` backends
or `localhost`.

## 2. Load the extension

1. Open `chrome://extensions` and turn on Developer mode.
2. Click Load unpacked and pick the `extension` folder.
3. Open the extension, go to Settings, and enter the backend URL and your pilot key.
   Chrome asks once for permission to reach a hosted backend.
4. Tell each friend to add the sender address to their contacts, so emails skip Promotions.

## 3. Google Sheet copy (optional)

1. Create a Google Sheet. Put these headers in row 1 of the first tab:
   `id, user, name, url, site, added_via, size, color, baseline_price, last_price, currency,
   availability, status, created_at`
2. Extensions, Apps Script, and paste the script below.
3. Deploy, New deployment, type Web app, execute as Me, access Anyone. Copy the URL.
4. Set `SHEETS_WEBHOOK_URL` to that URL in `.env` on the backend.

```javascript
// Upserts or removes wishlist rows sent by the Shopping Assistant backend.
const SECRET = ""; // Optional. If set, the backend must send the same value as ?secret=...

function doPost(e) {
  if (SECRET && e.parameter.secret !== SECRET) return json({ ok: false, error: "forbidden" });
  const body = JSON.parse(e.postData.contents);
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const ids = sheet.getLastRow() > 1 ? sheet.getRange(2, 1, sheet.getLastRow() - 1, 1).getValues().flat() : [];
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    if (body.action === "remove") {
      const index = ids.indexOf(body.id);
      if (index >= 0) sheet.deleteRow(index + 2);
    } else if (body.action === "upsert") {
      const row = headers.map((header) => body.row[header] ?? "");
      const index = ids.indexOf(body.row.id);
      if (index >= 0) sheet.getRange(index + 2, 1, 1, row.length).setValues([row]);
      else sheet.appendRow(row);
    }
  } finally {
    lock.releaseLock();
  }
  return json({ ok: true });
}

function json(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
```

If you set `SECRET`, add `?secret=...` to the end of `SHEETS_WEBHOOK_URL`.
A sheet outage never blocks the wishlist. The backend logs the error and moves on.

## 4. Price checks and emails

`serve --jobs` runs the pilot jobs inside the server (see `shopping_agent/jobs.py`):

* every 6 hours, a price and stock check of every watched item, at the saved size when the
  page lists sizes in its structured data (`shopping_agent/page_reader.py`)
* every minute, instant emails for the changes each person turned on (size back, price drop,
  sale), each sent once
* 9 AM daily summary and Saturday 9 AM weekly summary, in `PILOT_TIMEZONE`

A friend's visit to a product they watch also counts as a check, so prices stay fresh even on
sites that block servers. Emails go only to the address each person saves in Settings.
Product links in emails pass through `/r/<signed token>` to count clicks.

Run one job by hand:

```bash
python -m shopping_agent run-jobs --only check     # or instant, daily, weekly; no flag runs what is due
python -m shopping_agent --state data/users/iris/state.json weekly-digest   # preview one digest
```

## 5. Models

The three agents run on Claude by default: Product Intelligence and Supervisor on
`claude-haiku-5-5`, Verifier on `claude-sonnet-5-5`. Set `ANTHROPIC_API_KEY` in `.env`, and
change a role with `CLAUDE_PRODUCT_MODEL`, `CLAUDE_SUPERVISOR_MODEL` or `CLAUDE_VERIFIER_MODEL`.
The old Doubao setup still works with `SHOPPING_AGENT_PROVIDER=ark`.

## Supported sites and email choices

* The list of supported sites lives in `shopping_agent/site_catalog.json`. The settings page
  searches it through `GET /sites?q=lulu`. Add a site there (plus a content script match in
  `extension/manifest.json`) to support it.
* Email choices are checkboxes saved per user through `PUT /preferences` as
  `{"email": {"weekly_digest", "daily_digest", "size_back", "price_drop", "sale_start"}}`.
* Emails have a plain text part and a simple HTML part. Colors for the highlighted changes are
  at the top of `shopping_agent/digest.py`.

## Preview every screen

Double-click `extension/preview.html`. It shows the in-page card, the popup with sample data
(wishlist, empty, error, settings) and the three emails, all built from the real UI files.
After editing `shopping_agent/digest.py`, run `python -m shopping_agent preview-emails` and refresh.

## Tests

```bash
python -m unittest discover -s tests
node --test extension/tests/*.test.js
```

## Pilot metrics and launch

* Every tracking point and metric formula: [`pilot-metrics.md`](pilot-metrics.md).
  Report: `python -m shopping_agent metrics`, or `GET /metrics?format=md` with `ADMIN_KEY`.
* Deploying on Render and inviting friends: [`pilot-guide.md`](pilot-guide.md).

## Not built yet

* A Claude fallback in the live checks for pages without structured data.
* The "why not bought yet" guess (F11).
* Chrome Web Store listing.
