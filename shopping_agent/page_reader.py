"""Read price and stock from a product page, down to the size when the page lists sizes.

This is the Python twin of extension/product-parser.js. It reads JSON-LD first, then meta
tags, and never guesses: anything it cannot read stays "unknown".

Size level: many product pages (Shopify ProductGroup, schema.org hasVariant, or one Offer
per size) list every variant with its own availability. When the user saved a size, the
observation uses that size's availability, so "back in stock" means "back in YOUR size".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.request import Request, urlopen

from .models import Availability, Observation, ObservationSource

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_PAGE_BYTES = 3_000_000


@dataclass
class Variant:
    size: str | None
    color: str | None
    availability: Availability
    price: float | None = None


@dataclass
class PageProduct:
    name: str
    price: float | None
    currency: str
    availability: Availability
    confidence: float
    source: str
    promotion_text: str | None = None
    variants: list[Variant] = field(default_factory=list)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self._in_jsonld = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._buffer = []
        elif tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and key not in self.meta:
                self.meta[key] = attrs.get("content") or ""

    def handle_data(self, data):
        if self._in_jsonld:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._buffer))
            self._in_jsonld = False


def availability_from(value: Any) -> Availability:
    text = str(value or "").lower()
    if any(word in text for word in ("instock", "limitedavailability", "onlineonly", "in_stock")):
        return Availability.IN_STOCK
    if any(word in text for word in ("outofstock", "soldout", "discontinued", "out_of_stock")):
        return Availability.OUT_OF_STOCK
    return Availability.UNKNOWN


def _flatten(node: Any, out: list[dict]) -> list[dict]:
    if isinstance(node, list):
        for child in node:
            _flatten(child, out)
    elif isinstance(node, dict):
        out.append(node)
        if "@graph" in node:
            _flatten(node["@graph"], out)
        if "hasVariant" in node:
            _flatten(node["hasVariant"], out)
    return out


def _is_type(node: dict, kind: str) -> bool:
    value = node.get("@type")
    return kind in value if isinstance(value, list) else value == kind


def _offers(node: dict) -> list[dict]:
    offers = node.get("offers")
    if isinstance(offers, dict) and "offers" in offers and isinstance(offers["offers"], list):
        offers = offers["offers"]  # AggregateOffer with a list inside.
    if isinstance(offers, dict):
        return [offers]
    return [offer for offer in offers or [] if isinstance(offer, dict)]


def _price(offer: dict | None) -> float | None:
    if not offer:
        return None
    raw = offer.get("price", offer.get("lowPrice"))
    if raw is None and isinstance(offer.get("priceSpecification"), dict):
        raw = offer["priceSpecification"].get("price")
    try:
        value = float(re.sub(r"[^0-9.]", "", str(raw)))
    except ValueError:
        return None
    return value if value > 0 else None


def _text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("value")
    if isinstance(value, list):
        value = value[0] if value else None
    text = str(value).strip() if value not in (None, "") else None
    return text or None


def _variants(products: list[dict]) -> list[Variant]:
    variants: list[Variant] = []
    for node in products:
        size, color = _text(node.get("size")), _text(node.get("color"))
        offers = _offers(node)
        if (size or color) and offers:
            best = next((o for o in offers if availability_from(o.get("availability")) is Availability.IN_STOCK), offers[0])
            variants.append(Variant(size, color, availability_from(best.get("availability")), _price(best)))
            continue
        for offer in offers:  # Some pages put one Offer per size, with the size in the offer name or sku.
            label = _text(offer.get("size")) or _text(offer.get("name"))
            if label:
                variants.append(Variant(label, None, availability_from(offer.get("availability")), _price(offer)))
    return variants


def parse_html(html: str) -> PageProduct | None:
    collector = _Collector()
    collector.feed(html)
    nodes: list[dict] = []
    for text in collector.jsonld:
        try:
            _flatten(json.loads(text), nodes)
        except json.JSONDecodeError:
            continue
    products = [n for n in nodes if _is_type(n, "Product") or _is_type(n, "ProductGroup")]
    if products:
        named = next((n for n in products if n.get("name")), None)
        if named:
            with_offer = next((n for n in products if n.get("offers")), named)
            offers = _offers(with_offer)
            offer = next((o for o in offers if availability_from(o.get("availability")) is Availability.IN_STOCK), offers[0] if offers else None)
            return PageProduct(
                name=str(named["name"]).strip(),
                price=_price(offer),
                currency=str((offer or {}).get("priceCurrency") or "USD")[:3].upper(),
                availability=availability_from((offer or {}).get("availability")),
                confidence=0.95,
                source="json_ld",
                variants=_variants(products),
            )
    meta = collector.meta
    name = meta.get("og:title") or meta.get("twitter:title")
    if name and ("product" in meta.get("og:type", "").lower() or meta.get("product:price:amount")):
        try:
            price = float(meta.get("product:price:amount") or meta.get("og:price:amount") or "")
        except ValueError:
            price = None
        return PageProduct(
            name=name.strip(),
            price=price if price and price > 0 else None,
            currency=(meta.get("product:price:currency") or "USD")[:3].upper(),
            availability=availability_from(meta.get("product:availability") or meta.get("og:availability")),
            confidence=0.8,
            source="meta",
        )
    return None


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _label_has(label: str | None, wanted: str) -> bool:
    """True when the variant label names the wanted size as a whole token: "6" matches "Black / 6", not "16"."""
    if not label:
        return False
    parts = [_norm(p) for p in re.split(r"\s*(?:/|-|,|\||·)\s*", label)]
    return wanted in parts or _norm(label) == wanted or _norm(label).removeprefix("size ") == wanted


def match_variant(variants: list[Variant], size: str | None, color: str | None) -> list[Variant]:
    """Variants for the saved size (and color, when the page has colors). Empty when no match."""
    if not size or not variants:
        return []
    wanted = _norm(size).removeprefix("size ")
    hits = [v for v in variants if _norm(v.size).removeprefix("size ") == wanted or _label_has(v.size, wanted)]
    if color and hits:
        by_color = [v for v in hits if v.color and _norm(color) in _norm(v.color)]
        hits = by_color or hits
    return hits


def observation_from(product: PageProduct | None, *, size: str | None, color: str | None, checked_at: str,
                     source: ObservationSource = ObservationSource.STRUCTURED, error: str | None = None) -> Observation:
    if product is None:
        return Observation(price=None, availability=Availability.UNKNOWN, confidence=0.0, source=source,
                           checked_at=checked_at, error=error or "No product data found on the page")
    availability, matched_size = product.availability, None
    price = product.price
    hits = match_variant(product.variants, size, color)
    evidence = [f"{product.source}: {product.name}"]
    if hits:
        matched_size = size
        availability = (Availability.IN_STOCK if any(v.availability is Availability.IN_STOCK for v in hits)
                        else Availability.OUT_OF_STOCK if all(v.availability is Availability.OUT_OF_STOCK for v in hits)
                        else Availability.UNKNOWN)
        price = next((v.price for v in hits if v.price), price)
        evidence.append(f"size {size}: {availability.value} ({len(hits)} variant(s))")
    elif size and product.variants:
        evidence.append(f"size {size} not listed on the page; using product-level stock")
    return Observation(price=price, availability=availability, confidence=product.confidence, source=source,
                       checked_at=checked_at, promotion_text=product.promotion_text, evidence=evidence,
                       size=matched_size)


def variants_from_client(raw: Any) -> list[Variant]:
    """Variants the extension read from the page the user is on (same format as parse_html)."""
    out = []
    for entry in (raw or [])[:200] if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        price = entry.get("price")
        out.append(Variant(
            size=(str(entry["size"])[:40] if entry.get("size") else None),
            color=(str(entry["color"])[:40] if entry.get("color") else None),
            availability=availability_from(entry.get("availability")),
            price=float(price) if isinstance(price, (int, float)) and price > 0 else None,
        ))
    return out


def fetch_page(url: str, timeout: float = 20.0) -> str:
    """Fetch like a normal browser would. Some sites block servers; the caller records that as a failed check."""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL comes from the site catalog check)
        return response.read(MAX_PAGE_BYTES).decode(response.headers.get_content_charset() or "utf-8", "replace")
