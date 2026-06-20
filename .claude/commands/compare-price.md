Analyze and display price history for wishlist items from Google Drive.

## Steps

### 1. Read price history
Use `read_file_content` with file ID `YOUR_PRICE_HISTORY_SHEET_ID`.
Parse the CSV (columns: date, name, price, in_stock, change_type, notes).

### 2. Parse $ARGUMENTS
- If an item name is provided, filter to that item only
- If no argument, show all items

### 3. Display results

For each item, show:

**Price Timeline**
```
[Item Name]
Target price: $XX
---------------------------------------------
Date        Price     In Stock   Change
2026-05-18  $98.00    ✓          price_drop
2026-05-17  $118.00   ✓          no_change
...
```

**Summary stats**
- Lowest price seen: $XX (on DATE)
- Current price: $XX
- Distance from target: +$XX above / AT TARGET / $XX below

### 4. Recommendation
If current price is at or below target: "This is a good time to buy."
If price is trending down: "Price has dropped X% over the last N checks."
If no data yet: "No price history yet. Run /check-stock to start tracking."
