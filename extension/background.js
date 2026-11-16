// Service worker: owns settings, browsing sessions and every call to the backend.
// Content scripts and the popup never talk to the backend directly.

const DEFAULT_SETTINGS = {
  apiBase: "http://127.0.0.1:8787",
  pilotKey: "",
  sites: { Lululemon: true, SKIMS: true, Nordstrom: true, Sephora: true },
};
const SESSION_IDLE_MS = 30 * 60 * 1000; // 30 minutes with no activity starts a new session.

async function getSettings() {
  const { settings } = await chrome.storage.sync.get("settings");
  return { ...DEFAULT_SETTINGS, ...(settings || {}), sites: { ...DEFAULT_SETTINGS.sites, ...((settings || {}).sites || {}) } };
}

async function sessionId() {
  const now = Date.now();
  const { session } = await chrome.storage.local.get("session");
  if (session && now - session.lastAt < SESSION_IDLE_MS) {
    session.lastAt = now;
    await chrome.storage.local.set({ session });
    return session.id;
  }
  const fresh = { id: crypto.randomUUID(), lastAt: now };
  await chrome.storage.local.set({ session: fresh });
  return fresh.id;
}

async function api(method, path, body) {
  const settings = await getSettings();
  const response = await fetch(settings.apiBase.replace(/\/+$/, "") + path, {
    method,
    headers: { "Content-Type": "application/json", "X-Pilot-Key": settings.pilotKey || "" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

// Usage events for the pilot metrics. Never blocks the UI and never throws: a lost
// event is better than a broken button. Product data stays out of these on purpose.
async function track(event, props = {}) {
  try {
    await api("POST", "/track", { event, props: { ...props, session_id: await sessionId(), version: chrome.runtime.getManifest().version } });
  } catch (_) {
    // The backend is down or the key is not set yet.
  }
  return { tracked: true };
}

async function bumpBadge() {
  const { unseen = 0 } = await chrome.storage.local.get("unseen");
  const next = unseen + 1;
  await chrome.storage.local.set({ unseen: next });
  await chrome.action.setBadgeBackgroundColor({ color: "#1f1f1f" });
  await chrome.action.setBadgeText({ text: String(next) });
}

async function handle(message) {
  switch (message.type) {
    case "event": {
      const settings = await getSettings();
      if (!settings.sites[message.product.site]) return { skipped: true };
      const data = await api("POST", "/events", {
        event_type: message.eventType,
        session_id: await sessionId(),
        occurred_at: new Date().toISOString(),
        product: message.product,
      });
      if (data.saved) await bumpBadge();
      return data;
    }
    case "confirm": {
      const data = await api("POST", "/confirm", {
        pending_id: message.pendingId,
        approved: message.approved,
        size: message.size,
        color: message.color,
      });
      if (data.saved) await bumpBadge();
      return data;
    }
    case "wishlist":
      await chrome.storage.local.set({ unseen: 0 });
      await chrome.action.setBadgeText({ text: "" });
      return api("GET", "/wishlist");
    case "bought":
      return api(message.bought ? "POST" : "DELETE", `/wishlist/${encodeURIComponent(message.id)}/bought`);
    case "track":
      return track(message.event, message.props);
    case "testEmail":
      return api("POST", "/test-email", {});
    case "getPreferences":
      return api("GET", "/preferences");
    case "update":
      return api("PATCH", `/wishlist/${encodeURIComponent(message.id)}`, { size: message.size, color: message.color });
    case "remove":
      return api("DELETE", `/wishlist/${encodeURIComponent(message.id)}`);
    case "preferences":
      return api("PUT", "/preferences", { email: message.email, email_address: message.emailAddress });
    case "sites":
      return api("GET", `/sites?q=${encodeURIComponent(message.query || "")}`);
    case "clearHistory":
      return api("DELETE", "/history");
    case "health":
      return api("GET", "/health");
    default:
      throw new Error(`Unknown message ${message.type}`);
  }
}

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") track("extension_installed", { reason: details.reason });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handle(message)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
  return true; // Keep the channel open for the async reply.
});
