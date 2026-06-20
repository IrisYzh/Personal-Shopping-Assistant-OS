Send a shopping alert email directly to your_email@gmail.com using send_email.py (Gmail SMTP).

Parse $ARGUMENTS for: item name, change type (restock/price_drop/both), current price, target price, and product URL.

## Build the email content

### Subject line
- Restock only: `🛍️ Back in Stock: [item name]`
- Price drop only: `💰 Price Drop: [item name] is now $[current_price]`
- Both: `🎉 Back in Stock + Price Drop: [item name] — now $[current_price]`

### Body
```
Hi there,

Good news about an item on your shopping wishlist!

Item: [name] ([color, size if available])
Change: [Restock / Price dropped to $X / Both]
Current Price: $[current_price]
Your Target Price: $[target_price]
Link: [url]

[If restock]: This item is now available. Grab it before it sells out again!
[If price_drop]: The price has dropped to your target. Now is a great time to buy!
[If stock unknown]: Note: stock availability could not be confirmed automatically — check the size selector on the page.

— Your Shopping Assistant
```

## Send via script

Run this Bash command to send the email:

```bash
python3 send_email.py \
  --to "your_email@gmail.com" \
  --subject "[SUBJECT]" \
  --body "[BODY]"
```

Replace `[SUBJECT]` and `[BODY]` with the constructed content above.
Escape any double quotes in the body with backslash if needed.

## Success / failure
- If the script prints `Sent: ...` → confirm to the user that the email was sent
- If it prints `ERROR:` → report the error and suggest checking `.env` credentials
