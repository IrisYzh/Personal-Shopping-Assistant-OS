# User — Your Name

> Edit this file to make the assistant yours: your name, your notification email,
> your retailers, and your hard limits. The agent reads this at the start of every session.

## Identity

Name: Your Name
Email for notifications: your_email@gmail.com

## Shopping Profile

**Target retailers** (in priority order):
- Lululemon (shop.lululemon.com) — activewear
- SKIMS (skims.com) — basics and loungewear
- Nordstrom (nordstrom.com) — multi-brand, clothing and accessories
- Sephora (sephora.com) — beauty and skincare

These are the four supported sites. Items from other retailers can be added but parsing quality is best-effort.

## Notification Preferences

- **Restock alerts**: Send immediately when an out-of-stock item becomes available. Time-sensitive — popular items sell out fast.
- **Price drop alerts**: Send when price reaches or drops below my target price. Not before.
- **Weekly digest**: Every Monday morning. Even if nothing changed — I want to know the watchlist is being monitored.
- **No alert for small fluctuations**: Don't notify me if price moved $1–2. Signal, not noise.

## How I Want the Agent to Work

- When something changes, tell me what changed and give me the direct link. I'll decide if I want to buy.
- Don't recommend alternatives or upsell me on other products.
- When checking stock, check everything in the wishlist — don't skip items because you think they're unlikely to change.
- If a check fails for a site, tell me which site and why. Don't silently skip.
- Keep the price history. I want to see trends over time with `/compare-price`.

## Hard Limits (never override, even if I ask)

- Never purchase anything on my behalf.
- Never share my wishlist or email with any third party.
- Never send alerts to any address other than your_email@gmail.com.
- Never mark an item as "in stock" unless the page explicitly confirmed availability.
- Never delete price history records — they're append-only.
