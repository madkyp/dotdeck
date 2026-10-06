"""Página «Ver cambios»: qué trae una actualización antes de instalarla."""
from __future__ import annotations

import threading
from datetime import date

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import changes, engine, gitrepo  # noqa: E402
from .readme import ReadmeView, parse  # noqa: E402
from .window import pill  # noqa: E402

VISIBLE = 8
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def nice_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {MESES[d.month - 1]}"
    except ValueError:
        return iso


def num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


class ChangesPage(Adw.NavigationPage):
    def __init__(self, win, dot):
        super().__init__(title=f"Cambios de {dot.name}")
        self.win, self.dot = win, dot
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.gh = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Ver la comparación en GitHub",
                             sensitive=False)
        hb.pack_end(self.gh)
        self.upd = Gtk.Button(label="Actualizar", sensitive=False)
        self.upd.add_css_class("suggested-action")
        self.upd.connect("clicked", lambda *_: win.do_update(dot))
        hb.pack_end(self.upd)
        tv.add_top_bar(hb)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=18, margin_bottom=36,
                            margin_start=24, margin_end=24)
        clamp = Adw.Clamp(maximum_size=1000, tightening_threshold=700)
        clamp.set_child(self.body)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(clamp)
        tv.set_content(sw)
        self.set_child(tv)
        self.body.append(Adw.Spinner(width_request=36, height_request=36, margin_top=60))
        msg = Gtk.Label(label="Consultando qué ha cambiado…")
        msg.add_css_class("dim-label")
        self.body.append(msg)
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            st = engine.load_state(self.dot.id)
            if not st:
                raise changes.ChangesError("El dot no está instalado")
            rv = gitrepo.remote_version(self.dot)
            if rv["ref"] == st["version"]["ref"] and rv["commit"] == st["version"].get("commit"):
                raise changes.ChangesError(f"Ya tienes la última versión ({st['version']['label']}).")
            ch = changes.fetch(self.dot, st, rv)
            blocks = parse(ch.notes) if ch.notes else []
            GLib.idle_add(self._show, ch, blocks, None)
        except (changes.ChangesError, gitrepo.GitError) as e:
            GLib.idle_add(self._show, None, None, e)

    def _clear(self):
        while (c := self.body.get_first_child()) is not None:
            self.body.remove(c)

    def _show(self, ch, blocks, err):
        self._clear()
        if err:
            self.body.append(Adw.StatusPage(icon_name="emblem-ok-symbolic" if "última" in str(err)
                                            else "dialog-warning-symbolic",
                                            title="Sin cambios que mostrar", description=str(err)))
            return False
        self.upd.set_sensitive(True)
        if ch.url:
            self.gh.set_sensitive(True)
            self.gh.connect("clicked", lambda *_: Gtk.UriLauncher.new(ch.url).launch(self.win, None, None, None))

        # ---------- resumen
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("about-card")
        t = Gtk.Label(xalign=0, wrap=True)
        t.set_markup(f"<b>{GLib.markup_escape_text(ch.installed)}</b>  →  "
                     f"<b>{GLib.markup_escape_text(ch.latest)}</b>")
        t.add_css_class("title-3")
        card.append(t)
        stats = Adw.WrapBox(child_spacing=6, line_spacing=6)
        stats.append(pill(f"{num(ch.total)} commits", "update"))
        stats.append(pill(f"{num(ch.files)} archivos"))
        stats.append(pill(f"+{num(ch.additions)}", "installed"))
        stats.append(pill(f"−{num(ch.deletions)}", "danger"))
        if ch.first_date:
            stats.append(pill(f"del {nice_date(ch.first_date)} al {nice_date(ch.last_date)}"))
        card.append(stats)
        resume = [f"{len(items)} {title.split(' ', 1)[1].lower()}" for _, title, items in ch.grouped()
                  if _ in ("breaking", "feat", "fix", "perf")]
        if resume:
            r = Gtk.Label(label=" · ".join(resume), xalign=0, wrap=True)
            r.add_css_class("dim-label")
            card.append(r)
        self.body.append(card)

        # ---------- configuración afectada
        if ch.areas:
            self.body.append(self._title("Qué cambia en tu configuración",
                                         "Rutas de tu $HOME que tocan los archivos actualizados "
                                         "(se respaldan antes de actualizar)."))
            wb = Adw.WrapBox(child_spacing=6, line_spacing=6)
            for area, n in ch.areas[:24]:
                wb.append(pill(f"{area} · {n}"))
            self.body.append(wb)

        # ---------- notas del autor
        if blocks:
            g = Adw.PreferencesGroup(title="Notas del autor", description=f"Fuente: {ch.notes_source}")
            er = Adw.ExpanderRow(title="Novedades descritas por el autor",
                                 subtitle="Lo añadido a su registro de cambios en esta actualización")
            view = ReadmeView(blocks, None, "CHANGELOG.md", self.dot.repo)
            for m in ("top", "bottom", "start", "end"):
                getattr(view, f"set_margin_{m}")(16)
            er.add_row(view)
            er.set_expanded(len(ch.notes) < 6000)
            g.add(er)
            self.body.append(g)

        # ---------- commits por tipo
        for _key, title, items in ch.grouped():
            g = Adw.PreferencesGroup(title=f"{title} ({len(items)})")
            rows = [self._commit_row(c) for c in items]
            for r in rows[:VISIBLE]:
                g.add(r)
            if len(rows) > VISIBLE:
                more = Adw.ExpanderRow(title=f"Ver {len(rows) - VISIBLE} más")
                for r in rows[VISIBLE:]:
                    more.add_row(r)
                g.add(more)
            self.body.append(g)
        if ch.total > len(ch.commits) + 5:
            n = Gtk.Label(label=f"GitHub solo devuelve los últimos {len(ch.commits)} commits; "
                                "el resto está en la comparación completa (botón del navegador).",
                          wrap=True, xalign=0)
            n.add_css_class("dim-label")
            n.add_css_class("caption")
            self.body.append(n)
        return False

    def _title(self, title: str, sub: str) -> Gtk.Widget:
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("section-title")
        b.append(t)
        s = Gtk.Label(label=sub, xalign=0, wrap=True)
        s.add_css_class("dim-label")
        s.add_css_class("caption")
        b.append(s)
        return b

    def _commit_row(self, c: changes.Commit) -> Adw.ActionRow:
        sub = " · ".join(x for x in (c.scope, c.author, nice_date(c.date)) if x)
        r = Adw.ActionRow(title=GLib.markup_escape_text(c.title[:1].upper() + c.title[1:]),
                          subtitle=GLib.markup_escape_text(sub), activatable=bool(c.url))
        r.set_title_lines(2)
        sha = Gtk.Label(label=c.sha[:7], valign=Gtk.Align.CENTER)
        sha.add_css_class("monospace")
        sha.add_css_class("dim-label")
        sha.add_css_class("caption")
        r.add_suffix(sha)
        if c.url:
            r.set_tooltip_text("Abrir el commit en GitHub")
            r.connect("activated", lambda *_: Gtk.UriLauncher.new(c.url).launch(self.win, None, None, None))
        return r
