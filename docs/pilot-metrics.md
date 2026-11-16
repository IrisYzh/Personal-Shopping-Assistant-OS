# Pilot metrics: what is tracked and how each number is defined

Every tracking point writes one line to `data/users/analytics.jsonl` on the server.
Get the report any time:

```bash
# On the server, or on a copy of the file
python -m shopping_agent metrics                 # Markdown report
python -m shopping_agent metrics --since 2026-11-20
python -m shopping_agent metrics --json          # raw numbers

# From your browser, once deployed (ADMIN_KEY is in the Render dashboard)
https://<your-app>.onrender.com/metrics?format=md&key=<ADMIN_KEY>
```

## The funnel

```
product page counted  ->  card shown  ->  Add / No thanks / closed / no answer
                                     \->  saved by cart or wishlist click
watched item  ->  change found  ->  email sent  ->  email link clicked  ->  marked Bought
```

## Tracking points

Logged by the backend. The extension cannot fake these.

| Event | When | Main fields |
| --- | --- | --- |
| `product_view_counted` | A product page stayed visible for 8 seconds | site, product_url, session_id, watched |
| `cart_or_wishlist_click` | The person clicked Add to bag or a wishlist button | kind, site |
| `prompt_shown` | The backend decided to show the card | pending_id, view_count |
| `prompt_deferred` | A card was due, but one was already shown today | site |
| `prompt_accepted` | Add to wishlist on the card | size_filled, color_filled |
| `prompt_declined` | No thanks on the card | |
| `item_auto_saved` | A cart or wishlist click saved the item | kind |
| `item_edited` | Size or color changed in the popup | size_set, color_set |
| `item_removed` | Remove in the popup | status, days_watched |
| `item_marked_bought` | Bought in the popup | days_watched, had_alert, had_email, price_below_baseline |
| `item_unmarked_bought` | Undo or Watch again | |
| `check_run` | One price and stock check | via (scheduled_check or page_visit), ok, size_level, error |
| `change_detected` | A check found a restock, price drop or sale | type, size_level |
| `email_sent` | An email went out | email_id, kind (instant_size_back, instant_price_drop, instant_sale_start, daily_digest, weekly_digest) |
| `email_failed` | Gmail refused an email | kind, error |
| `email_link_clicked` | A product link in an email was opened | email_id, kind, item_id |
| `settings_saved` | Settings saved | each email choice, has_email_address |
| `history_cleared` | Clear my browsing history | removed_events |

Sent by the extension through `POST /track` (only these names are accepted):

| Event | When |
| --- | --- |
| `extension_installed` | First install |
| `popup_opened` | The toolbar popup opened |
| `prompt_rendered` | The card really appeared on the page |
| `prompt_closed` | The card's × was pressed |
| `site_search` | A search in "Sites I watch" finished: result count, the site added, and the typed text only when nothing matched |
| `site_added`, `site_removed` | A site chip was added or removed |
| `wishlist_link_clicked` | A product name in the popup was clicked |

## Metric definitions

| Metric | Formula | Why it matters |
| --- | --- | --- |
| Prompts per 100 counted views | prompt_shown / product_view_counted × 100 | How often the card interrupts. Too high feels pushy. |
| Card render rate | prompt_rendered / prompt_shown | Checks that the card really shows on each site. |
| Prompt accept rate | prompt_accepted / prompt_shown | Is "3 views in 7 days" a good signal of interest? |
| Prompt decline rate | prompt_declined / prompt_shown | Wrong guesses. |
| Closed without answer | prompt_closed / prompt_shown | Mild annoyance. |
| No answer | shown minus accepted, declined and closed, over shown | People who left the page. |
| Size filled on accept | accepts with a size / prompt_accepted | Can size-level alerts work? |
| Email click rate | emails with at least one click / emails sent | Are the emails useful? Counted per email, not per click. |
| Bought per watched item | (marked bought minus undone) / (accepted + auto saved) | The closest thing to conversion. Self-reported. |
| Check success rate | check_run ok / all check_run | How often shops block or confuse the reader. |
| Prompts per person per active day | prompt_shown / person-days with any event | Guardrail: the rule is at most 1. |
| Emails per person | email_sent / people | Guardrail against noise. |

There is no email open rate on purpose. Open tracking needs a hidden image, which makes
emails look like marketing and pushes them toward Promotions or spam.

## Reading the numbers with 3 to 5 friends

Always write the count next to a rate: "2 of 3 cards accepted", not "67%". With this few
people, one person can move every rate. Pair the numbers with a short chat with each
friend at the end of the pilot, and ask about the moments behind them: the card they
closed, the email they did not open, the item they bought.

## Privacy

The log keeps product URLs, button presses and email events. It never stores page text,
prices you did not save, email bodies, or your friends' email addresses. Tell friends
about it before they install, and delete `analytics.jsonl` when the pilot ends.
