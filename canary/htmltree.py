"""A tiny, tolerant HTML tree built on the standard library.

Both parser processes share this reader but not the rule code.  The tree keeps
text nodes and tag attributes verbatim so list-row splitting (a row's title and
price living in two sibling divs) is observable rather than silently flattened.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


def norm_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None,
                 parent: "Node | None" = None):
        self.tag = tag.lower()
        self.attrs = attrs or {}
        self.children = []  # Node | str
        self.parent = parent

    # -- construction -------------------------------------------------
    def append(self, child) -> None:
        if isinstance(child, Node):
            child.parent = self
        self.children.append(child)

    # -- attributes ----------------------------------------------------
    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    def has_class(self, cls: str) -> bool:
        return cls in self.classes

    # -- traversal -----------------------------------------------------
    def iter(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None,
                 attrs: dict[str, str] | None = None, stop=None):
        out = []
        for node in self.iter():
            if node is self:
                continue
            if tag and node.tag != tag:
                continue
            if cls and cls not in node.classes:
                continue
            if attrs and any(node.attrs.get(k) != v for k, v in attrs.items()):
                continue
            out.append(node)
            if stop and stop(node):
                break
        return out

    def find(self, tag: str | None = None, cls: str | None = None,
             attrs: dict[str, str] | None = None):
        found = self.find_all(tag=tag, cls=cls, attrs=attrs)
        return found[0] if found else None

    def select(self, selector: str):
        """Very small selector subset: tag, .class, #id, tag.class, [attr]."""
        return [n for n in self.iter() if n is not self and n.matches(selector)]

    def select_one(self, selector: str):
        found = self.select(selector)
        return found[0] if found else None

    def matches(self, selector: str) -> bool:
        selector = selector.strip()
        if not selector:
            return False
        attr = None
        m = re.search(r"\[([a-zA-Z0-9_-]+)(?:=['\"]?([^'\"\]]+)['\"]?)?\]", selector)
        if m:
            attr = (m.group(1), m.group(2))
            selector = selector[: m.start()]
        tag = None
        cls = None
        ident = None
        if selector.startswith("#"):
            ident = selector[1:]
        elif "." in selector:
            tag, cls = selector.split(".", 1)
            tag = tag or None
        elif selector:
            tag = selector
        if tag and self.tag != tag.lower():
            return False
        if cls and cls not in self.classes:
            return False
        if ident and self.attrs.get("id") != ident:
            return False
        if attr:
            if attr[0] not in self.attrs:
                return False
            if attr[1] is not None and self.attrs.get(attr[0]) != attr[1]:
                return False
        return True

    # -- content -------------------------------------------------------
    def text_content(self) -> str:
        parts: list[str] = []

        def walk(node: "Node"):
            for child in node.children:
                if isinstance(child, Node):
                    if child.tag == "br":
                        parts.append("\n")
                    elif child.tag in ("div", "p", "li", "tr", "h1", "h2", "h3"):
                        parts.append(" ")
                        walk(child)
                        parts.append(" ")
                    else:
                        walk(child)
                else:
                    parts.append(child)

        walk(self)
        return norm_text("".join(parts))

    def raw_text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, Node):
                parts.append(child.raw_text())
            else:
                parts.append(child)
        return "".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        cls = ".".join(sorted(self.classes))
        return f"<Node {self.tag}{('.' + cls) if cls else ''}>"


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].append(node)
        if tag.lower() not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].append(data)


def parse_html(markup: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root
