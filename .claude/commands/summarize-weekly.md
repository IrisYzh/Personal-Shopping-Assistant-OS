Generate a weekly shopping watchlist summary and send it to your_email@gmail.com via Gmail.

## Chain

### Step 1 — Read price history (last 7 days)
Use `read_file_content` with file ID `YOUR_PRICE_HISTORY_SHEET_ID`.
Filter rows where `date` is within the past 7 days from today.

### Step 2 — Read current wishlist
Use `read_file_content` with file ID `YOUR_WISHLIST_SHEET_ID`.
Get current state of all items.

### Step 3 — Build summary

Format:
```
Weekly Shopping Watchlist Summary — Week of [DATE]

ITEMS WITH CHANGES THIS WEEK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[name] — [change_type] on [date]
  Price: $[old] → $[new] | Target: $[target] | [URL]

NO CHANGES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[name] — Last checked [date], $[price], [in stock / out of stock]

ITEMS NOT YET CHECKED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[name] — never checked (run /check-stock to start tracking)
```

### Step 4 — Send via Gmail
Use `mcp__claude_ai_Gmail__create_draft` (or send directly if available):
- To: your_email@gmail.com
- Subject: `🛒 Weekly Shopping Watchlist — [YYYY-MM-DD]`
- Body: the summary from Step 3

Confirm with draft ID after sending.
