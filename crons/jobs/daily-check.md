---
name: daily-check
schedule: "3 9 * * *"
description: Daily stock and price check for all wishlist items.
---

Run /check-stock to check all items in the shopping wishlist on Google Drive.

For each item:
1. Fetch the product page via WebFetch
2. Extract current price and stock status (JSON-LD first, then HTML keywords, then WebSearch fallback)
3. Compare with last known values in wishlist
4. Update Google Drive files (wishlist + price_history)
5. Send Gmail alerts to your_email@gmail.com for any restocks or price drops

Save results to projects/check-stock/{YYYY-MM-DD}_daily-check.md.
Append any wins/misses to context/learnings.md under /check-stock.
