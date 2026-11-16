// Runs on product pages of the four supported sites.
// It reads the product, waits until the user has really looked at it, sends one event,
// and shows the small "add to wishlist?" card when the backend says now is the moment.
(function () {
  const DWELL_MS = 8000; // A view counts only after 8 seconds on the page.
  const parser = globalThis.ShoppingParser;
  let currentUrl = null;
  let product = null;
  let visibleMs = 0;
  let lastTick = null;
  let viewSent = false;
  let timer = null;

  function readPage() {
    const jsonLdTexts = [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => s.textContent || "");
    const meta = {};
    document.querySelectorAll("meta[property], meta[name]").forEach((tag) => {
      const key = (tag.getAttribute("property") || tag.getAttribute("name") || "").toLowerCase();
      if (key && !(key in meta)) meta[key] = tag.getAttribute("content") || "";
    });
    return parser.parseProduct({ url: location.href, jsonLdTexts, meta });
  }

  function send(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          if (chrome.runtime.lastError) return resolve(null);
          resolve(response || null);
        });
      } catch (_) {
        resolve(null); // The extension was reloaded while the page stayed open.
      }
    });
  }

  async function report(eventType) {
    if (!product) return;
    const response = await send({ type: "event", eventType, product });
    if (response && response.ok && response.data && response.data.prompt_now && response.data.prompt) {
      showCard(response.data.prompt);
    }
  }

  // ----- dwell timer: count only time while the tab is visible -----
  function tick() {
    const now = Date.now();
    if (document.visibilityState === "visible" && lastTick !== null) visibleMs += now - lastTick;
    lastTick = now;
    if (!viewSent && visibleMs >= DWELL_MS) {
      viewSent = true;
      report("view");
    }
  }

  function startPage() {
    currentUrl = location.href;
    product = readPage();
    visibleMs = 0;
    lastTick = Date.now();
    viewSent = false;
    if (timer) clearInterval(timer);
    if (product) timer = setInterval(tick, 1000);
  }

  // Lululemon and SKIMS change pages without a full reload, so watch the URL.
  setInterval(() => {
    if (location.href !== currentUrl) {
      const before = product && product.url;
      const next = parser.canonicalUrl(location.href);
      if (next !== before) startPage();
      else currentUrl = location.href; // Only a color or size query changed.
    }
  }, 1000);

  // ----- add to cart and wishlist clicks -----
  document.addEventListener(
    "click",
    (event) => {
      if (!product) return;
      const target = event.target instanceof Element ? event.target.closest('button, a, [role="button"]') : null;
      if (!target || target.closest("#shopping-assistant-card")) return;
      const label = target.getAttribute("aria-label") || target.getAttribute("title") || target.textContent;
      const kind = parser.classifyClick(label);
      if (kind) {
        product = readPage() || product; // Read again in case the chosen size changed the price.
        report(kind);
      }
    },
    true
  );

  // ----- the in-page card, in a shadow root so site styles cannot break it -----
  function showCard(prompt) {
    if (document.getElementById("shopping-assistant-card")) return;
    const host = document.createElement("div");
    host.id = "shopping-assistant-card";
    host.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:2147483647;";
    const shadow = host.attachShadow({ mode: "closed" });
    globalThis.ShoppingCard.render(shadow, prompt);

    const close = () => host.remove();
    const answer = async (approved) => {
      const size = shadow.querySelector('input[name="size"]').value;
      const color = shadow.querySelector('input[name="color"]').value;
      const response = await send({ type: "confirm", pendingId: prompt.pending_id, approved, size, color });
      if (approved && response && response.ok) {
        globalThis.ShoppingCard.markSaved(shadow);
        setTimeout(close, 2500);
      } else {
        close();
      }
    };
    shadow.querySelector(".main").addEventListener("click", () => answer(true));
    shadow.querySelector(".ghost").addEventListener("click", () => answer(false));
    shadow.querySelector(".close").addEventListener("click", () => {
      send({ type: "track", event: "prompt_closed", props: { pending_id: prompt.pending_id, site: prompt.site } });
      close();
    });
    document.documentElement.appendChild(host);
    // The backend counts a prompt as shown when it decides to ask. This confirms the card really appeared.
    send({ type: "track", event: "prompt_rendered", props: { pending_id: prompt.pending_id, site: prompt.site } });
  }

  startPage();
})();
