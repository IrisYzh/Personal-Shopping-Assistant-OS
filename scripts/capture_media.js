#!/usr/bin/env node
// Record real screenshots and screen videos of the extension for the portfolio.
//
//   cd "Personal Shopping Assistant OS"
//   npm install --no-save playwright && npx playwright install chromium
//   node scripts/capture_media.js            # writes portfolio-media/raw/
//
// What is real: the extension code in ./extension, the backend code in ./shopping_agent, and the
// shop pages (opened live). What is local: the backend runs on this computer with a throwaway
// data folder and a demo user, so nothing here touches the pilot backend or anyone's data.
// MOCK=1 swaps the shop pages for small local copies, for testing this script without the internet.

const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const EXT = path.join(ROOT, "extension");
const OUT = path.resolve(process.env.OUT || path.join(ROOT, "portfolio-media", "raw"));
const PORT = Number(process.env.PORT || 8787); // The unpacked extension talks to 127.0.0.1:8787 by default.
const MOCK = process.env.MOCK === "1";
const HEADED = process.env.HEADED === "1";
const KEY = "demo-" + Math.random().toString(36).slice(2, 14);
const W = 1440;
const H = 900;

const ALIGN = "https://shop.lululemon.com/p/align-pant-2/i4goprzy85";
const SEARCHES = [
  "https://shop.lululemon.com/search?Ntt=define%20jacket",
  "https://shop.lululemon.com/search?Ntt=scuba%20oversized%20hoodie",
];
const SKIMS_SEARCH = "https://skims.com/search?q=soft%20lounge%20dress";
const NORDSTROM = "https://www.nordstrom.com/s/skims-soft-lounge-long-slipdress-regular-plus/5897391";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
fs.mkdirSync(OUT, { recursive: true });
async function shot(page, name, opts = {}) {
  // Stills look cleaner without the demo cursor; the videos keep it.
  const hide = (on) => page.evaluate((v) => { const c = document.getElementById("__demo_cursor"); if (c) c.style.visibility = v ? "hidden" : ""; }, on).catch(() => {});
  await hide(true);
  await page.screenshot({ path: path.join(OUT, name), ...opts });
  await hide(false);
  log("saved", name);
}

// ----- backend: the real server code, local and throwaway -----
function startBackend(dataDir) {
  const child = spawn("python3", ["-m", "shopping_agent", "serve", "--host", "127.0.0.1", "--port", String(PORT), "--data-dir", dataDir], {
    cwd: ROOT,
    env: { ...process.env, PILOT_KEYS: `demo:${KEY}`, SHEETS_WEBHOOK_URL: "", GMAIL_USER: "", GMAIL_APP_PASSWORD: "" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stderr.on("data", (d) => process.env.DEBUG && process.stderr.write(d));
  child.stdout.on("data", (d) => process.env.DEBUG && process.stdout.write(d));
  return child;
}

async function waitForBackend() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/health`);
      if (r.ok) return;
    } catch (_) {}
    await sleep(200);
  }
  throw new Error(`Backend did not start on port ${PORT}. Is something else using it?`);
}

// ----- a visible cursor, because headless video has none -----
const CURSOR = `(() => {
  if (window.top !== window || location.protocol === "chrome-extension:") return;
  const add = () => {
    if (document.getElementById("__demo_cursor")) return;
    const c = document.createElement("div");
    c.id = "__demo_cursor";
    c.style.cssText = "position:fixed;left:-40px;top:-40px;width:22px;height:22px;z-index:2147483647;pointer-events:none;transition:transform .08s";
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 2 L3 18 L7.5 13.8 L10.6 20.4 L13.4 19.1 L10.4 12.6 L16.6 12.4 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(c);
    addEventListener("mousemove", (e) => { c.style.left = e.clientX - 3 + "px"; c.style.top = e.clientY - 2 + "px"; }, true);
    addEventListener("mousedown", () => (c.style.transform = "scale(.85)"), true);
    addEventListener("mouseup", () => (c.style.transform = ""), true);
  };
  if (document.documentElement) add(); else addEventListener("DOMContentLoaded", add);
})();`;

async function glide(page, from, to, steps = 22) {
  await page.mouse.move(from.x, from.y);
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    await page.mouse.move(from.x + (to.x - from.x) * e, from.y + (to.y - from.y) * e);
    await sleep(14);
  }
}

// Close sign-up modals and cookie banners. Picks the most private choice.
async function tidy(page) {
  for (const label of ["Reject All", "Decline", "Reject", "No thanks", "Close", "Close dialog", "close"]) {
    const button = page.locator(`[role="dialog"] button:has-text("${label}"), [role="dialog"] [aria-label="${label}"], #onetrust-banner-sdk button:has-text("${label}")`).first();
    if (await button.isVisible().catch(() => false)) await button.click({ timeout: 1500 }).catch(() => {});
  }
  await page.keyboard.press("Escape").catch(() => {});
}

async function firstProductFrom(context, searchUrl) {
  const page = await context.newPage();
  try {
    await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForSelector('a[href*="/p/"]', { timeout: 20000 });
    const href = await page.$$eval('a[href*="/p/"]', (as) => as.map((a) => a.href.split("?")[0]).find((h) => h.includes("/p/")));
    return href || null;
  } catch (error) {
    log("search failed", searchUrl, error.message.split("\n")[0]);
    return null;
  } finally {
    await page.close();
  }
}

async function openProduct(context, url, wait = 4000) {
  const page = await context.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForLoadState("load", { timeout: 30000 }).catch(() => {});
  await sleep(wait);
  await tidy(page);
  return page;
}

// ----- MOCK mode: stand-in pages so the script can be tested without the shops -----
function mockPage(name, price, stock) {
  const ld = JSON.stringify({ "@context": "https://schema.org", "@type": "Product", name, offers: { "@type": "Offer", price: String(price), priceCurrency: "USD", availability: `https://schema.org/${stock}` } });
  return `<!doctype html><html><head><title>${name}</title><script type="application/ld+json">${ld}</script>
  <style>body{font-family:Helvetica,Arial,sans-serif;margin:0;background:#fafafa}header{height:64px;border-bottom:1px solid #e5e5e5;background:#fff;display:flex;align-items:center;padding:0 60px;font-weight:700}
  .wrap{display:flex;gap:40px;padding:40px 60px}.img{width:620px;height:720px;background:linear-gradient(160deg,#2b2b2b,#666)}h1{font-size:28px}</style></head>
  <body><header>MOCK STORE</header><div class="wrap"><div class="img"></div><div><h1>${name}</h1><p>$${price} USD</p><button aria-label="Add to Wishlist" style="font-size:22px">&#9825;</button><a href="/p/mock-jacket/1">x</a><a href="/products/mock-dress">y</a></div></div></body></html>`;
}

async function main() {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "psa-demo-data-"));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "psa-demo-profile-"));
  const videoDir = fs.mkdtempSync(path.join(os.tmpdir(), "psa-demo-video-")); // Setup-page videos stay out of OUT.
  const backend = startBackend(dataDir);
  let context;
  try {
    await waitForBackend();
    log("backend ready on", PORT);
    context = await chromium.launchPersistentContext(profile, {
      headless: !HEADED,
      channel: "chromium",
      args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, ...(HEADED ? [] : ["--headless=new"])],
      viewport: { width: W, height: H },
      deviceScaleFactor: 2,
      locale: "en-US",
      timezoneId: "America/Los_Angeles",
      recordVideo: { dir: videoDir, size: { width: W, height: H } },
    });
    await context.addInitScript(CURSOR);
    if (MOCK) {
      await context.route("https://shop.lululemon.com/**", (route) => {
        const url = route.request().url();
        if (url.includes("align")) return route.fulfill({ contentType: "text/html; charset=utf-8", body: mockPage('lululemon Align™ High-Rise Pant 25"', 59, "InStock") });
        return route.fulfill({ contentType: "text/html; charset=utf-8", body: mockPage("Define Jacket Nulu", 128, "InStock") });
      });
      await context.route("https://skims.com/**", (route) => route.fulfill({ contentType: "text/html; charset=utf-8", body: mockPage("Soft Lounge Long Slip Dress", 78, "InStock") }));
      await context.route("https://www.nordstrom.com/**", (route) => route.fulfill({ contentType: "text/html; charset=utf-8", body: mockPage("SKIMS Soft Lounge Long Slipdress", 88, "InStock") }));
    }

    let [worker] = context.serviceWorkers();
    if (!worker) worker = await context.waitForEvent("serviceworker", { timeout: 20000 });
    await worker.evaluate(
      ([key, port]) => chrome.storage.sync.set({ settings: { apiBase: `http://127.0.0.1:${port}`, pilotKey: key, sites: { Lululemon: true, SKIMS: true, Nordstrom: true, Sephora: true } } }),
      [KEY, PORT]
    );
    const popupUrl = worker.url().replace("background.js", "popup.html");
    log("extension ready");

    // ---------- 1. The card: two looks, a break, then the third look ----------
    for (let i = 0; i < 2; i++) {
      const page = await openProduct(context, ALIGN, 9500);
      await page.close();
      log("view", i + 1, "counted");
    }
    await worker.evaluate(() => chrome.storage.local.remove("session")); // Stands in for coming back after a break.

    const product = await openProduct(context, ALIGN, 1500);
    await shot(product, "01-product-page.png");
    await glide(product, { x: 700, y: 450 }, { x: 980, y: 300 });
    await product.mouse.wheel(0, 250);
    await sleep(900);
    await product.mouse.wheel(0, -250);
    let card = null;
    for (let i = 0; i < 30 && !card; i++) {
      card = await product.$("#shopping-assistant-card");
      if (!card) await sleep(500);
    }
    if (!card) throw new Error("The card did not appear on the third view.");
    await sleep(900);
    await shot(product, "02-card.png");
    const box = await card.boundingBox();
    await shot(product, "02-card-closeup.png", { clip: { x: box.x - 24, y: box.y - 24, width: box.width + 48, height: box.height + 48 } });
    const size = { x: box.x + 16 + 60, y: box.y + box.height - 16 - 38 - 12 - 18 };
    await glide(product, { x: 980, y: 300 }, size, 30);
    await product.mouse.click(size.x, size.y);
    await product.keyboard.type("6", { delay: 160 });
    const color = { x: box.x + box.width - 16 - 60, y: size.y };
    await glide(product, size, color, 14);
    await product.mouse.click(color.x, color.y);
    await product.keyboard.type("Graphite Grey", { delay: 70 });
    await sleep(500);
    await shot(product, "03-card-filled.png", { clip: { x: box.x - 24, y: box.y - 24, width: box.width + 48, height: box.height + 48 } });
    const add = { x: box.x + box.width - 16 - 65, y: box.y + box.height - 16 - 19 };
    await glide(product, color, add, 18);
    await sleep(250);
    await product.mouse.click(add.x, add.y);
    await sleep(700);
    const saved = await product.$("#shopping-assistant-card");
    const savedBox = saved ? await saved.boundingBox() : box;
    await shot(product, "04-card-saved.png", { clip: { x: savedBox.x - 24, y: savedBox.y - 24, width: savedBox.width + 48, height: savedBox.height + 48 } });
    await sleep(2600);
    await shot(product, "05-after-saved.png");
    const cardVideo = product.video();
    await product.close();
    if (cardVideo) fs.copyFileSync(await cardVideo.path(), path.join(OUT, "video-card-flow.webm"));
    log("card flow done");

    // ---------- 2. "Save this item" in the popup, on other real products ----------
    const others = [];
    for (const search of SEARCHES) {
      const url = MOCK ? "https://shop.lululemon.com/p/mock-jacket/1" + others.length : await firstProductFrom(context, search);
      if (url) others.push(url);
    }

    async function popupFor(page) {
      await page.bringToFront();
      const tabId = await worker.evaluate(async () => {
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        return tab && tab.id;
      });
      const popup = await context.newPage();
      await popup.setViewportSize({ width: 360, height: 600 });
      // A real popup reads the tab under it. Opened as a page, it would read itself, so point it at the shop tab.
      await popup.addInitScript((id) => {
        const query = chrome.tabs.query.bind(chrome.tabs);
        chrome.tabs.query = (info) => (info && info.active ? Promise.resolve([{ id }]) : query(info));
      }, tabId);
      await popup.goto(popupUrl);
      await sleep(1200);
      return popup;
    }

    const sizes = [["M", "Black"], ["L", "Heathered Grey"], ["S", "Onyx"]];
    for (let i = 0; i < others.length; i++) {
      const page = await openProduct(context, others[i], 4000).catch((e) => (log("open failed", others[i], e.message), null));
      if (!page) continue;
      const last = i === others.length - 1;
      if (i === 0) await shot(page, "06-second-product.png");
      const popup = await popupFor(page);
      const visible = await popup.isVisible("#current");
      if (!visible) {
        log("no product read on", others[i]);
        await popup.close();
        await page.close();
        continue;
      }
      const tag = String(i + 1);
      await shot(popup, `07-popup-on-this-page-${tag}.png`);
      await popup.fill("#currentSize", sizes[i % sizes.length][0]);
      await popup.fill("#currentColor", sizes[i % sizes.length][1]);
      await shot(popup, `08-popup-filled-${tag}.png`);
      await popup.click("#saveCurrent");
      await sleep(900);
      await shot(popup, `09-popup-saved-${tag}.png`);
      if (last) await shot(page, "10-product-with-popup-base.png");
      await popup.close();
      await page.close();
    }

    // ---------- 3. The site's own heart: SKIMS and Nordstrom ----------
    const WISH = /(add to (wish ?list|favorites|loves)|\bwish ?list\b|\bfavorite\b|\blove\b|save for later|^save(,|$))/i;
    const NOT = /\b(remove|delete|unlove|unfavorite|unsave)\b/i;
    const skims = MOCK ? "https://skims.com/products/mock-dress" : await (async () => {
      const page = await context.newPage();
      try {
        await page.goto(SKIMS_SEARCH, { waitUntil: "domcontentloaded", timeout: 45000 });
        await page.waitForSelector('a[href*="/products/"]', { timeout: 20000 });
        return await page.$$eval('a[href*="/products/"]', (as) => as.map((a) => a.href.split("?")[0])[0]);
      } catch (e) {
        log("SKIMS search failed", e.message.split("\n")[0]);
        return null;
      } finally {
        await page.close();
      }
    })();
    for (const [name, url] of [["skims", skims], ["nordstrom", NORDSTROM]]) {
      if (!url) continue;
      const page = await openProduct(context, url, 4000).catch((e) => (log("open failed", url, e.message), null));
      if (!page) continue;
      // Find the shop's own wishlist button, the same way the extension recognizes it.
      const target = await page.evaluate(([w, n]) => {
        const wish = new RegExp(w, "i");
        const not = new RegExp(n, "i");
        for (const el of document.querySelectorAll('button, a, [role="button"]')) {
          const label = (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").replace(/\s+/g, " ").trim();
          if (!label || label.length > 80 || not.test(label) || !wish.test(label)) continue;
          const r = el.getBoundingClientRect();
          if (r.width < 8 || r.height < 8) continue;
          el.scrollIntoView({ block: "center" });
          const b = el.getBoundingClientRect();
          return { x: b.x + b.width / 2, y: b.y + b.height / 2, label };
        }
        return null;
      }, [WISH.source, NOT.source]);
      if (!target) {
        log("no wishlist button found on", name);
        await page.close();
        continue;
      }
      log(name, "wishlist button:", target.label);
      await sleep(800);
      await shot(page, `15-${name}-product.png`);
      await glide(page, { x: W / 2, y: H / 2 }, target, 28);
      await sleep(300);
      await page.mouse.click(target.x, target.y);
      await sleep(2500);
      await tidy(page);
      const popup = await popupFor(page);
      await shot(popup, `16-${name}-popup-after-heart.png`);
      await popup.close();
      const video = page.video();
      await page.close();
      if (video) fs.copyFileSync(await video.path(), path.join(OUT, `video-${name}-heart.webm`));
    }

    // ---------- 4. Wishlist, Bought and Undo, Settings ----------
    const blank = await context.newPage();
    const popup = await popupFor(blank);
    await shot(popup, "11-popup-wishlist.png", { fullPage: true });
    const boughtButtons = popup.locator("#items li button:has-text('Bought')");
    const count = await boughtButtons.count();
    if (count) {
      await boughtButtons.nth(count - 1).click();
      await sleep(400);
      await shot(popup, "12-popup-bought-undo.png", { fullPage: true });
      await sleep(4600);
      await popup.click("#boughtBox summary").catch(() => {});
      await sleep(400);
      await shot(popup, "13-popup-bought-section.png", { fullPage: true });
    }
    await popup.click('button[data-view="settings"]');
    await sleep(500);
    await shot(popup, "14-popup-settings.png", { fullPage: true });
    await popup.close();
    await blank.close();
    log("popup done");
  } finally {
    if (context) await context.close().catch(() => {});
    backend.kill();
  }
  log("done, files in", OUT);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
