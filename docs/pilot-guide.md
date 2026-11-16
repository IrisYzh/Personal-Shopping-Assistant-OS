# Pilot launch guide

Everything in the code is done. These are the steps only you can do, because they need
your accounts. Plan about one hour. Costs: Render Starter about $7 a month while the pilot
runs. Gmail and GitHub are free.

## 1. Push the code to GitHub (5 minutes)

Render deploys from GitHub. This folder is already linked to
`github.com/IrisYzh/Personal-Shopping-Assistant-OS`, on the branch `codex/multiagent-mvp`.
The new pilot files are on your Mac but not committed yet. In Terminal:

```bash
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/Desktop/秋招/portfolio/Personal\ Shopping\ Assistant\ OS
git status                      # .env and data/users must NOT be listed
git add -A
git commit -m "Pilot build: tracking, size checks, email jobs, deploy files"
git push
```

If the repository is public, that is fine: no keys or passwords are in the code. They only
go into Render in step 4. In Render, pick the branch you pushed.

## 2. Make a Gmail sender (10 minutes)

Use a new Gmail address just for the pilot, like `iris.shopping.assistant@gmail.com`, so a
problem never touches your own inbox.

1. Create the Gmail account.
2. Turn on 2-Step Verification: myaccount.google.com > Security.
3. Create an App Password: myaccount.google.com/apppasswords. Name it "Shopping Assistant".
   Copy the 16 letters. This is `GMAIL_APP_PASSWORD`.

Gmail allows about 500 emails a day, far more than a 5-person pilot needs.

## 3. Make the pilot keys (2 minutes)

One key per friend, so each person has a private wishlist. Run this once per friend:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(16))"
```

Then write them as `name:key` pairs separated by commas, for example
`iris:Xy7...,amy:Q2b...,lena:Pp9...`. Names may use letters, numbers, `-` and `_`.
This whole line is `PILOT_KEYS`. Keep a copy in your notes, you will send each key to its owner.

## 4. Deploy on Render (15 minutes)

1. Sign up at render.com with your GitHub account.
2. **New > Blueprint**, pick the repository. Render reads `render.yaml` and sets up one web
   service with a 1 GB disk on the Starter plan.
3. Fill in the values it asks for:
   | Variable | Value |
   | --- | --- |
   | `PILOT_KEYS` | the line from step 3 |
   | `GMAIL_USER` | the pilot Gmail address |
   | `GMAIL_APP_PASSWORD` | the 16 letters from step 2 |
   | `PUBLIC_URL` | leave empty for now |
   | `SHEETS_WEBHOOK_URL` | optional, see `docs/browser-extension.md` |
   | `ANTHROPIC_API_KEY` | optional, not needed for the pilot |

   `TRACKING_SECRET` and `ADMIN_KEY` are generated for you.
4. When the deploy is green, copy the service URL (like `https://shopping-assistant-pilot.onrender.com`),
   put it in `PUBLIC_URL`, and save. Render redeploys by itself.
5. Check it: open `<URL>/health`. You should see `{"ok": true}`.
6. Open **Environment** in Render and copy `ADMIN_KEY`. Your report lives at
   `<URL>/metrics?format=md&key=<ADMIN_KEY>`. Bookmark it and do not share it.

## 5. Build the extension zip (2 minutes)

```bash
python3 scripts/package_extension.py --api-base https://shopping-assistant-pilot.onrender.com
```

This writes `dist/shopping-assistant-0.3.0.zip` with your backend already filled in.

## 6. Try it yourself first (15 minutes)

1. Unzip the file. In Chrome open `chrome://extensions`, turn on **Developer mode**, click
   **Load unpacked**, and pick the `shopping-assistant` folder.
2. Open the extension > Settings. Paste **your** key, type your email, keep the email boxes
   you want, and press **Save settings**. Then press **Send me a test email**.
3. In the test email, add the sender to your contacts.
4. Open one Lululemon product page three times on different visits (8 seconds or more each).
   The card should appear on the third visit. Press Add to wishlist with a size.
5. Open the report link from step 4. You should see 3 counted views, 1 prompt, 1 accept.

## 7. Invite friends

Send each friend, privately:

* the zip file
* their own key (never someone else's)
* this short note:

> Thanks for testing! Unzip the file, open chrome://extensions, turn on Developer mode,
> click Load unpacked and pick the folder. Then click the extension icon > Settings, paste
> your key, add your email, Save, and press "Send me a test email". Please add that sender
> to your contacts. After that just shop as usual on Lululemon, SKIMS, Nordstrom or Sephora.
> If you buy something you saved, press "Bought" in the extension. The extension only looks
> at product pages on those four sites, and counts simple usage like which buttons you press.

## 8. During the pilot

* Look at the report once or twice a week. Write counts next to rates.
* Price checks run every 6 hours. Daily summaries go out at 9 AM, weekly ones Saturday 9 AM,
  Los Angeles time. Change `PILOT_TIMEZONE` in Render if most friends live elsewhere.
* If the report shows a low "checks that worked" rate for one site, that site blocks
  servers. Visits by your friends still update prices for that site.
* Raw events for your own charts: `<URL>/metrics?format=jsonl&key=<ADMIN_KEY>`.

## 9. When the pilot ends

1. Save the Markdown report and the raw export.
2. In Render, suspend or delete the service. Deleting the disk deletes everyone's data.
3. Tell friends they can remove the extension from `chrome://extensions`.
4. Delete the App Password and, if you like, the pilot Gmail account.

## What is not in this pilot

* Claude is not used in the live checks yet. The page reader handles JSON-LD and meta tags.
  A Claude fallback for pages without structured data is the next step.
* Restock by size works only where the page lists sizes in its structured data. Otherwise
  the email says "Back in stock" without a size.
* Only the four sites in `shopping_agent/site_catalog.json`.
* The extension is not on the Chrome Web Store, so friends load it in Developer mode.
