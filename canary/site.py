"""A mutable local practice site implementing the two markup generations.

Serving is a real HTTP server on a loopback port; the crawler talks to it over
HTTP exactly as it would to a remote site.  Editions mutate the markup:

  v1        original markup (what parse-old was written against)
  v2        redesign: rows split into sibling parts, fields moved to attributes,
            list pages paginated
  v3        redesign plus a promotion banner and lazy description chunk
  v4        redesign plus a price-block wrapper (prices nested deeper)

Seeded items a1..a6 are the "a" series and always exist.  The operator can add
series "b".."f" at runtime from the UI or the CLI, which is how new pages enter
the corpus after a rule switch.
"""

from __future__ import annotations

import html
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SEED_ITEMS = [
    # page id, title, price, brand, sku, stock, description
    ("a1", "可调光台灯 A1", "459.00", "洛森", "LS-A1", "12",
     "三档色温。", "黄铜底座，附送 LED 灯泡。"),
    ("a2", "模块化置物架 A2", "1280.00", "木屿", "MY-A2", "0",
     "承重 40kg。", "可拼接单元，单层高度 32cm。"),
    ("a3", "手冲咖啡壶 A3", "268.00", "岚工", "LG-A3", "5",
     "容量 600ml。", "细口壶嘴，适用于电磁炉。"),
    ("a4", "羊毛盖毯 A4", "599.00", "原野", "YY-A4", "23",
     "尺寸 130×180cm。", "可机洗，缩水率低于 3%。"),
    ("a5", "折叠工作台 A5", "860.00", "洛森", "LS-A5", "2",
     "台面 120×60cm。", "折叠后厚度 9cm，带锁扣。"),
    ("a6", "陶瓷收纳罐 A6", "129.00", "木屿", "MY-A6", "48",
     "容量 1.2L。", "密封圈可拆洗。"),
]

SERIES_NAMES = ["黄铜壁灯", "帆布收纳箱", "胡桃木边桌", "玻璃醒酒器", "亚麻窗帘",
                "铸铁煎锅", "藤编洗衣篮", "玻璃水瓶", "羊毛靠垫", "铜制挂钩",
                "橡木砧板", "陶土花盆", "棉麻桌旗", "不锈钢量杯", "竹制托盘",
                "亚克力相框", "羊毛地毯", "陶瓷马克杯", "黄铜门把手", "折叠晾衣架"]
SERIES_BRANDS = ["洛森", "木屿", "岚工", "原野", "青川", "石间"]
SERIES_DESCRIPTIONS = ["三段亮度，适合阅读。", "手工打磨，纹理略有差异。",
                       "适合小户型，可拆装。", "食品级涂层，可进洗碗机。",
                       "天然材质，颜色随批次微差。"]


@dataclass
class SiteState:
    variant: str = "v1"          # v1 | v2 | v3 | v4
    edition: int = 1
    items: dict[str, dict] = field(default_factory=dict)
    series: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    # counters visible in the UI
    request_count: int = 0
    started_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.items:
            for idx, (pid, title, price, brand, sku, stock, desc, extra) in enumerate(SEED_ITEMS):
                self.items[pid] = {
                    "id": pid, "title": title, "price": price, "brand": brand,
                    "sku": sku, "stock": stock, "description": desc,
                    "extra": extra, "list": "l0" if idx < 4 else "l1",
                }
            self.series = []

    # -- mutation -------------------------------------------------------
    def add_series(self, letter: str | None = None, size: int = 4,
                   list_size: int = 4) -> list[str]:
        with self.lock:
            if letter is None:
                letter = "abcdefghijklmnopqrstuvwxyz"[len(self.series) + 1]
            if letter == "a":
                raise ValueError("series 'a' is reserved for the seed items")
            created = []
            existing_lists = [lid for lid, _ in self.list_pages()]
            next_list = len(existing_lists)
            for i in range(size):
                pid = f"{letter}{i + 1}"
                idx = (len(self.items) + i) % len(SERIES_NAMES)
                created.append(pid)
                self.items[pid] = {
                    "id": pid,
                    "title": f"{SERIES_NAMES[idx]} {pid.upper()}",
                    "price": f"{(idx + 3) * 47 + 20}.00",
                    "brand": SERIES_BRANDS[idx % len(SERIES_BRANDS)],
                    "sku": f"{SERIES_BRANDS[idx % len(SERIES_BRANDS)][:1]}X-{pid.upper()}",
                    "stock": str((idx * 7) % 31),
                    "description": SERIES_DESCRIPTIONS[idx % len(SERIES_DESCRIPTIONS)],
                    "extra": "批次 " + str(200 + idx),
                    "list": f"l{next_list + i // list_size}",
                }
            self.series.append(letter)
            self.edition += 1
            return created

    def set_variant(self, variant: str) -> None:
        if variant not in ("v1", "v2", "v3", "v4"):
            raise ValueError("variant must be v1, v2, v3 or v4")
        with self.lock:
            self.variant = variant
            self.edition += 1

    def set_price(self, page_id: str, price: str) -> None:
        with self.lock:
            if page_id not in self.items:
                raise KeyError(page_id)
            self.items[page_id]["price"] = price
            self.edition += 1

    def set_stock(self, page_id: str, stock: str) -> None:
        with self.lock:
            if page_id not in self.items:
                raise KeyError(page_id)
            self.items[page_id]["stock"] = stock
            self.edition += 1

    def list_pages(self) -> list[tuple[str, list[str]]]:
        pages: dict[str, list[str]] = {}
        for pid in sorted(self.items):
            list_id = self.items[pid]["list"]
            pages.setdefault(list_id, []).append(pid)
        return sorted(pages.items(), key=lambda kv: (len(kv[0]), kv[0]))

    def etag(self, path: str) -> str:
        with self.lock:
            return f'"{self.variant}-{self.edition}-{abs(hash(path)) % 10**8}"'


def _page_shell(title: str, body: str, variant: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head><body>"
        + (f"<div class=\"promo-banner\">秋季家居展 · 订单满 999 减 120</div>"
           if variant in ("v3", "v4") else "")
        + body
        + "</body></html>"
    )


def render_home(state: SiteState) -> str:
    if state.variant == "v1":
        body = "<h1>示例家居资料站</h1><div class=\"nav\"><span class=\"site-version\">版本 1</span></div><ul>"
        for list_id, _ in state.list_pages():
            body += f"<li><a href=\"/category/{list_id}\">分类 {list_id.upper()}</a></li>"
        body += "</ul>"
        return _page_shell("示例家居资料站", body, state.variant)
    body = "<header class=\"site-header\"><h1>示例家居资料站</h1><nav>"
    for list_id, _ in state.list_pages():
        body += f"<a href=\"/category/{list_id}\">分类 {list_id.upper()}</a>"
    body += "</nav></header>"
    return _page_shell("示例家居资料站", body, state.variant)


def render_list(state: SiteState, list_id: str, page_no: int) -> str | None:
    with state.lock:
        ids = next((members for lid, members in state.list_pages()
                    if lid == list_id), None)
    if not ids:
        return None
    page_size = 10
    total_pages = (len(ids) + page_size - 1) // page_size
    if page_no > total_pages:
        return None
    slice_ids = ids[(page_no - 1) * page_size: page_no * page_size]

    if state.variant == "v1":
        rows = []
        for pid in slice_ids:
            item = state.items[pid]
            rows.append(
                f"<div class=\"row\" data-item=\"{pid}\">"
                f"<a class=\"title\" href=\"/item/{pid}\">{html.escape(item['title'])}</a>"
                f"<span class=\"price\">¥{item['price']}</span>"
                f"<span class=\"brand\">{html.escape(item['brand'])}</span>"
                f"</div>"
            )
        body = (f"<h1 class=\"list-title\">分类 {list_id.upper()}</h1>"
                f"<div class=\"product-list\">{''.join(rows)}</div>")
        return _page_shell(f"分类 {list_id.upper()}", body, state.variant)

    rows = []
    for pid in slice_ids:
        item = state.items[pid]
        price_node = (f"<span class=\"price\" data-value=\"{item['price']}\">"
                      f"¥{item['price']}</span>")
        if state.variant == "v4":
            price_node = f"<div class=\"price-block\">{price_node}</div>"
        # The redesign splits one logical row into sibling .row parts.  A
        # selector written against the old one-row-per-item layout therefore
        # sees two partial rows for every item, not one.
        rows.append(
            f"<div class=\"row-group\" data-item=\"{pid}\">"
            f"<div class=\"row part-title\"><div class=\"row-title\">"
            f"<a class=\"title\" href=\"/item/{pid}\">"
            f"{html.escape(item['title'])}</a></div></div>"
            f"<div class=\"row part-price\"><div class=\"row-price\">{price_node}</div></div>"
            f"<div class=\"row-meta\"><span class=\"brand\">{html.escape(item['brand'])}</span>"
            f"<span class=\"sku\">{html.escape(item['sku'])}</span></div>"
            f"</div>"
        )
    pager = ""
    if total_pages > 1:
        links = [f"<a class=\"page\" href=\"/category/{list_id}?page={n}\">第 {n} 页</a>"
                 if n != page_no else f"<span class=\"page current\">{n}</span>"
                 for n in range(1, total_pages + 1)]
        pager = f"<div class=\"pager\">{''.join(links)}</div>"
    body = (f"<h1 class=\"list-title\">分类 {list_id.upper()}</h1>"
            f"<div class=\"product-list\">{''.join(rows)}</div>{pager}")
    return _page_shell(f"分类 {list_id.upper()}", body, state.variant)


def render_item(state: SiteState, page_id: str) -> str | None:
    with state.lock:
        item = state.items.get(page_id)
        if item is None:
            return None
        item = dict(item)
        variant = state.variant
    if variant == "v1":
        body = (
            f"<div class=\"breadcrumb\"><a href=\"/category/{item['list']}\">分类</a></div>"
            f"<div class=\"product-detail\" data-sku=\"{html.escape(item['sku'])}\">"
            f"<h1 class=\"title\">{html.escape(item['title'])}</h1>"
            f"<div class=\"price\">¥{item['price']}</div>"
            f"<div class=\"brand\">{html.escape(item['brand'])}</div>"
            f"<div class=\"stock\">库存 {item['stock']} 件</div>"
            f"<div class=\"description\">{html.escape(item['description'])}</div>"
            f"<div class=\"description-tail\">{html.escape(item['extra'])}</div>"
            f"</div>"
        )
        return _page_shell(item["title"], body, variant)

    desc = html.escape(item["description"])
    extra = html.escape(item["extra"])
    if variant == "v3":
        desc_block = (
            f"<section class=\"detail-description\"><p class=\"description\">{desc}</p>"
            f"<div class=\"description-extra\" data-chunk=\"2\">{extra}</div></section>"
        )
    else:
        desc_block = (
            f"<p class=\"description\">{desc}</p>"
            f"<p class=\"description-extra\">{extra}</p>"
        )
    body = (
        f"<div class=\"breadcrumb\"><a href=\"/category/{item['list']}\">分类</a></div>"
        f"<article class=\"product\" data-skuid=\"{html.escape(item['sku'])}\">"
        f"<h1 class=\"title\">{html.escape(item['title'])}</h1>"
        f"<div class=\"price\" data-value=\"{item['price']}\">¥{item['price']}</div>"
        f"<dl class=\"meta\"><dt>品牌</dt><dd class=\"maker\">{html.escape(item['brand'])}</dd>"
        f"<dt>编号</dt><dd class=\"sku\">{html.escape(item['sku'])}</dd>"
        f"<dt>库存</dt><dd class=\"stock\" data-stock=\"{item['stock']}\">"
        f"{item['stock']} 件</dd></dl>"
        f"{desc_block}"
        f"</article>"
    )
    return _page_shell(item["title"], body, variant)


class SiteHandler(BaseHTTPRequestHandler):
    server_version = "CanaryPracticeSite/1.0"

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    @property
    def state(self) -> SiteState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, body: str, status: int = 200) -> None:
        raw = body.encode()
        with self.state.lock:
            self.state.request_count += 1
        etag = self.state.etag(self.path)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/":
            self._send(render_home(self.state))
            return
        if path == "/healthz":
            self._send("ok")
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "category":
            page_no = 1
            try:
                page_no = max(1, int(qs.get("page", ["1"])[0]))
            except ValueError:
                page_no = 1
            body = render_list(self.state, parts[1], page_no)
            if body is None:
                self._send("<h1>404</h1>", 404)
            else:
                self._send(body)
            return
        if len(parts) == 2 and parts[0] == "item":
            body = render_item(self.state, parts[1])
            if body is None:
                self._send("<h1>404</h1>", 404)
            else:
                self._send(body)
            return
        self._send("<h1>404</h1>", 404)


class PracticeSite:
    def __init__(self, state: SiteState | None = None, port: int = 0):
        self.state = state or SiteState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), SiteHandler)
        self.httpd.daemon_threads = True
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "PracticeSite":
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True, name="practice-site")
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=3)


def serve_forever(port: int = 7789) -> PracticeSite:
    site = PracticeSite(port=port).start()
    return site
