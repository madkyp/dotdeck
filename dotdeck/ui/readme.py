"""Renderizado nativo de README (Markdown/HTML de GitHub → widgets GTK).

Markdown → HTML con python-markdown, y el HTML (incluido el HTML crudo típico
de los README: <div align>, <img>, <details>…) se convierte en una lista de
bloques que luego se transforma en widgets en el hilo principal.
"""
from __future__ import annotations

import html
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .images import AsyncPicture, is_badge  # noqa: E402

try:
    import markdown as _md
except ImportError:  # pragma: no cover
    _md = None

INLINE = {"b": "<b>", "strong": "<b>", "i": "<i>", "em": "<i>", "s": "<s>", "del": "<s>",
          "code": "<span font_family='monospace' background='#8080802e'>",
          "kbd": "<span font_family='monospace' background='#8080802e'>", "u": "<u>"}
CLOSE = {"b": "</b>", "strong": "</b>", "i": "</i>", "em": "</i>", "s": "</s>", "del": "</s>",
         "code": "</span>", "kbd": "</span>", "u": "</u>", "a": "</a>"}
MEDIA_URL = re.compile(r"https://github\.com/user-attachments/assets/[0-9a-f-]{20,}"
                       r"|https?://\S+\.(?:mp4|webm|mov|m4v)(?:\?\S*)?", re.I)
ALERTS = {"NOTE": "ℹ Nota", "TIP": "💡 Consejo", "IMPORTANT": "❗ Importante",
          "WARNING": "⚠ Atención", "CAUTION": "⛔ Precaución"}
BLOCK_BREAK = {"p", "div", "center", "section", "article", "picture", "header", "footer", "dl", "dt", "dd",
               "figure", "figcaption", "nav", "main", "aside"}


def to_html(text: str, is_markdown: bool) -> str:
    if not is_markdown:
        return "<pre>" + html.escape(text) + "</pre>"
    if _md is None:
        return "<pre>" + html.escape(text) + "</pre>"
    # GitHub permite listas sin línea en blanco previa; python-markdown no.
    text = re.sub(r"(?m)^([^\n*\-+\d\s>][^\n]*)\n(\s*[-*+] )", r"\1\n\n\2", text)
    # GitHub procesa Markdown dentro de <div>/<details>; python-markdown solo con markdown="1".
    text = re.sub(r"<(div|center|details|section|article)(?![^>]*markdown=)([^>]*)>",
                  r'<\1\2 markdown="1">', text, flags=re.I)
    return _md.markdown(text, extensions=["fenced_code", "tables", "sane_lists", "md_in_html"],
                        output_format="html")


class Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: list = []
        self.targets: list[list] = [self.root]
        self.buf: list[str] = []
        self.open: list[str] = []
        self.mode: list[str] = []         # heading | li | cell | summary | quote
        self.heading = 0
        self.pre = False
        self.pre_buf: list[str] = []
        self.lists: list[dict] = []
        self.table: dict | None = None
        self.details: list[dict] = []
        self.skip = 0                     # dentro de <script>/<style>/<svg>

    # ------------- helpers
    @property
    def out(self) -> list:
        return self.targets[-1]

    def _markup(self) -> str:
        s = "".join(self.buf) + "".join(CLOSE.get(t, "") for t in reversed(self.open))
        self.buf, self.open = [], []
        s = re.sub(r"[ \t\r\n]+", " ", s).replace(" \x00 ", "\n").replace("\x00", "\n").strip()
        return s

    def flush(self) -> None:
        m = self._markup()
        if not m:
            return
        mode = self.mode[-1] if self.mode else ""
        if mode == "heading":
            self.out.append(("heading", self.heading, m))
        elif mode == "li" and self.lists:
            items = self.lists[-1]["items"]
            depth = len(self.lists) - 1
            if items and items[-1][2] and items[-1][1] == depth:
                prev = items[-1][0]
                items[-1] = ((prev + "\n" + m) if prev else m, depth, True)
            else:
                items.append((m, depth, True))
        elif mode == "cell" and self.table is not None and self.table["rows"]:
            self.table["rows"][-1][-1] += (" " if self.table["rows"][-1][-1] else "") + m
        elif mode == "summary" and self.details:
            self.details[-1]["summary"] += m
        elif mode == "quote":
            m = re.sub(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*",
                       lambda x: f"<b>{ALERTS[x.group(1)]}</b>\n", m)
            self.out.append(("quote", m))
        else:
            url = plain(m)
            if MEDIA_URL.fullmatch(url):
                self.out.append(("media", url))  # GitHub incrusta así los vídeos
            else:
                self.out.append(("para", m))

    def _pop_mode(self, m: str) -> None:
        if m in self.mode:
            while self.mode and self.mode.pop() != m:
                pass

    # ------------- parser
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "svg"):
            self.skip += 1
            return
        if self.skip:
            return
        if self.pre:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush()
            self.heading = int(tag[1])
            self.mode.append("heading")
        elif tag in BLOCK_BREAK:
            self.flush()
        elif tag == "br":
            self.buf.append("\x00")
        elif tag in INLINE:
            self.buf.append(INLINE[tag])
            self.open.append(tag)
        elif tag == "a":
            href = a.get("href") or ""
            self.buf.append(f"<a href=\"{GLib.markup_escape_text(href)}\">" if href else "<a href=\"#\">")
            self.open.append("a")
        elif tag == "img":
            src = a.get("src") or ""
            if not src or is_badge(src):
                return
            if self.mode and self.mode[-1] in ("cell", "li"):
                alt = a.get("alt") or "imagen"
                self.buf.append(f"[{GLib.markup_escape_text(alt)}]")
                return
            self.flush()
            if self.out and self.out[-1][0] == "images":
                self.out[-1][1].append(src)
            else:
                self.out.append(("images", [src]))
        elif tag == "video":
            src = a.get("src") or ""
            if src:
                self.flush()
                self.out.append(("media", src))
        elif tag == "source" and (a.get("type") or "").startswith("video") and a.get("src"):
            self.flush()
            self.out.append(("media", a["src"]))
        elif tag == "source":
            src = (a.get("srcset") or "").split(" ")[0]
            if src and not is_badge(src) and "prefers-color-scheme: light" not in (a.get("media") or ""):
                self.flush()
                self.out.append(("images", [src]))
        elif tag in ("ul", "ol"):
            self.flush()
            if not self.lists:
                blk = ("list", tag == "ol", [])
                self.out.append(blk)
                items = blk[2]
            else:
                items = self.lists[-1]["items"]
            self.lists.append({"items": items})
        elif tag == "li":
            self.flush()
            self.mode.append("li")
            if self.lists:
                self.lists[-1]["items"].append(("", len(self.lists) - 1, True))
        elif tag == "pre":
            self.flush()
            self.pre, self.pre_buf = True, []
        elif tag == "blockquote":
            self.flush()
            self.mode.append("quote")
        elif tag == "table":
            self.flush()
            self.table = {"rows": [], "head": False}
        elif tag == "tr" and self.table is not None:
            self.table["rows"].append([])
        elif tag in ("td", "th") and self.table is not None:
            self.flush()
            if not self.table["rows"]:
                self.table["rows"].append([])
            self.table["rows"][-1].append("")
            if tag == "th":
                self.table["head"] = True
            self.mode.append("cell")
        elif tag == "hr":
            self.flush()
            self.out.append(("hr",))
        elif tag == "details":
            self.flush()
            d = {"summary": "", "blocks": []}
            self.details.append(d)
            self.out.append(("details", d))
            self.targets.append(d["blocks"])
        elif tag == "summary":
            self.flush()
            self.mode.append("summary")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if self.pre:
            if tag == "pre":
                self.out.append(("code", "".join(self.pre_buf).rstrip("\n")))
                self.pre = False
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush()
            self._pop_mode("heading")
        elif tag in BLOCK_BREAK:
            self.flush()
        elif tag in INLINE or tag == "a":
            if tag in self.open:
                while self.open:
                    t = self.open.pop()
                    self.buf.append(CLOSE.get(t, ""))
                    if t == tag:
                        break
        elif tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
        elif tag == "li":
            self.flush()
            self._pop_mode("li")
        elif tag == "blockquote":
            self.flush()
            self._pop_mode("quote")
        elif tag in ("td", "th"):
            self.flush()
            self._pop_mode("cell")
        elif tag == "table" and self.table is not None:
            self.flush()
            self.out.append(("table", self.table["rows"], self.table["head"]))
            self.table = None
        elif tag == "summary":
            self.flush()
            self._pop_mode("summary")
        elif tag == "details" and self.details:
            self.flush()
            self.details.pop()
            if len(self.targets) > 1:
                self.targets.pop()

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self.pre_buf.append(data)
            return
        self.buf.append(GLib.markup_escape_text(data))

    def close(self):
        super().close()
        self.flush()


def strip_links(markup: str) -> str:
    """<a> es extensión de GtkLabel, no de Pango: se quita solo para validar."""
    return re.sub(r"</?a(\s[^>]*)?>", "", markup)


def parse(text: str, is_markdown: bool = True) -> list:
    p = Blocks()
    p.feed(to_html(text, is_markdown))
    p.close()
    return p.root


def media_list(blocks: list) -> list[str]:
    """Vídeos incrustados (URL suelta en su línea o <video>); las <img> nunca cuentan como vídeo."""
    return [b[1] for b in _walk(blocks) if b[0] == "media"]


def image_list(blocks: list) -> list[str]:
    out = []
    for b in blocks:
        if b[0] == "images":
            out += b[1]
        elif b[0] == "details":
            out += image_list(b[1]["blocks"])
    return out


# ------------------------------------------------------------------ widgets
class ReadmeView(Gtk.Box):
    def __init__(self, blocks: list, clone: Path | None, readme_path: str, repo_url: str,
                 resolve_image=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("readme")
        self.clone, self.base = clone, posixpath.dirname(readme_path)
        self.repo_url = repo_url.rstrip("/")
        self.resolve_image = resolve_image
        for b in blocks:
            w = self._block(b)
            if w is not None:
                self.append(w)

    def _label(self, markup: str, css: str | None = None) -> Gtk.Label:
        lbl = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
                        focusable=False)
        try:
            Pango.parse_markup(strip_links(markup), -1, "\0")
            lbl.set_markup(markup)
        except GLib.Error:
            lbl.set_text(re.sub(r"<[^>]+>", "", html.unescape(markup)))
        lbl.connect("activate-link", self._on_link)
        if css:
            lbl.add_css_class(css)
        return lbl

    def _on_link(self, label, uri):
        if uri.startswith("#"):
            return True
        if not re.match(r"^[a-z]+:", uri):
            uri = f"{self.repo_url}/blob/HEAD/{posixpath.normpath(posixpath.join(self.base, uri))}"
        Gtk.UriLauncher.new(uri).launch(label.get_root(), None, None, None)
        return True

    def _src(self, src: str) -> str:
        if self.resolve_image:
            return self.resolve_image(src)
        return src

    def _block(self, b):
        kind = b[0]
        if kind == "heading":
            css = {1: "title-1", 2: "title-2", 3: "title-3"}.get(b[1], "title-4")
            lbl = self._label(b[2], css)
            lbl.set_margin_top(14 if b[1] <= 2 else 8)
            return lbl
        if kind == "para":
            return self._label(b[1])
        if kind == "quote":
            lbl = self._label(b[1])
            lbl.add_css_class("md-quote")
            return lbl
        if kind == "code":
            lbl = Gtk.Label(label=b[1], xalign=0, selectable=True)
            lbl.add_css_class("monospace")
            sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
            sw.set_child(lbl)
            sw.add_css_class("code-block")
            return sw
        if kind == "hr":
            return Gtk.Separator(margin_top=6, margin_bottom=6)
        if kind == "list":
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            n = 0
            for markup, depth, _ in b[2]:
                if not markup:
                    continue
                n += 1
                row = Gtk.Box(spacing=8, margin_start=18 * depth)
                bullet = Gtk.Label(label=f"{n}." if b[1] and depth == 0 else "•", yalign=0)
                bullet.add_css_class("accent")
                row.append(bullet)
                lbl = self._label(markup)
                lbl.set_hexpand(True)
                row.append(lbl)
                box.append(row)
            return box
        if kind == "images":
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                               column_spacing=8, row_spacing=8, homogeneous=False)
            many = len(b[1]) > 1
            for src in b[1][:12]:
                flow.append(AsyncPicture(self._src(src), self.clone, max_w=360 if many else 760,
                                         max_h=300 if many else 460, allow_video=True))
            return flow
        if kind == "media":
            return AsyncPicture(self._src(b[1]), self.clone, max_w=760, max_h=420, allow_video=True)
        if kind == "table":
            # Sin ScrolledWindow: dentro de uno horizontal GTK da a las etiquetas su ancho mínimo,
            # el texto se parte letra a letra y la tabla mide miles de px (huecos en blanco).
            grid = Gtk.Grid(column_spacing=18, row_spacing=8, hexpand=True)
            grid.add_css_class("md-table")
            for r, row in enumerate(b[1]):
                for c, cell in enumerate(row):
                    lbl = self._label(f"<b>{cell}</b>" if r == 0 and b[2] else cell)
                    lbl.set_width_chars(min(14, max(6, len(re.sub(r"<[^>]+>", "", cell)))))
                    lbl.set_max_width_chars(50)
                    lbl.set_hexpand(c == len(row) - 1)
                    lbl.set_yalign(0)
                    grid.attach(lbl, c, r, 1, 1)
            return grid
        if kind == "details":
            d = b[1]
            exp = Gtk.Expander()
            exp.set_label_widget(self._label(d["summary"] or "Detalles"))
            inner = ReadmeView(d["blocks"], self.clone, posixpath.join(self.base, "x"), self.repo_url,
                               self.resolve_image)
            inner.set_margin_start(12)
            inner.set_margin_top(6)
            exp.set_child(inner)
            return exp
        return None


# ------------------------------------------------------------------ resumen elegante
FEATURE_TITLES = re.compile(r"(?i)feature|what.?s (in|inside|included)|highlights|overview|includes|"
                            r"caracter|funcion|componentes|components|modules|widgets")
TOC = re.compile(r"(?i)^(table of contents|contents|índice|indice|toc)$")
ABOUTISH = re.compile(r"(?i)about|what.?is|overview|introduc|descrip|acerca|qué es|que es|summary")
INSTALLISH = re.compile(r"(?i)install|requirement|depend|prerequis|credit|licen|contribut|faq|uninstall|updat")
SKIP_INTRO = re.compile(r"(?i)^(\[|<a |star|⭐|made with|built with)")


def plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).strip()


def is_prose(markup: str) -> bool:
    """¿Párrafo de presentación? Texto real (no menús de enlaces, badges ni pasos de instalación)."""
    t = plain(markup)
    links = "".join(plain(x) for x in re.findall(r"<a [^>]*>(.*?)</a>", markup, re.S))
    prose = len(t) - len(links)
    return (len(t) > 40 and prose >= 40 and prose / max(len(t), 1) > 0.6
            and not SKIP_INTRO.match(t) and "](" not in t and "![" not in t
            and not t.startswith("http") and t.count("|") < 2
            and not re.search(r"(?i)install|check this out|see (the|our)|click|nixos|support|^note|disclaimer", t[:90]))


def first_sentence(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"(?<=[.!?;])\s", text)
    s = text[:m.start()] if m and m.start() > 25 else text
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:—–-") + "…"
    return s


def _walk(blocks):
    for b in blocks:
        yield b
        if b[0] == "details":
            yield from _walk(b[1]["blocks"])


def summarize(blocks: list) -> dict:
    """README → {intro: [markup], features: [(título, frase)], sections: [(título, bloques, vista previa)]}."""
    levels = [b[1] for b in blocks if b[0] == "heading" and b[1] >= 2]
    lvl = min(levels) if levels else 99
    intro: list[str] = []
    sections: list[list] = []
    cur = None
    for b in blocks:
        if b[0] == "heading" and b[1] <= lvl:
            if b[1] < lvl and cur is None:
                continue  # título principal (h1)
            cur = [plain(b[2]), [], ""]
            sections.append(cur)
            continue
        if cur is None:
            if b[0] == "para":
                t = plain(b[1])
                if is_prose(b[1]) and len(intro) < 3:
                    intro.append(b[1])
            continue
        cur[1].append(b)
        if not cur[2]:
            for x in _walk([b]):
                if x[0] in ("para", "quote") and len(plain(x[1])) > 20:
                    cur[2] = first_sentence(plain(x[1]), 110)
                    break
    sections = [s for s in sections if s[1] and not TOC.search(s[0])]
    if not intro:
        for title, bs, _ in sections:
            if not ABOUTISH.search(title):
                continue
            for b in bs:
                if b[0] == "para" and is_prose(b[1]):
                    intro.append(b[1])
                    break
            if intro:
                break

    def bold_items(bs):
        out = []
        for b in _walk(bs):
            if b[0] != "list":
                continue
            for markup, depth, _ in b[2]:
                m = re.match(r"^\s*<b>(.{2,60}?)</b>\s*[—–:\-]*\s*(.*)$", markup, re.S)
                if depth == 0 and m and len(plain(m.group(2))) > 10:
                    out.append((plain(m.group(1)).rstrip(":"), first_sentence(plain(m.group(2)))))
        return out

    feats: list[tuple[str, str]] = []
    for title, bs, _ in sections:
        if FEATURE_TITLES.search(title):
            feats += bold_items(bs)
    if len(feats) < 3:
        for _, bs, _ in sections:
            feats += [f for f in bold_items(bs) if f not in feats]
            if len(feats) >= 6:
                break
    seen, uniq = set(), []
    for t, d in feats:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append((t, d))
    return {"intro": intro, "features": uniq[:12], "sections": [tuple(s) for s in sections]}
