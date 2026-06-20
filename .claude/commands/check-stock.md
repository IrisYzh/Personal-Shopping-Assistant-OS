Run the full shopping monitoring chain. This is the core skill of the Personal Shopping Assistant OS.

## Chain

### Step 1 — Read Wishlist from Google Drive
Use `read_file_content` with file ID `YOUR_WISHLIST_SHEET_ID`.
Parse the CSV into a list of items.

### Step 2 — Check Each Item
For each item in the wishlist:

**a. Fetch the product page**
Use WebFetch on the item's URL.

**b. Extract price and stock status**
From the fetched content, look for:
- JSON-LD blocks (`<script type="application/ld+json">`) — highest priority
- Keywords: "Add to Cart", "Out of Stock", "Sold Out", "Back in Stock"
- Price patterns: `$XX`, `$XX.XX`

If WebFetch returns insufficient content (blocked, JS-only), fall back to WebSearch:
`site:[retailer domain] "[item name]" price in stock`

Record:
- `current_price`: numeric value found, or `null` if not found
- `current_in_stock`: `true`, `false`, or `unknown`

**c. Detect changes**
Compare with stored `last_price` and `in_stock`:
- **Restock**: `in_stock` was `false`/`unknown`, now `true` AND `notify_restock = true`
- **Price drop**: `current_price` ≤ `target_price` AND price changed AND `notify_price_drop = true`

### Step 3 — Update Drive Files

**Update wishlist** (file ID `YOUR_WISHLIST_SHEET_ID`):
Set `last_price`, `in_stock`, `last_checked` (today's date) for each item checked.

**Append to price history** (file ID `YOUR_PRICE_HISTORY_SHEET_ID`):
Add a row for every item checked:
`date, name, price, in_stock, change_type, notes`
- `change_type`: `restock`, `price_drop`, `both`, or `no_change`

### Step 4 — Send Gmail Alerts
For each item with a meaningful change, invoke the `/notify-email` skill with the item details.

### Step 5 — Print Summary
Show a summary table of results:
| Item | Old Price | New Price | In Stock | Change | Alert Sent |
