// Run with: node --test extension/tests/*.test.js
const test = require("node:test");
const assert = require("node:assert");
const { parseProduct, classifyClick, canonicalUrl } = require("../product-parser.js");

test("reads a Lululemon style JSON-LD product with several offers", () => {
  const jsonLd = JSON.stringify({
    "@context": "https://schema.org",
    "@type": "Product",
    name: "Align High-Rise Pant 25\"",
    offers: [
      { "@type": "Offer", price: "98.00", priceCurrency: "USD", availability: "https://schema.org/OutOfStock" },
      { "@type": "Offer", price: "98.00", priceCurrency: "USD", availability: "https://schema.org/InStock" },
    ],
  });
  const product = parseProduct({ url: "https://shop.lululemon.com/p/women-pants/Align/_/prod2020012?color=0001", jsonLdTexts: [jsonLd] });
  assert.strictEqual(product.name, 'Align High-Rise Pant 25"');
  assert.strictEqual(product.price, 98);
  assert.strictEqual(product.availability, "in_stock");
  assert.strictEqual(product.site, "Lululemon");
  assert.strictEqual(product.url, "https://shop.lululemon.com/p/women-pants/Align/_/prod2020012");
});

test("reads a Shopify ProductGroup inside @graph, as on SKIMS", () => {
  const jsonLd = JSON.stringify({
    "@graph": [
      { "@type": "WebSite", name: "SKIMS" },
      {
        "@type": "ProductGroup",
        name: "Fits Everybody T-Shirt Bra",
        hasVariant: [{ "@type": "Product", offers: { price: 48, priceCurrency: "USD", availability: "OutOfStock" } }],
      },
    ],
  });
  const product = parseProduct({ url: "https://skims.com/products/fits-everybody-t-shirt-bra", jsonLdTexts: ["{bad json", jsonLd] });
  assert.strictEqual(product.name, "Fits Everybody T-Shirt Bra");
  assert.strictEqual(product.price, 48);
  assert.strictEqual(product.availability, "out_of_stock");
});

test("falls back to meta tags and never guesses", () => {
  const meta = { "og:type": "product", "og:title": "Rare Beauty Soft Pinch Liquid Blush", "product:price:amount": "23" };
  const product = parseProduct({ url: "https://www.sephora.com/product/P97989778", meta });
  assert.strictEqual(product.price, 23);
  assert.strictEqual(product.availability, "unknown");
  assert.strictEqual(product.confidence, 0.8);
  assert.strictEqual(parseProduct({ url: "https://www.sephora.com/shop/blush", meta: { "og:title": "Blush" } }), null);
});

test("ignores unsupported sites", () => {
  assert.strictEqual(parseProduct({ url: "https://www.amazon.com/dp/B0", meta: { "og:type": "product", "og:title": "X" } }), null);
});

test("classifies cart and wishlist buttons on the four sites", () => {
  assert.strictEqual(classifyClick("Add to Bag"), "add_to_cart");
  assert.strictEqual(classifyClick("  Add to   Basket "), "add_to_cart");
  assert.strictEqual(classifyClick("Add to Wish List"), "add_to_wishlist");
  assert.strictEqual(classifyClick("Love"), "add_to_wishlist");
  assert.strictEqual(classifyClick("Remove from wishlist"), null);
  assert.strictEqual(classifyClick("Size guide"), null);
});

test("canonical url drops tracking params", () => {
  assert.strictEqual(canonicalUrl("https://www.nordstrom.com/s/sweater/123?origin=hp&color=red#reviews"), "https://www.nordstrom.com/s/sweater/123");
});
