"""A tiny HTML tree on top of ``html.parser``, enough for job pages.

The real pages are messy, so this is deliberately small: build a tree, then query
it by tag, class and attribute, and turn a subtree into plain text with line
breaks preserved for paragraphs and list items. No third-party parser, no XPath.
"""

from __future__ import annotations

from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "h1", "h2", "h3", "h4",
         "h5", "h6", "section", "article", "header", "footer", "table", "blockquote"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []
        self.parent = parent

    # -- attributes -------------------------------------------------------- #
    def get(self, name: str, default=None):
        return self.attrs.get(name, default)

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def has_class(self, name: str) -> bool:
        return name in self.classes()

    # -- traversal --------------------------------------------------------- #
    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find_all(self, tag: str | None = None, class_: str | None = None,
                 attrs: dict | None = None) -> list["Node"]:
        found = []
        for node in self.walk():
            if tag and node.tag != tag:
                continue
            if class_ and not node.has_class(class_):
                continue
            if attrs and any(node.get(k) != v for k, v in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, tag: str | None = None, class_: str | None = None,
             attrs: dict | None = None) -> "Node | None":
        nodes = self.find_all(tag, class_, attrs)
        return nodes[0] if nodes else None

    def first_text(self, tag: str | None = None, class_: str | None = None) -> str:
        node = self.find(tag, class_)
        return node.text() if node else ""

    # -- text -------------------------------------------------------------- #
    def text(self) -> str:
        parts: list[str] = []
        self._collect(parts)
        return " ".join("".join(parts).split())

    def _collect(self, parts: list[str]) -> None:
        for child in self.children:
            if isinstance(child, Node):
                child._collect(parts)
            else:
                parts.append(str(child))

    def to_text(self) -> str:
        """Plain text that keeps paragraph and list breaks."""
        raw = self._block_text()
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1] != "":
                out.append("")
        return "\n".join(out).strip()

    def _block_text(self) -> str:
        pieces: list[str] = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == "br":
                    pieces.append("\n")
                elif child.tag in BLOCK:
                    pieces.append("\n")
                    pieces.append(child._block_text())
                    pieces.append("\n")
                else:
                    pieces.append(child._block_text())
            else:
                pieces.append(str(child))
        return "".join(pieces)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {sorted(self.classes())[:2]}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # stray close tag: ignore

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


def to_text(html: str) -> str:
    return parse(html).to_text()
