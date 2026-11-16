// Reads product name, price and stock from a product page.
// Order: JSON-LD first, then meta tags. If neither works, it returns null and never guesses.
// Pure functions, so tests can run them in Node without a browser.
(function (root) {
  const SITES = {
    "shop.lululemon.com": "Lululemon",
    "skims.com": "SKIMS",
    "www.skims.com": "SKIMS",
    "www.nordstrom.com": "Nordstrom",
    "www.sephora.com": "Sephora",
  };

  function siteFor(url) {
    try {
      return SITES[new URL(url).hostname.toLowerCase()] || null;
    } catch (_) {
      return null;
    }
  }

  function canonicalUrl(url) {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/+$/, "") || "/";
    return `https://${parsed.hostname.toLowerCase()}${path}`;
  }

  // JSON-LD can be one object, an array, or a {"@graph": [...]} wrapper.
  function flatten(node, out) {
    if (!node || typeof node !== "object") return out;
    if (Array.isArray(node)) {
      node.forEach((child) => flatten(child, out));
      return out;
    }
    out.push(node);
    if (node["@graph"]) flatten(node["@graph"], out);
    // ProductGroup (common on Shopify sites like SKIMS) holds the real products here.
    if (node.hasVariant) flatten(node.hasVariant, out);
    return out;
  }

  function isType(node, type) {
    const value = node["@type"];
    return Array.isArray(value) ? value.includes(type) : value === type;
  }

  function availabilityFrom(value) {
    const text = String(value || "").toLowerCase();
    if (!text) return "unknown";
    if (text.includes("instock") || text.includes("limitedavailability") || text.includes("onlineonly")) return "in_stock";
    if (text.includes("outofstock") || text.includes("soldout") || text.includes("discontinued")) return "out_of_stock";
    return "unknown";
  }

  function firstOffer(offers) {
    if (!offers) return null;
    const list = Array.isArray(offers) ? offers : [offers];
    // Prefer an in-stock offer, so one sold-out size does not mark the whole product sold out.
    const inStock = list.find((offer) => availabilityFrom(offer.availability) === "in_stock");
    return inStock || list[0];
  }

  function priceFrom(offer) {
    if (!offer) return null;
    const raw = offer.price ?? offer.lowPrice ?? (offer.priceSpecification && offer.priceSpecification.price);
    const value = parseFloat(String(raw ?? "").replace(/[^0-9.]/g, ""));
    return Number.isFinite(value) && value > 0 ? value : null;
  }

  function textOf(value) {
    if (value && typeof value === "object") value = Array.isArray(value) ? value[0] : value.name || value.value;
    const text = value == null ? "" : String(value).trim();
    return text || null;
  }

  function offersOf(node) {
    let offers = node.offers;
    if (offers && !Array.isArray(offers) && Array.isArray(offers.offers)) offers = offers.offers;
    if (!offers) return [];
    return (Array.isArray(offers) ? offers : [offers]).filter((offer) => offer && typeof offer === "object");
  }

  // Every size or color the page lists, with its own stock. The backend picks the saved size,
  // so "back in stock" can mean "back in your size". Same rules as shopping_agent/page_reader.py.
  function variantsFrom(products) {
    const out = [];
    for (const node of products) {
      const size = textOf(node.size);
      const color = textOf(node.color);
      const offers = offersOf(node);
      if ((size || color) && offers.length) {
        const best = firstOffer(offers);
        out.push({ size, color, availability: availabilityFrom(best.availability), price: priceFrom(best) });
        continue;
      }
      for (const offer of offers) {
        const label = textOf(offer.size) || textOf(offer.name);
        if (label) out.push({ size: label, color: null, availability: availabilityFrom(offer.availability), price: priceFrom(offer) });
      }
    }
    return out.slice(0, 200);
  }

  function fromJsonLd(jsonLdTexts) {
    const nodes = [];
    for (const text of jsonLdTexts) {
      try {
        flatten(JSON.parse(text), nodes);
      } catch (_) {
        // A broken script tag on the page should not stop the others.
      }
    }
    const products = nodes.filter((node) => isType(node, "Product") || isType(node, "ProductGroup"));
    if (!products.length) return null;
    const named = products.find((node) => node.name) || products[0];
    const withOffer = products.find((node) => node.offers) || named;
    const offer = firstOffer(withOffer.offers);
    if (!named.name) return null;
    return {
      name: String(named.name).trim(),
      price: priceFrom(offer),
      currency: (offer && offer.priceCurrency) || "USD",
      availability: availabilityFrom(offer && offer.availability),
      confidence: 0.95,
      source: "json_ld",
      variants: variantsFrom(products),
    };
  }

  function fromMeta(meta) {
    const name = meta["og:title"] || meta["twitter:title"];
    const isProduct = (meta["og:type"] || "").toLowerCase().includes("product") || meta["product:price:amount"];
    if (!name || !isProduct) return null;
    const price = parseFloat(meta["product:price:amount"] || meta["og:price:amount"] || "");
    return {
      name: String(name).trim(),
      price: Number.isFinite(price) && price > 0 ? price : null,
      currency: meta["product:price:currency"] || meta["og:price:currency"] || "USD",
      availability: availabilityFrom(meta["product:availability"] || meta["og:availability"]),
      confidence: 0.8,
      source: "meta",
    };
  }

  function parseProduct({ url, jsonLdTexts = [], meta = {} }) {
    if (!siteFor(url)) return null;
    const product = fromJsonLd(jsonLdTexts) || fromMeta(meta);
    if (!product) return null;
    return { ...product, url: canonicalUrl(url), site: siteFor(url) };
  }

  // Button text that means "add to cart" or "save to wishlist" on the four sites.
  const CART = /\badd to (bag|cart|basket)\b/i;
  const WISHLIST = /(add to (wish ?list|favorites|loves)|\bwish ?list\b|\bfavorite\b|\blove\b|save for later|^save(,|$))/i; // Lululemon heart: "Save, <product name>"
  const REMOVE = /\b(remove|delete|unlove|unfavorite|unsave)\b/i;

  function classifyClick(label) {
    const text = String(label || "").replace(/\s+/g, " ").trim();
    if (!text || text.length > 80) return null;
    if (REMOVE.test(text)) return null;
    if (CART.test(text)) return "add_to_cart";
    if (WISHLIST.test(text)) return "add_to_wishlist";
    return null;
  }

  const api = { siteFor, canonicalUrl, parseProduct, classifyClick, availabilityFrom, variantsFrom };
  root.ShoppingParser = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
