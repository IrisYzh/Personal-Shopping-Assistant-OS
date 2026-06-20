# 🛍️ Personal Shopping Assistant OS

An **agentic OS** for [Claude Code](https://docs.claude.com/en/docs/claude-code) that automatically
watches a wishlist of fashion & beauty items across **Lululemon, SKIMS, Nordstrom, and Sephora**,
detects two kinds of meaningful change — **a restock** or **a price drop to your target** — and
sends you an **immediate Gmail alert**. A weekly digest lands every Monday morning.

The problem it solves: popular items sell out and restock unpredictably, and sale prices are
fleeting. Refreshing product pages by hand every day is tedious and easy to forget. This OS turns
that into a zero-maintenance loop — **update one spreadsheet, and the agent handles the rest.**

> This is a **template**. It ships with an example persona and placeholders for every personal
> value (your email, your Google Sheets). Follow [Setup](#-setup) to make it yours.

---

## ✨ How it works

The agent wires three external systems into one automated loop:

```
                ┌──────────────────────────────────────────────┐
                │   Google Drive (2 Google Sheets)             │
                │   • shopping_wishlist  ← you edit this        │
                │   • price_history      ← agent appends        │
                └───────────────┬──────────────────────────────┘
                                │ read / write
                                ▼
   CronCreate ───▶  /check-stock  ──▶ WebFetch each product URL
   (daily 9:03)         │                └─ Claude extracts price + stock
                        │                    from JSON-LD / HTML
                        │                    └─ WebSearch fallback if blocked
                        │
                        ├─ compare with last known price / stock
                        ├─ update wishlist + append to price_history
                        │
                        └─ on meaningful change ──▶ /notify-email
                                                       └─ send_email.py (Gmail SMTP)
                                                           └─ alert in your inbox
```

An **identity layer** keeps the agent disciplined:
- `context/SOUL.md` — voice & principles (direct, no fabrication, no notification spam)
- `context/USER.md` — your preferences and hard limits
- `context/learnings.md` — a running retrospective (wins / misses / lessons) the agent appends to

---

## 🧩 Skills

| Command | Purpose |
|---------|---------|
| `/manage-wishlist` | Add, remove, list, or update items in the Google Drive wishlist |
| `/check-stock` | **Core chain** — read Drive → check each URL → update Drive → send alerts |
| `/notify-email` | Send a formatted Gmail alert for a single item change |
| `/compare-price` | Read price history and show trends, lowest price, and a buy recommendation |
| `/summarize-weekly` | Build a weekly digest from history and email it |

### How skills chain

**Daily core chain** (`/check-stock` → `/notify-email`), runs automatically every morning:

```
/check-stock
  ├─ Drive: read shopping_wishlist
  ├─ WebFetch each URL → Claude extracts price + stock (WebSearch fallback)
  ├─ compare with stored last_price / in_stock
  ├─ Drive: update wishlist + append to price_history
  └─ if change → /notify-email → send_email.py → inbox
```

**Weekly digest** (`/summarize-weekly` → `/notify-email`), every Monday:

```
/summarize-weekly
  ├─ Drive: read price_history (last 7 days) + current wishlist
  ├─ build summary (changed / unchanged / never checked)
  └─ /notify-email → send_email.py → inbox
```

---

## 📁 Project structure

```
Personal Shopping Assistant OS/
├── CLAUDE.md                  # Agent config: Drive IDs, context matrix, hard limits
├── README.md                  # This file
├── send_email.py              # Gmail SMTP sender (called by /notify-email)
├── .env.example               # Credential template (copy to .env)
├── .gitignore                 # Ignores .env, outputs, OS junk
├── context/                   # Agent identity layer
│   ├── SOUL.md                #   voice & principles
│   ├── USER.md                #   ← EDIT: your name, email, retailers, limits
│   ├── learnings.md           #   per-skill retrospectives
│   └── memory/                #   daily session logs (append-only)
├── projects/                  # Skill output storage
│   ├── check-stock/
│   ├── compare-price/
│   └── summarize-weekly/
├── crons/jobs/                # Scheduled-job definitions
│   ├── daily-check.md
│   └── weekly-summary.md
└── .claude/commands/          # The five skills
    ├── manage-wishlist.md
    ├── check-stock.md
    ├── notify-email.md
    ├── compare-price.md
    └── summarize-weekly.md
```

---

## 🚀 Setup

### Prerequisites
- [Claude Code](https://docs.claude.com/en/docs/claude-code) (CLI or VS Code extension)
- Python 3 (standard library only — no `pip install` needed)
- A Google account with **2-Step Verification** enabled (required for a Gmail App Password)
- The **Google Drive connector** available in your Claude Code session (for reading/writing the Sheets)

### Step 1 — Get the project

```bash
git clone <your-fork-url>
cd "Personal Shopping Assistant OS"
```

Open the folder in Claude Code.

### Step 2 — Configure Gmail (App Password)

Alerts are sent over Gmail SMTP, which needs a 16-character **App Password** (not your normal password).

1. Enable 2-Step Verification: <https://myaccount.google.com/security>
2. Create an App Password: <https://myaccount.google.com/apppasswords>
   - App name: `Shopping Assistant`
   - Copy the 16-char password (format `xxxx xxxx xxxx xxxx`)
3. Create your `.env` from the template:
   ```bash
   cp .env.example .env
   ```
4. Edit `.env`:
   ```
   GMAIL_USER=your_email@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```
   > `.env` is gitignored — your credentials are never committed.
5. Test it:
   ```bash
   python3 send_email.py --to "your_email@gmail.com" --subject "Test" --body "It works!"
   ```
   You should see `Sent: Test → your_email@gmail.com` and an email in your inbox.

### Step 3 — Create your two Google Sheets

Create two Google Sheets in your Drive:

**1. `shopping_wishlist`** — header row:
```
name, url, target_price, notify_restock, notify_price_drop, last_price, in_stock, last_checked
```
Fill in `name`, `url`, `target_price`, and the two `notify_*` flags (`TRUE`/`FALSE`).
Leave `last_price`, `in_stock`, `last_checked` empty — the agent fills those in.

**2. `price_history`** — header row:
```
date, name, price, in_stock, change_type, notes
```
Leave it empty — the agent only appends.

Grab each Sheet's **file ID** from its URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_LONG_ID`**`/edit`

### Step 4 — Wire your IDs and email into the config

Replace the placeholders with your real values. The fastest way (run from the project root):

```bash
# macOS
sed -i '' \
  -e 's/YOUR_WISHLIST_SHEET_ID/<your wishlist id>/g' \
  -e 's/YOUR_PRICE_HISTORY_SHEET_ID/<your price history id>/g' \
  -e 's/your_email@gmail.com/<your email>/g' \
  CLAUDE.md .claude/commands/*.md crons/jobs/*.md
```
(On Linux, use `sed -i` without the `''`.)

Then personalize **`context/USER.md`** — your name, notification email, retailers, and hard limits.

### Step 5 — Run a manual check

In Claude Code, approve the Google Drive connector when prompted, then run:

```
/check-stock
```

The agent reads your wishlist, checks each URL, updates Drive, and emails you on any restock or
price drop. Try the others too: `/manage-wishlist list`, `/compare-price`, `/summarize-weekly`.

### Step 6 — Schedule it (optional)

The cron definitions live in `crons/jobs/`:
- `daily-check.md` — `/check-stock` every day at **9:03 AM**
- `weekly-summary.md` — `/summarize-weekly` every Monday at **9:07 AM**

> Scheduled jobs are **session-scoped** in Claude Code — they run while a session is active.
> Re-run `/check-stock` manually or restart the session to re-arm the schedule.

---

## 🔒 Safety & hard limits

The agent is constrained (in `CLAUDE.md` and `context/USER.md`) to **never**:
- purchase anything on your behalf
- send alerts to any address other than your configured email
- fabricate a price or stock status (it marks `unknown` if extraction fails)
- delete rows from `price_history` (append-only)
- send duplicate alerts for an unchanged state

---

## 📝 Notes & limitations

- **Best-effort parsing.** JS-heavy retailer pages sometimes block scraping; the agent falls back
  to web search, and marks `unknown` rather than guessing. Lululemon / SKIMS / Nordstrom / Sephora
  are the tuned targets — other retailers work on a best-effort basis.
- **Your data stays yours.** The wishlist and history live in *your* Google Drive; credentials live
  in *your* local `.env`. Nothing in this repo contains personal data.

---

## 📄 License

MIT — see `LICENSE`. Use it, fork it, make it your own.
