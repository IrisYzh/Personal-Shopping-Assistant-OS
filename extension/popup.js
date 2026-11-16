// Popup: the wishlist, and the settings for sites, email and privacy.
const DEFAULT_EMAIL = { weekly_digest: true, daily_digest: false, size_back: true, price_drop: true, sale_start: false };
const DEFAULTS = { apiBase: "http://127.0.0.1:8787", pilotKey: "", email: DEFAULT_EMAIL, sites: {} };
// Used only in preview mode. In the extension, suggestions come from the backend catalog.
const PREVIEW_CATALOG = [
  { name: "Lululemon", domain: "shop.lululemon.com", aliases: ["lulu"], category: "Activewear" },
  { name: "SKIMS", domain: "skims.com", aliases: ["skim"], category: "Shapewear and basics" },
  { name: "Nordstrom", domain: "nordstrom.com", aliases: ["nordstrom rack"], category: "Department store" },
  { name: "Sephora", domain: "sephora.com", aliases: [], category: "Beauty" },
];

// Preview mode: when this page is opened as a plain file (from preview.html), there is no
// extension API, so it shows sample data instead. Inside Chrome it talks to background.js.
const PREVIEW = !(globalThis.chrome && chrome.runtime && chrome.runtime.id);
const SAMPLE_ITEMS = [
  { id: "1", name: "Align High-Rise Pant 25\"", url: "#", site: "Lululemon", last_price: 69, baseline_price: 98, currency: "USD", availability: "in_stock", size: "6", color: "Black", status: "watching" },
  { id: "2", name: "Fits Everybody T-Shirt Bra", url: "#", site: "SKIMS", last_price: 48, baseline_price: 48, currency: "USD", availability: "out_of_stock", size: "34B", color: "Clay" },
  { id: "3", name: "Rare Beauty Soft Pinch Liquid Blush", url: "#", site: "Sephora", last_price: 23, baseline_price: 23, currency: "USD", availability: "unknown", size: "", color: "Hope" },
  { id: "4", name: "Wunder Puff Cropped Jacket", url: "#", site: "Lululemon", last_price: 149, baseline_price: 198, currency: "USD", availability: "in_stock", size: "4", color: "Black", status: "bought", bought_at: "2026-11-12T18:00:00+00:00" },
];
const previewState = new URLSearchParams(location.search).get("state");
const previewStore = { settings: { ...DEFAULTS } };

function call(message) {
  if (PREVIEW) {
    if (previewState === "error") return Promise.resolve({ ok: false, error: "Failed to fetch" });
    if (message.type === "bought") return Promise.resolve({ ok: true, data: { item: { status: message.bought ? "bought" : "watching", bought_at: message.bought ? new Date().toISOString() : null } } });
    if (message.type === "testEmail") return Promise.resolve({ ok: true, data: { sent_to: "you@example.com" } });
    if (message.type === "getPreferences") return Promise.resolve({ ok: true, data: { preferences: { email: DEFAULT_EMAIL, email_address: "you@example.com" } } });
    if (message.type === "update") return Promise.resolve({ ok: true, data: { item: { size: message.size.trim(), color: message.color.trim() } } });
    if (message.type === "wishlist") return Promise.resolve({ ok: true, data: { items: previewState === "empty" ? [] : SAMPLE_ITEMS } });
    if (message.type === "clearHistory") return Promise.resolve({ ok: true, data: { removed_events: 42 } });
    if (message.type === "sites") {
      const q = message.query.trim().toLowerCase();
      const hit = (site) => [site.name.toLowerCase(), site.domain, ...site.aliases].some((word) => word.includes(q));
      return Promise.resolve({ ok: true, data: { sites: q ? PREVIEW_CATALOG.filter(hit) : [] } });
    }
    return Promise.resolve({ ok: true, data: {} });
  }
  return new Promise((resolve) => chrome.runtime.sendMessage(message, (response) => resolve(response || { ok: false, error: "No response" })));
}

const storage = PREVIEW
  ? { get: async () => previewStore, set: async (value) => Object.assign(previewStore, value) }
  : chrome.storage.sync;

function money(value, currency) {
  if (value === null || value === undefined || value === "") return "price unknown";
  return (currency === "USD" ? "$" : `${currency} `) + Number(value).toFixed(2);
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== undefined) node.textContent = text;
  return node;
}

// Usage events for the pilot metrics (see docs/pilot-metrics.md). Fire and forget.
function track(event, props = {}) {
  if (!PREVIEW) call({ type: "track", event, props });
}

// ----- wishlist -----
let allItems = [];

async function loadWishlist() {
  const status = document.getElementById("status");
  const response = await call({ type: "wishlist" });
  if (!response.ok) {
    document.getElementById("items").replaceChildren();
    document.getElementById("boughtBox").hidden = true;
    status.className = "error";
    status.textContent = `Could not reach the backend. ${response.error}. Check the URL and key in Settings.`;
    return;
  }
  allItems = response.data.items || [];
  drawWishlist();
}

function drawWishlist() {
  const status = document.getElementById("status");
  const list = document.getElementById("items");
  const watching = allItems.filter((item) => item.status !== "bought");
  const bought = allItems.filter((item) => item.status === "bought");
  status.className = "muted";
  status.textContent = watching.length
    ? `Watching ${watching.length} item${watching.length === 1 ? "" : "s"} for you.`
    : bought.length
      ? "Not watching anything right now."
      : "Nothing here yet. Add an item to your cart or wishlist on a supported site, or say yes when I ask.";
  list.replaceChildren(...watching.map(renderItem));
  const box = document.getElementById("boughtBox");
  box.hidden = !bought.length;
  document.getElementById("boughtCount").textContent = `Bought (${bought.length})`;
  document.getElementById("boughtItems").replaceChildren(...bought.map(renderBought));
}

async function setBought(item, bought) {
  const response = await call({ type: "bought", id: item.id, bought });
  if (!response.ok) return false;
  const saved = (response.data && response.data.item) || {};
  item.status = saved.status || (bought ? "bought" : "watching");
  item.bought_at = saved.bought_at || null;
  return true;
}

function nameLink(item) {
  const link = el("a", { class: "name", href: item.url, target: "_blank", rel: "noopener" }, item.name);
  link.addEventListener("click", () => track("wishlist_link_clicked", { site: item.site, status: item.status || "watching" }));
  return link;
}

function renderBought(item) {
  const li = el("li", { class: "bought" });
  li.appendChild(nameLink(item));
  const when = item.bought_at ? new Date(item.bought_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  const row = el("div", { class: "prefs" });
  row.appendChild(el("span", { class: "pref-text muted" }, `Bought${when ? " on " + when : ""} · ${money(item.last_price, item.currency)}`));
  const back = el("button", { class: "small" }, "Watch again");
  back.addEventListener("click", async () => {
    if (await setBought(item, false)) drawWishlist();
  });
  row.appendChild(back);
  li.appendChild(row);
  return li;
}

function renderItem(item) {
  const li = el("li");
  li.appendChild(nameLink(item));

  const stock = item.availability === "in_stock" ? "in stock" : item.availability === "out_of_stock" ? "sold out" : "stock unknown";
  const meta = el("div", { class: "meta" }, `${item.site} · ${money(item.last_price, item.currency)} · ${stock}`);
  const base = Number(item.baseline_price);
  const last = Number(item.last_price);
  if (base && last && last < base) {
    meta.appendChild(document.createTextNode(" · "));
    meta.appendChild(el("span", { class: "drop" }, `${Math.round(((base - last) / base) * 100)}% below when you saved it`));
  }
  li.appendChild(meta);

  // Size and color: shown as fixed text with an Edit button. Edit turns them into inputs
  // and the button into Save. Save stores them and switches back to fixed text.
  const prefs = el("div", { class: "prefs" });
  const shown = el("span", { class: "pref-text" });
  const size = el("input", { placeholder: "Size", maxlength: "20", "aria-label": "Size" });
  const color = el("input", { placeholder: "Color", maxlength: "40", "aria-label": "Color" });
  const toggle = el("button", { class: "small" });
  const bought = el("button", { class: "small", title: "Stop watching and keep it under Bought" }, "Bought");
  const remove = el("button", { class: "small remove", title: "Stop watching and delete it" }, "Remove");
  let editing = PREVIEW && previewState === "editing" && item.id === "1"; // Preview of the edit state.

  const describe = () => {
    const parts = [item.size && `Size ${item.size}`, item.color && `Color ${item.color}`].filter(Boolean);
    shown.textContent = parts.length ? parts.join(" · ") : "No size or color yet";
    shown.classList.toggle("muted", !parts.length);
  };
  const render = () => {
    prefs.replaceChildren();
    if (editing) {
      size.value = item.size || "";
      color.value = item.color || "";
      toggle.textContent = "Save";
      toggle.classList.add("primary");
      prefs.append(size, color, toggle);
      size.focus();
    } else {
      describe();
      toggle.textContent = "Edit";
      toggle.classList.remove("primary");
      prefs.append(shown, toggle, bought, remove);
    }
  };
  const save = async () => {
    toggle.disabled = true;
    const response = await call({ type: "update", id: item.id, size: size.value, color: color.value });
    toggle.disabled = false;
    if (!response.ok) {
      toggle.textContent = "Try again";
      return;
    }
    const saved = (response.data && response.data.item) || {};
    item.size = saved.size !== undefined ? saved.size : size.value.trim();
    item.color = saved.color !== undefined ? saved.color : color.value.trim();
    editing = false;
    render();
  };
  toggle.addEventListener("click", () => {
    if (editing) return save();
    editing = true;
    render();
  });
  [size, color].forEach((input) =>
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") save();
      if (event.key === "Escape") {
        editing = false;
        render();
      }
    })
  );
  remove.addEventListener("click", async () => {
    const response = await call({ type: "remove", id: item.id });
    if (response.ok) {
      allItems = allItems.filter((other) => other.id !== item.id);
      drawWishlist();
    }
  });
  // Bought: the row says so for a few seconds with an Undo, then moves under "Bought".
  bought.addEventListener("click", async () => {
    if (!(await setBought(item, true))) return;
    prefs.replaceChildren();
    const undo = el("button", { class: "small" }, "Undo");
    prefs.append(el("span", { class: "pref-text done" }, "Marked as bought. Nice!"), undo);
    const timer = setTimeout(drawWishlist, 4000);
    undo.addEventListener("click", async () => {
      clearTimeout(timer);
      if (await setBought(item, false)) render();
    });
  });
  render();
  li.appendChild(prefs);
  return li;
}

// ----- settings -----
let watched = {}; // site name -> true or false

function renderChips() {
  const box = document.getElementById("sites");
  box.replaceChildren();
  const names = Object.keys(watched).filter((name) => watched[name]);
  if (!names.length) box.appendChild(el("span", { class: "muted" }, "No sites yet. Search to add one."));
  names.forEach((name) => {
    const chip = el("span", { class: "chip" }, name);
    const remove = el("button", { "aria-label": `Stop watching ${name}` }, "\u00d7");
    remove.addEventListener("click", () => {
      watched[name] = false;
      track("site_removed", { site: name });
      renderChips();
    });
    chip.appendChild(remove);
    box.appendChild(chip);
  });
}

function setupSiteSearch() {
  const input = document.getElementById("siteSearch");
  const list = document.getElementById("siteSuggestions");
  let results = [];
  let active = -1;
  let seq = 0;

  let lastLogged = "";
  // One site_search event per finished search, not per keystroke. Typed text is kept only
  // when nothing matched, because those are the sites friends want us to add next.
  const logSearch = (added) => {
    const text = input.value.trim().toLowerCase();
    if (!text || text === lastLogged) return;
    lastLogged = text;
    track("site_search", { results: results.length, added: added || null, query: results.length ? null : text.slice(0, 40) });
  };
  const close = () => {
    logSearch();
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  };
  const choose = (site) => {
    logSearch(site.name);
    if (!watched[site.name]) track("site_added", { site: site.name });
    watched[site.name] = true;
    renderChips();
    input.value = "";
    close();
    input.focus();
  };
  const draw = () => {
    list.replaceChildren();
    if (!input.value.trim()) return close();
    if (!results.length) {
      list.appendChild(el("li", { class: "empty" }, `No supported site matches "${input.value.trim()}" yet. More sites are coming.`));
    }
    results.forEach((site, index) => {
      const added = watched[site.name];
      const row = el("li", { role: "option", "aria-selected": String(index === active) });
      row.append(el("span", {}, site.name), el("span", { class: "hint" }, added ? "Added" : site.category || site.domain));
      row.addEventListener("mousedown", (event) => {
        event.preventDefault(); // Keep focus in the input.
        choose(site);
      });
      list.appendChild(row);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };

  input.addEventListener("input", async () => {
    const mine = ++seq;
    const response = await call({ type: "sites", query: input.value });
    if (mine !== seq) return; // A newer keystroke already answered.
    results = response.ok ? response.data.sites || [] : [];
    active = results.length ? 0 : -1;
    draw();
  });
  input.addEventListener("keydown", (event) => {
    if (list.hidden) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!results.length) return;
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      draw();
    } else if (event.key === "Enter" && results[active]) {
      event.preventDefault();
      choose(results[active]);
    } else if (event.key === "Escape") {
      close();
    }
  });
  // In the preview page several popups sit side by side, so keep the list open there.
  if (!(previewState || "").startsWith("search:")) input.addEventListener("blur", close);
}

async function loadSettings() {
  const { settings } = await storage.get("settings");
  const current = { ...DEFAULTS, ...(settings || {}) };
  document.getElementById("apiBase").value = current.apiBase;
  document.getElementById("pilotKey").value = current.pilotKey;
  const email = { ...DEFAULT_EMAIL, ...(current.email || {}) };
  document.querySelectorAll("fieldset input[type=checkbox]").forEach((box) => (box.checked = Boolean(email[box.name])));
  // Until the user changes it, every supported site is on.
  watched = { Lululemon: true, SKIMS: true, Nordstrom: true, Sephora: true, ...current.sites };
  renderChips();
  document.getElementById("emailAddress").value = current.emailAddress || "";
  // The backend is the source of truth for email, so a reinstall does not lose it.
  const remote = await call({ type: "getPreferences" });
  if (remote.ok && remote.data && remote.data.preferences) {
    const prefs = remote.data.preferences;
    if (prefs.email_address) document.getElementById("emailAddress").value = prefs.email_address;
    const choices = { ...DEFAULT_EMAIL, ...(prefs.email || {}) };
    document.querySelectorAll("fieldset input[type=checkbox]").forEach((box) => (box.checked = Boolean(choices[box.name])));
  }
}

async function saveSettings() {
  const note = document.getElementById("saved");
  const apiBase = document.getElementById("apiBase").value.trim().replace(/\/+$/, "");
  const email = {};
  document.querySelectorAll("fieldset input[type=checkbox]").forEach((box) => (email[box.name] = box.checked));
  const emailAddress = document.getElementById("emailAddress").value.trim();
  const settings = {
    apiBase,
    pilotKey: document.getElementById("pilotKey").value.trim(),
    email,
    emailAddress,
    sites: { ...watched },
  };
  const anyEmail = Object.values(email).some(Boolean);
  if (anyEmail && !emailAddress) {
    note.textContent = "Add your email address so I know where to write.";
    document.getElementById("emailAddress").focus();
    return;
  }
  // A hosted backend needs host permission for its own origin. Chrome asks the user once.
  try {
    const origin = new URL(apiBase).origin + "/*";
    if (origin.startsWith("https://") && !PREVIEW) {
      const granted = await chrome.permissions.request({ origins: [origin] });
      if (!granted) {
        note.textContent = "Chrome needs permission to reach your backend.";
        return;
      }
    }
  } catch (_) {
    note.textContent = "That backend URL does not look right.";
    return;
  }
  await storage.set({ settings });
  const response = await call({ type: "preferences", email, emailAddress });
  note.textContent = response.ok ? "Saved." : `Saved here, but the backend said: ${response.error}`;
}

setupSiteSearch();
document.getElementById("save").addEventListener("click", saveSettings);
document.getElementById("testEmail").addEventListener("click", async () => {
  const note = document.getElementById("testNote");
  note.textContent = "Sending...";
  const response = await call({ type: "testEmail" });
  note.textContent = response.ok ? `Sent to ${response.data.sent_to || "your address"}. Check your inbox.` : response.error;
});
document.getElementById("clearHistory").addEventListener("click", async (event) => {
  const response = await call({ type: "clearHistory" });
  event.target.textContent = response.ok ? `Cleared ${response.data.removed_events} records` : "Could not clear";
});

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((other) => other.classList.toggle("active", other === tab));
    document.querySelectorAll(".view").forEach((view) => (view.hidden = view.id !== tab.dataset.view));
    if (tab.dataset.view === "wishlist") loadWishlist();
  })
);

if (previewState === "settings" || (previewState || "").startsWith("search:")) {
  document.querySelector('button[data-view="settings"]').click();
}
if ((previewState || "").startsWith("search:")) {
  // Preview only: show the search box with a query typed in.
  setTimeout(() => {
    const input = document.getElementById("siteSearch");
    input.value = previewState.slice("search:".length);
    input.scrollIntoView({ block: "center" });
    input.focus();
    input.dispatchEvent(new Event("input"));
  }, 50);
}
loadSettings();
loadWishlist();
track("popup_opened");
