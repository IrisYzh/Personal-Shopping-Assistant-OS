---
name: weekly-summary
schedule: "7 9 * * 1"
description: Monday morning weekly shopping watchlist digest.
---

Run /summarize-weekly to generate and send the weekly shopping watchlist summary.

Steps:
1. Read price_history from Google Drive (last 7 days)
2. Read current wishlist from Google Drive
3. Build summary: items with changes / no changes / never checked
4. Send digest email to your_email@gmail.com
   Subject: 🛒 Weekly Shopping Watchlist — {YYYY-MM-DD}

Save output to projects/summarize-weekly/{YYYY-MM-DD}_weekly-summary.md.
