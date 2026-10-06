"""Pestaña «Descubrir»: buscar dots en GitHub y añadirlos al catálogo con un clic."""
from __future__ import annotations

import threading
from datetime import date

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .. import discover, gitrepo  # noqa: E402
from .images import load_async  # noqa: E402
from .window import pill  # noqa: E402


def ago(iso: str) -> str:
    try:
        days = (date.today() - date.fromisoformat(iso)).days
    except ValueError:
        return ""
    if days <= 0:
        return "hoy"
    if days < 31:
        return f"hace {days} d"
    if days < 365:
        return f"hace {days // 30} meses"
    return f"hace {days // 365} años"


def kstars(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


class DiscoverView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.page = 1
        self.preset = "popular"
        self.loaded = False
        self.gen = 0

        bar = Gtk.Box(spacing=10, margin_start=24, margin_end=24, margin_top=12)
        bar.add_css_class("filter-bar")
        self.search = Gtk.SearchEntry(placeholder_text="Buscar dots en GitHub (p.ej. catppuccin, minimal, ags…)",
                                      hexpand=True)
        self.search.connect("activate", lambda *_: self.reload())
        self.search.connect("search-changed", lambda *_: self._debounce())
        bar.append(self.search)
        self.append(bar)

        chips = Adw.WrapBox(child_spacing=6, line_spacing=6, margin_start=24, margin_end=24, margin_top=8)
        self.toggles = {}
        first = None
        for key, label, _ in discover.PRESETS:
            t = Gtk.ToggleButton(label=label, group=first)
            t.add_css_class("pill")
            t.add_css_class("flat")
            t.connect("toggled", lambda b, k=key: b.get_active() and self._preset(k))
            first = first or t
            self.toggles[key] = t
            chips.append(t)
        self.append(chips)

        # Filtros: orden, estrellas mínimas, actividad y ocultar lo que ya tienes
        filters = Adw.WrapBox(child_spacing=8, line_spacing=6, margin_start=24, margin_end=24, margin_top=8)
        self.f_period = self._dropdown(filters, "Popular", [x[1] for x in discover.PERIODS])
        self.f_sort = self._dropdown(filters, "Orden", [x[1] for x in discover.SORTS])
        self.f_stars = self._dropdown(filters, "Estrellas", [x[1] for x in discover.STARS])
        self.f_act = self._dropdown(filters, "Actividad", [x[1] for x in discover.ACTIVITY])
        self.hide_known = Gtk.CheckButton(label="Ocultar los que ya tengo", valign=Gtk.Align.CENTER)
        self.hide_known.connect("toggled", lambda *_: self.flow.invalidate_filter())
        filters.append(self.hide_known)
        self.count = Gtk.Label(valign=Gtk.Align.CENTER)
        self.count.add_css_class("dim-label")
        self.count.add_css_class("caption")
        filters.append(self.count)
        self.append(filters)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                column_spacing=18, row_spacing=18, min_children_per_line=1,
                                max_children_per_line=6, valign=Gtk.Align.START,
                                margin_start=24, margin_end=24, margin_top=14, margin_bottom=12)
        self.flow.set_filter_func(lambda c: not (self.hide_known.get_active() and getattr(c, "known", False)))
        self.more = Gtk.Button(label="Cargar más", halign=Gtk.Align.CENTER, margin_bottom=24, visible=False)
        self.more.add_css_class("pill")
        self.more.connect("clicked", lambda *_: self._load(append=True))
        self.status = Adw.StatusPage(icon_name="system-search-symbolic", title="Descubre dots de Hyprland",
                                     description="Buscando en GitHub…", vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.status)
        box.append(self.flow)
        box.append(self.more)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(box)
        self.append(sw)
        self._timer = 0
        first.set_active(True)

    def _dropdown(self, parent, label: str, items: list[str]) -> Gtk.DropDown:
        box = Gtk.Box(spacing=6)
        l = Gtk.Label(label=label, valign=Gtk.Align.CENTER)
        l.add_css_class("dim-label")
        l.add_css_class("caption")
        box.append(l)
        dd = Gtk.DropDown.new_from_strings(items)
        dd.set_valign(Gtk.Align.CENTER)
        dd.connect("notify::selected", lambda *_: self.loaded and self.reload())
        box.append(dd)
        parent.append(box)
        return dd

    # ------------------------------------------------------------------
    def ensure_loaded(self):
        if not self.loaded:
            self.loaded = True
            self.reload()

    def _preset(self, key):
        self.preset = key
        if self.loaded:
            self.reload()

    def _debounce(self):
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(600, lambda: (setattr(self, "_timer", 0), self.reload(), False)[-1])

    def reload(self):
        self.page = 1
        self.flow.remove_all()
        self._load(append=False)

    def _load(self, append: bool):
        self.gen += 1
        gen, text, preset, page = self.gen, self.search.get_text(), self.preset, self.page if append else 1
        if not append:
            self.status.set_visible(True)
            self.status.set_description("Buscando en GitHub…")
        self.more.set_sensitive(False)

        sort = discover.SORTS[self.f_sort.get_selected()][0]
        stars = discover.STARS[self.f_stars.get_selected()][0]
        days = discover.ACTIVITY[self.f_act.get_selected()][0]
        period, plabel, _ = discover.PERIODS[self.f_period.get_selected()]
        trending = period != "all"
        # En modo tendencia el orden y la actividad no aplican (se ordena por estrellas ganadas).
        self.f_sort.set_sensitive(not trending)
        self.f_act.set_sensitive(not trending)
        if trending and not append:
            self.status.set_description(f"Calculando lo más popular de «{plabel.lower()}»… "
                                        "(historial de estrellas de ~60 repos; la primera vez tarda unos segundos)")

        def work():
            try:
                if trending:
                    res = discover.trending(text, preset, period, min_stars=stars)
                    GLib.idle_add(self._show_trending, gen, res, plabel)
                else:
                    res = discover.search(text, preset, page, sort=sort, min_stars=stars, active_days=days)
                    GLib.idle_add(self._show, gen, res, None)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._show, gen, None, e)

        threading.Thread(target=work, daemon=True).start()

    def _show(self, gen, res, err):
        if gen != self.gen:
            return False  # respuesta de una búsqueda anterior
        self.more.set_sensitive(True)
        if err:
            self.status.set_visible(True)
            self.status.set_description(f"No se pudo buscar: {err}")
            self.more.set_visible(False)
            return False
        self.status.set_visible(not res.items and self.page == 1)
        if not res.items and self.page == 1:
            self.status.set_description("Sin resultados. Prueba con otras palabras.")
        self.count.set_label(f"{res.total:,} repos".replace(",", "."))
        known = {gitrepo.normalize_url(d.repo).lower(): d for d in self.win.dots.values()}
        for r in res.items:
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(self._card(r, known.get(r.url.lower())))
            child.known = r.url.lower() in known
            self.flow.append(child)
        self.page += 1
        self.more.set_visible(res.has_more)
        return False

    def _show_trending(self, gen, res, plabel):
        if gen != self.gen:
            return False
        self.more.set_sensitive(True)
        self.more.set_visible(False)
        self.status.set_visible(not res)
        if not res:
            self.status.set_description("No hay datos de estrellas para este periodo.")
        self.count.set_label(f"{len(res)} repos · estrellas ganadas «{plabel.lower()}» (datos: OSS Insight)")
        known = {gitrepo.normalize_url(d.repo).lower(): d for d in self.win.dots.values()}
        for i, (r, g) in enumerate(res):
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(self._card(r, known.get(r.url.lower()), gained=g, rank=i + 1, plabel=plabel))
            child.known = r.url.lower() in known
            self.flow.append(child)
        return False

    def _card(self, r: discover.Repo, in_catalog, gained: int | None = None, rank: int = 0,
              plabel: str = "") -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("dot-card")
        card.set_size_request(300, -1)
        thumb = Gtk.Overlay()
        ph = Gtk.Box()
        ph.add_css_class("thumb-placeholder")
        ph.set_size_request(300, 150)
        thumb.set_child(ph)
        if gained is None:
            badge = Gtk.Label(label=f"★ {kstars(r.stars)}", halign=Gtk.Align.END, valign=Gtk.Align.START,
                              margin_top=10, margin_end=10)
            badge.set_tooltip_text(f"{r.stars:,} estrellas en GitHub".replace(",", "."))
        else:
            badge = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=10, margin_end=10,
                              justify=Gtk.Justification.CENTER)
            badge.set_markup(f"▲ +{kstars(gained)} ★\n<span size='small' weight='normal'>"
                             f"{kstars(r.stars)} en total</span>")
            badge.set_tooltip_text(f"+{gained} estrellas ({plabel.lower()}) · {r.stars} en total")
            badge.add_css_class("trend")
        badge.add_css_class("star-badge")
        rk = None
        if rank:
            rk = Gtk.Label(label=f"#{rank}", halign=Gtk.Align.START, valign=Gtk.Align.START,
                           margin_top=10, margin_start=10)
            rk.add_css_class("rank-badge")
            thumb.add_overlay(rk)
        thumb.add_overlay(badge)
        card.append(thumb)

        def done(tex, err, thumb=thumb, badge=badge, rk=rk):
            if tex is not None:
                pic = Gtk.Picture.new_for_paintable(tex)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.set_size_request(300, 150)
                pic.add_css_class("thumb")
                pic.set_overflow(Gtk.Overflow.HIDDEN)
                thumb.add_overlay(pic)
                for ov in (badge, rk):  # insignias siempre por encima de la imagen
                    if ov is not None:
                        thumb.remove_overlay(ov)
                        thumb.add_overlay(ov)
            return False

        load_async(r.preview_image, None, done)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=12, margin_bottom=14,
                       margin_start=16, margin_end=16)
        owner, name = r.full_name.split("/", 1)
        t = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        t.add_css_class("card-title")
        info.append(t)
        a = Gtk.Label(label=f"por {owner} · activo {ago(r.pushed)}" + (f" · {r.license}" if r.license else ""),
                      xalign=0,
                      ellipsize=Pango.EllipsizeMode.END)
        a.add_css_class("dim-label")
        a.add_css_class("caption")
        info.append(a)
        desc = Gtk.Label(label=r.description or " ", xalign=0, yalign=0, wrap=True, lines=2,
                         ellipsize=Pango.EllipsizeMode.END, max_width_chars=34, height_request=36)
        desc.set_tooltip_text(r.description or None)
        desc.add_css_class("caption")
        info.append(desc)
        tags = Adw.WrapBox(child_spacing=4, line_spacing=4, margin_top=4)
        for tp in [x for x in r.topics if x not in ("hyprland", "dotfiles", "linux")][:3]:
            tags.append(pill(tp))
        info.append(tags)
        acts = Gtk.Box(spacing=8, margin_top=8)
        if in_catalog:
            b = Gtk.Button(label="Ver en tu catálogo", hexpand=True)
            b.connect("clicked", lambda *_, d=in_catalog: self.win.open_detail(d))
        else:
            b = Gtk.Button(hexpand=True)
            b.set_child(Adw.ButtonContent(icon_name="dotdeck-add-symbolic", label="Añadir"))
            b.add_css_class("suggested-action")
            b.set_tooltip_text("Analizar el repo y añadirlo al catálogo (podrás revisarlo antes de guardar)")
            b.connect("clicked", lambda *_, u=r.url: self.win.open_editor(url=u))
        b.add_css_class("pill")
        acts.append(b)
        gh = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Abrir en GitHub")
        gh.add_css_class("flat")
        gh.add_css_class("circular")
        gh.connect("clicked", lambda *_, u=r.url: Gtk.UriLauncher.new(u).launch(self.win, None, None, None))
        acts.append(gh)
        info.append(acts)
        card.append(info)
        # Pulsar la tarjeta (fuera de los botones) abre la vista previa con README, capturas y vídeos.
        click = Gtk.GestureClick()
        click.connect("released", lambda g, n, x, y, r=r: self._open_preview(g, x, y, r))
        card.add_controller(click)
        card.set_cursor_from_name("pointer")
        card.set_tooltip_text("Ver README, capturas y vídeos")
        return card

    def _open_preview(self, gesture, x, y, repo):
        w = gesture.get_widget().pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not gesture.get_widget():
            if isinstance(w, Gtk.Button):
                return  # el clic era en un botón de la tarjeta
            w = w.get_parent()
        self.win.open_repo_preview(repo)
