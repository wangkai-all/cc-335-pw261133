"""Seed sources for the two parse-rule versions.

Rule code is stored in `rule_versions.source` when first seeded, then executed by
the parser worker processes.  The source text is the version: changing it means
creating a new rule version, never editing a version that has parsed documents.

Both rules implement the same tiny interface:

    FIELDS = [...]
    parse_item(root, url) -> {"kind": "item", "fields": {...}, "rows": [],
                              "links": [...]}
    parse_list(root, url) -> {"kind": "list", "fields": {},
                              "rows": [{"key", "fields"}], "links": [...]}
"""

OLD_RULE = r'''
# parse-old: selectors written against the original listing markup.
FIELDS = ["title", "price", "brand", "sku", "stock", "description"]
ROW_FIELDS = ["title", "price", "url"]


def _text(node):
    return node.text_content() if node is not None else ""


def _first(root, selectors):
    for sel in selectors:
        node = root.select_one(sel)
        if node is not None:
            return node
    return None


def _money(value):
    m = re.search(r"(-?\d+(?:\.\d+)?)", (value or "").replace(",", ""))
    return m.group(1) if m else (value or "").strip()


def _href(node, base):
    if node is None:
        return ""
    if node.tag == "a":
        return urljoin(base, node.get("href", ""))
    a = node.select_one("a[href]")
    return urljoin(base, a.get("href", "")) if a is not None else ""


def _links(root, base):
    seen = []
    for a in root.select("a[href]"):
        href = a.get("href", "")
        if href.startswith("/item/") or href.startswith("/category/"):
            full = urljoin(base, href)
            if full not in seen:
                seen.append(full)
    return seen


def parse_item(root, url):
    box = _first(root, [".product-detail", "main", "body"])
    fields = {
        "title": _text(_first(root, ["h1.title", ".title"])),
        "price": _money(_text(_first(root, [".price"]))),
        "brand": _text(_first(root, [".brand"])),
        "sku": (box.get("data-sku") or "") if box is not None else "",
        "stock": _text(_first(root, [".stock"])),
        "description": _text(_first(root, [".description"])),
    }
    return {"kind": "item", "fields": fields, "rows": [],
            "links": _links(root, url)}


def parse_list(root, url):
    rows = []
    for node in root.select(".row"):
        rows.append({
            "key": node.get("data-item") or "",
            "fields": {
                "title": _text(_first(node, [".title", ".name"])),
                "price": _money(_text(_first(node, [".price"]))),
                "url": _href(node, url),
            },
        })
    return {"kind": "list", "fields": {}, "rows": rows,
            "links": _links(root, url)}
'''

NEW_RULE = r'''
# parse-new: handles the redesigned listing and product markup, while keeping
# fallbacks for pages captured under the original markup.
FIELDS = ["title", "price", "brand", "sku", "stock", "description"]
ROW_FIELDS = ["title", "price", "url"]


def _text(node):
    return node.text_content() if node is not None else ""


def _first(root, selectors):
    for sel in selectors:
        node = root.select_one(sel)
        if node is not None:
            return node
    return None


def _money(value):
    m = re.search(r"(-?\d+(?:\.\d+)?)", (value or "").replace(",", ""))
    return m.group(1) if m else (value or "").strip()


def _href(node, base):
    if node is None:
        return ""
    if node.tag == "a":
        return urljoin(base, node.get("href", ""))
    a = node.select_one("a[href]")
    return urljoin(base, a.get("href", "")) if a is not None else ""


def _links(root, base):
    seen = []
    for a in root.select("a[href]"):
        href = a.get("href", "")
        if href.startswith("/item/") or href.startswith("/category/"):
            full = urljoin(base, href)
            if full not in seen:
                seen.append(full)
    return seen


def parse_item(root, url):
    title_node = _first(root, ["h1.title", ".title"])
    price_node = _first(root, [".price"])
    sku_node = _first(root, ["[data-skuid]", ".product-detail[data-sku]",
                             "[data-sku]"])
    stock_node = _first(root, ["[data-stock]", ".stock"])
    chunks = []
    for sel in [".description", ".description-extra"]:
        node = root.select_one(sel)
        if node is not None:
            text = node.text_content()
            if text and text not in chunks:
                chunks.append(text)
    if price_node is not None and price_node.get("data-value"):
        price = price_node.get("data-value")
    else:
        price = _money(_text(price_node))
    fields = {
        "title": _text(title_node),
        "price": (price or "").strip(),
        "brand": _text(_first(root, [".maker", ".brand"])),
        "sku": (sku_node.get("data-skuid") or sku_node.get("data-sku") or "")
               if sku_node is not None else "",
        "stock": (stock_node.get("data-stock") or _text(stock_node))
                 if stock_node is not None else "",
        "description": " ".join(chunks),
    }
    return {"kind": "item", "fields": fields, "rows": [],
            "links": _links(root, url)}


def parse_list(root, url):
    rows = []
    groups = root.select(".row-group")
    if groups:
        # A redesigned row is one group containing sibling title/price parts.
        for group in groups:
            key = group.get("data-item") or ""
            title = _text(_first(group, [".row-title .title", ".row-title",
                                         ".title", ".name"]))
            price = _money(_text(_first(group, [".row-price .price",
                                                ".price"])))
            rows.append({"key": key,
                         "fields": {"title": title, "price": price,
                                    "url": _href(group, url)}})
    else:
        for node in root.select(".row"):
            rows.append({
                "key": node.get("data-item") or "",
                "fields": {
                    "title": _text(_first(node, [".title", ".name"])),
                    "price": _money(_text(_first(node, [".price"]))),
                    "url": _href(node, url),
                },
            })
    return {"kind": "list", "fields": {}, "rows": rows,
            "links": _links(root, url)}
'''

DRAFT_RULE = r'''
# parse-draft: deliberately imperfect candidate used to demonstrate that the
# comparison gate catches field loss and split list rows instead of trusting
# row counts.  Item description drops the extra chunk; list grouping finds the
# right containers but reads price from the title part.
FIELDS = ["title", "price", "brand", "sku", "stock", "description"]
ROW_FIELDS = ["title", "price", "url"]


def _text(node):
    return node.text_content() if node is not None else ""


def _first(root, selectors):
    for sel in selectors:
        node = root.select_one(sel)
        if node is not None:
            return node
    return None


def _money(value):
    m = re.search(r"(-?\d+(?:\.\d+)?)", (value or "").replace(",", ""))
    return m.group(1) if m else (value or "").strip()


def _href(node, base):
    if node is None:
        return ""
    if node.tag == "a":
        return urljoin(base, node.get("href", ""))
    a = node.select_one("a[href]")
    return urljoin(base, a.get("href", "")) if a is not None else ""


def _links(root, base):
    seen = []
    for a in root.select("a[href]"):
        href = a.get("href", "")
        if href.startswith("/item/") or href.startswith("/category/"):
            full = urljoin(base, href)
            if full not in seen:
                seen.append(full)
    return seen


def parse_item(root, url):
    price_node = _first(root, [".price"])
    sku_node = _first(root, ["[data-skuid]", ".product-detail[data-sku]"])
    stock_node = _first(root, ["[data-stock]", ".stock"])
    fields = {
        "title": _text(_first(root, ["h1.title", ".title"])),
        "price": (price_node.get("data-value") if price_node is not None
                  and price_node.get("data-value") else _money(_text(price_node))),
        "brand": _text(_first(root, [".maker", ".brand"])),
        "sku": (sku_node.get("data-skuid") or sku_node.get("data-sku") or "")
               if sku_node is not None else "",
        # BUG under test: stock is never read, and the description is cut to a
        # teaser (an over-aggressive "short description" normalization).
        "stock": "",
        "description": _text(_first(root, [".description"]))[:10],
    }
    return {"kind": "item", "fields": fields, "rows": [],
            "links": _links(root, url)}


def parse_list(root, url):
    rows = []
    groups = root.select(".row-group")
    if groups:
        for group in groups:
            # BUG under test: price is read from inside the title part.
            title_part = group.select_one(".row-title")
            rows.append({
                "key": group.get("data-item") or "",
                "fields": {
                    "title": _text(_first(group, [".row-title .title", ".title"])),
                    "price": _money(_text(_first(title_part, [".price"]))),
                    "url": _href(group, url),
                },
            })
    else:
        for node in root.select(".row"):
            rows.append({
                "key": node.get("data-item") or "",
                "fields": {
                    "title": _text(_first(node, [".title", ".name"])),
                    "price": _money(_text(_first(node, [".price"]))),
                    "url": _href(node, url),
                },
            })
    return {"kind": "list", "fields": {}, "rows": rows,
            "links": _links(root, url)}
'''

# The worker gives rule code only the helpers it is expected to use.
RULE_BUILTINS = {
    "re": __import__("re"),
    "urljoin": __import__("urllib.parse", fromlist=["urljoin"]).urljoin,
    "len": len,
    "str": str,
    "dict": dict,
    "list": list,
    "set": set,
    "any": any,
    "all": all,
    "sorted": sorted,
    "range": range,
    "enumerate": enumerate,
    "min": min,
    "max": max,
    "strip": str.strip,
}
