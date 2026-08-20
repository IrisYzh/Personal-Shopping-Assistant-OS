from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from .types import ProductPageSnapshot


SCRIPT_PATTERN = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    flags=re.IGNORECASE | re.DOTALL,
)
META_PATTERN = re.compile(r"<meta\s+([^>]+)>", flags=re.IGNORECASE)
ATTRIBUTE_PATTERN = re.compile(r"([:\w-]+)\s*=\s*[\"'](.*?)[\"']", flags=re.DOTALL)


def _walk_products(value: Any):
    if isinstance(value, dict):
        type_value = value.get("@type")
        types = type_value if isinstance(type_value, list) else [type_value]
        if "Product" in types:
            yield value
        for child in value.values():
            yield from _walk_products(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_products(child)


def parse_jsonld(snapshot: ProductPageSnapshot) -> dict[str, Any]:
    errors: list[str] = []
    products: list[dict[str, Any]] = []
    for block in SCRIPT_PATTERN.findall(snapshot.html):
        try:
            parsed = json.loads(html_lib.unescape(block).strip())
            products.extend(_walk_products(parsed))
        except (json.JSONDecodeError, TypeError) as exc:
            errors.append(str(exc))
    if not products:
        return {"found": False, "errors": errors}

    product = products[0]
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    availability = str(offers.get("availability", "")).lower()
    if "instock" in availability:
        stock = "in_stock"
    elif "outofstock" in availability or "soldout" in availability:
        stock = "out_of_stock"
    else:
        stock = "unknown"
    raw_price = offers.get("price") or offers.get("lowPrice")
    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "found": True,
        "name": product.get("name"),
        "url": product.get("url") or snapshot.url,
        "price": price,
        "currency": offers.get("priceCurrency") or "CNY",
        "availability": stock,
        "image_url": image,
        "evidence": ["schema.org/Product", "schema.org/Product.offers"],
    }


def inspect_meta(snapshot: ProductPageSnapshot) -> dict[str, Any]:
    values: dict[str, str] = {}
    for attributes in META_PATTERN.findall(snapshot.html):
        parsed = {key.lower(): value for key, value in ATTRIBUTE_PATTERN.findall(attributes)}
        key = parsed.get("property") or parsed.get("name")
        content = parsed.get("content")
        if key and content:
            values[key.lower()] = html_lib.unescape(content)
    price_value = values.get("product:price:amount") or values.get("og:price:amount")
    try:
        price = float(price_value) if price_value else None
    except ValueError:
        price = None
    return {
        "name": values.get("og:title") or snapshot.title or None,
        "url": values.get("og:url") or snapshot.url,
        "price": price,
        "currency": values.get("product:price:currency") or "CNY",
        "image_url": values.get("og:image"),
        "evidence": [key for key in values if key.startswith(("og:", "product:"))],
    }


def inspect_visible_text(snapshot: ProductPageSnapshot, limit: int = 6000) -> dict[str, Any]:
    text = snapshot.visible_text
    if not text and snapshot.html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", snapshot.html, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"text": text[:limit], "truncated": len(text) > limit}
