# Behavior-first Intent Recognition

## Product decision

The default capture path no longer asks the user to express shopping intent in a sentence. A future
Chrome content script records auditable product-level behavior. Counting stays deterministic and
local, so a page view does not spend LLM tokens or expose browsing history to a model.

The existing text classifier remains available for direct commands such as a target price, a stock
request, or a user correction.

## Seven-day intent window

Events are grouped by canonical product URL in a rolling seven-day window.

| Behavior state | Intent | Action |
|---|---|---|
| `add_to_cart` is active | explicit purchase | Add to monitoring automatically |
| `add_to_wishlist` is active | explicit purchase | Add to monitoring automatically |
| At least 3 views across at least 2 sessions, with no active strong action | fuzzy interest | Defer one question to the daily digest |
| Fewer views or refreshes in only one session | unknown | Record silently |
| Cart or wishlist action followed by removal | no longer a strong signal | Re-evaluate future intent; do not silently delete an existing monitor |

The cross-session condition prevents several refreshes in one visit from being mistaken for growing
interest. The thresholds are explicit configuration values in `BehaviorIntentAgent`, so later product
experiments can tune them without changing storage or reminder logic.

## Default monitoring for explicit intent

An active cart or wishlist action creates an `explicit_purchase` decision. A resolved product then
enters monitoring with these defaults:

- check every 6 hours because stock monitoring is enabled;
- alert when price first crosses 10% below the captured baseline unless the user provides a target;
- alert on an out-of-stock to in-stock transition;
- alert when a new promotion string differs from the last accepted promotion;
- deduplicate repeated observations and repeated daily questions.

The product page still uses JSON-LD first, then product meta, then limited visible text. Low-confidence
model observations cannot overwrite an accepted state.

## Daily fuzzy-interest digest

Repeated browsing does not silently add a product to the monitoring list. `daily-digest` creates at
most one pending question per product per calendar day:

> You viewed this product 3 times in the last 7 days. Do you currently want to buy it? If yes, at what
> price should we remind you? You can also choose not to follow it.

Approval converts the fuzzy interest into explicit purchase intent. The user can provide a target
price during confirmation. Declining deletes the pending confirmation and never creates monitoring.

## Local demo

Record three views in two sessions:

```bash
python3 -m shopping_agent --state data/behavior-demo.json behavior view \
  --product "复古跑鞋" --url "https://shop.example.com/retro-runner" \
  --session-id session-a --occurred-at 2026-09-01T10:00:00+00:00

python3 -m shopping_agent --state data/behavior-demo.json behavior view \
  --product "复古跑鞋" --url "https://shop.example.com/retro-runner" \
  --session-id session-a --occurred-at 2026-09-01T11:00:00+00:00

python3 -m shopping_agent --state data/behavior-demo.json behavior view \
  --product "复古跑鞋" --url "https://shop.example.com/retro-runner" \
  --session-id session-b --occurred-at 2026-09-02T10:00:00+00:00

python3 -m shopping_agent --state data/behavior-demo.json daily-digest \
  --at 2026-09-02T20:00:00+00:00
```

Record a strong action:

```bash
python3 -m shopping_agent --state data/behavior-demo.json behavior add_to_cart \
  --product "复古跑鞋" --url "https://shop.example.com/retro-runner" \
  --price 629 --stock in_stock --session-id session-b
```

## Integration boundary

The Chrome layer only needs to send this payload to a trusted backend or local extension runtime:

```json
{
  "event": "view",
  "session_id": "browser-session-id",
  "occurred_at": "ISO-8601 timestamp",
  "product": {
    "name": "resolved name",
    "url": "canonical product URL",
    "current_price": 629,
    "availability": "in_stock",
    "confidence": 0.95
  }
}
```

Raw browsing events should have a short retention period and should not be sent to an LLM one by one.
Only aggregated counts and specific uncertain cases need model reasoning.
