"""Pantalla de detalle: galería, README renderizado, información y acciones."""
from __future__ import annotations

import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .. import catalog, config, deps, engine, gitrepo  # noqa: E402
from .bridge import Operation, alert  # noqa: E402
from .images import AsyncPicture  # noqa: E402
from . import readme  # noqa: E402
from .readme import ReadmeView, strip_links  # noqa: E402
from .window import pill, resolve_image  # noqa: E402

TRACK = {"release": "releases de GitHub", "tag": "tags de git", "commit": "commits de la rama"}


class DetailPage(Adw.NavigationPage):
    def __init__(self, win, dot: catalog.Dot, preview=None):
        """``preview``: discover.Repo → vista previa de un repo que aún no está en el catálogo."""
        super().__init__(title=dot.name, tag=f"dot-{dot.id}")
        self.win, self.dot, self.preview = win, dot, preview
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        repo = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Abrir el repositorio")
        repo.connect("clicked", lambda *_: Gtk.UriLauncher.new(dot.repo).launch(win, None, None, None))
        hb.pack_end(repo)
        tv.add_top_bar(hb)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=18, margin_bottom=36,
                            margin_start=24, margin_end=24)
        clamp = Adw.Clamp(maximum_size=1000, tightening_threshold=700)
        clamp.set_child(self.body)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(clamp)
        self.sw = sw
        tv.set_content(sw)
        # Barra fija de acciones: sustituye a copiar los comandos del README.
        # WrapBox: en ventanas estrechas los botones bajan de línea en vez de ensanchar la página
        self.actionbar = Adw.WrapBox(child_spacing=10, line_spacing=6, align=0.5, margin_top=8, margin_bottom=8,
                                     margin_start=8, margin_end=8)
        self.b_install = self._abtn("folder-download-symbolic", "Instalar", lambda: win.do_install(self.dot))
        self.b_update = self._abtn("software-update-available-symbolic", "Actualizar",
                                   lambda: win.do_update(self.dot))
        self.b_changes = self._abtn("view-list-bullet-symbolic", "Ver cambios", lambda: win.open_changes(self.dot))
        self.b_reload = self._abtn("system-reboot-symbolic", "Recargar Hyprland", win.do_reload)
        for b in (self.b_install, self.b_update, self.b_changes, self.b_reload):
            self.actionbar.append(b)
        bar = Gtk.Box()
        bar.add_css_class("toolbar")
        bar.append(self.actionbar)
        self.actionbar.set_hexpand(True)
        tv.add_bottom_bar(bar)
        bar.set_visible(preview is None)  # en vista previa no hay nada que instalar todavía
        self.set_child(tv)
        self.connect("showing", lambda *_: self.build())

    def _abtn(self, icon: str, label: str, cb) -> Gtk.Button:
        b = Gtk.Button()
        b.set_child(Adw.ButtonContent(icon_name=icon, label=label))
        b.add_css_class("pill")
        b.connect("clicked", lambda *_: cb())
        return b

    def _update_actionbar(self, st, upd):
        self.b_install.set_sensitive(st is None)
        self.b_install.set_tooltip_text("Ya está instalado" if st else "Instalar con backup previo (pide sudo)")
        self.b_update.set_sensitive(st is not None)
        self.b_update.set_tooltip_text("No está instalado" if st is None else
                                       (f"Actualizar a {upd['latest']} (pide sudo)" if upd
                                        else "Buscar e instalar la última versión (pide sudo)"))
        self.b_changes.set_visible(bool(st and upd))
        self.b_changes.set_tooltip_text("Ver qué trae la actualización antes de instalarla")
        for b, on in ((self.b_install, st is None), (self.b_update, bool(upd))):
            (b.add_css_class if on else b.remove_css_class)("suggested-action")

    # ------------------------------------------------------------------
    def build(self):
        while (c := self.body.get_first_child()) is not None:
            self.body.remove(c)
        dot, win = self.dot, self.win
        st = win.states.get(dot.id)
        upd = win.upd.get("available", {}).get(dot.id)

        if not self.preview:
            self._update_actionbar(st, upd)

        # ---------- hero
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hero.add_css_class("hero")
        t = Gtk.Label(label=dot.name, xalign=0, wrap=True)
        t.add_css_class("hero-title")
        hero.append(t)
        sub = Gtk.Label(xalign=0, wrap=True, use_markup=True,
                        label=f"por <b>{GLib.markup_escape_text(dot.author or '—')}</b> · "
                              f"<a href=\"{GLib.markup_escape_text(dot.repo)}\">"
                              f"{GLib.markup_escape_text(dot.repo.split('://')[-1])}</a>")
        sub.add_css_class("dim-label")
        hero.append(sub)
        if dot.description:
            d = Gtk.Label(label=dot.description, xalign=0, wrap=True)
            hero.append(d)
        # WrapBox: las etiquetas saltan de línea en vez de ensanchar toda la página
        pills = Adw.WrapBox(child_spacing=6, line_spacing=6, margin_top=4)
        if self.preview:
            self._preview_hero(hero, pills)
            self.body.append(hero)
            self._preview_body()
            return
        if upd:
            pills.append(pill(f"↑ {upd['latest']} disponible", "update"))
        if st and st.get("external"):
            pills.append(pill(f"✓ Instalado en {config.contract(st['deploy'])} · {st['version']['label']}",
                              "installed"))
        elif st:
            pills.append(pill(f"✓ Instalado {st['version']['label']}", "installed"))
        else:
            pills.append(pill("No instalado"))
        if dot.example:
            pills.append(pill("Ejemplo temporal", "example"))
        elif dot.origin == "user":
            pills.append(pill("Añadido por ti", "user"))
        else:
            pills.append(pill("Lista inicial"))
        if dot.license:
            pills.append(pill(dot.license))
        if dot.hypr_format:
            pills.append(pill(f"hyprland.{dot.hypr_format}"))
        hero.append(pills)

        # ---------- acciones
        actions = Gtk.Box(spacing=10, margin_top=10)
        # Instalar/Actualizar están en la barra fija inferior; aquí solo lo secundario.
        if st:
            u = Gtk.Button(label="Dejar de seguir" if st.get("external") else "Desinstalar")
            u.add_css_class("destructive-action")
            u.add_css_class("pill")
            u.connect("clicked", lambda *_: win.do_uninstall(dot.id))
            actions.append(u)
        e = Gtk.Button(label="Editar")
        e.add_css_class("pill")
        e.set_tooltip_text("Editar la definición" + ("" if dot.origin == "user"
                                                     else " (se guardará como copia tuya)"))
        e.connect("clicked", lambda *_: win.open_editor(dot))
        actions.append(e)
        if dot.origin == "user":
            x = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Eliminar del catálogo")
            x.add_css_class("flat")
            x.connect("clicked", lambda *_: self._delete())
            actions.append(x)
        hero.append(actions)
        self.body.append(hero)

        # ---------- avisos
        if dot.warnings or st and st.get("unprotected"):
            wb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            wb.add_css_class("warn-box")
            for w in dot.warnings:
                wb.append(Gtk.Label(label=f"⚠  {w}", xalign=0, wrap=True))
            if st and st.get("unprotected"):
                wb.append(Gtk.Label(xalign=0, wrap=True, label="⚠  El instalador cambió rutas fuera del backup: "
                                    + ", ".join(config.contract(p) for p in st["unprotected"][:6])))
            self.body.append(wb)

        # ---------- galería (se rellena al cargar la previsualización)
        self.gallery_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.body.append(self.gallery_slot)

        # ---------- resumen (Acerca de + Lo más destacado), se rellena al cargar el README
        self.summary_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.summary_slot.append(Adw.Spinner(width_request=32, height_request=32, margin_top=12))
        self.body.append(self.summary_slot)

        # ---------- info
        self.body.append(self._info(st))

        # ---------- documentación completa, plegada por secciones
        self.docs_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.body.append(self.docs_slot)
        win.previews.get(dot, self._loaded)
        GLib.idle_add(lambda: (self.sw.get_vadjustment().set_value(0), False)[-1])

    # ------------------------------------------------------------------ vista previa (Descubrir)
    def _preview_hero(self, hero, pills):
        from .discover_view import ago, kstars
        r = self.preview
        st = pill(f"★ {kstars(r.stars)}")
        st.add_css_class("star-badge")
        pills.append(st)
        pills.append(pill(f"activo {ago(r.pushed)}"))
        if r.license:
            pills.append(pill(r.license))
        for tp in [x for x in r.topics if x not in ("hyprland",)][:6]:
            pills.append(pill(tp))
        hero.append(pills)
        known = next((d for d in self.win.dots.values()
                      if gitrepo.normalize_url(d.repo).lower() == r.url.lower()), None)
        actions = Gtk.Box(spacing=10, margin_top=10)
        if known:
            b = Gtk.Button(label="Ver en tu catálogo")
            b.connect("clicked", lambda *_: self.win.open_detail(known))
        else:
            b = Gtk.Button()
            b.set_child(Adw.ButtonContent(icon_name="dotdeck-add-symbolic", label="Añadir al catálogo"))
            b.add_css_class("suggested-action")
            b.set_tooltip_text("Analizar el repo y añadirlo (podrás revisar la definición antes de guardar)")
            b.connect("clicked", lambda *_: self.win.open_editor(url=r.url))
        b.add_css_class("pill")
        actions.append(b)
        gh = Gtk.Button()
        gh.set_child(Adw.ButtonContent(icon_name="web-browser-symbolic", label="Abrir en GitHub"))
        gh.add_css_class("pill")
        gh.connect("clicked", lambda *_: Gtk.UriLauncher.new(r.url).launch(self.win, None, None, None))
        actions.append(gh)
        hero.append(actions)
        note = Gtk.Label(xalign=0, wrap=True, label="Vista previa: aún no está en tu catálogo. Al añadirlo, "
                         "DotDeck analizará cómo se instala y qué rutas toca.")
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        hero.append(note)

    def _preview_body(self):
        self.gallery_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.body.append(self.gallery_slot)
        self.summary_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        sp = Adw.Spinner(width_request=32, height_request=32, margin_top=12)
        self.summary_slot.append(sp)
        msg = Gtk.Label(label="Descargando el README y las capturas…")
        msg.add_css_class("dim-label")
        self.summary_slot.append(msg)
        self.body.append(self.summary_slot)
        self.docs_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.body.append(self.docs_slot)
        self.win.previews.get(self.dot, self._loaded)
        GLib.idle_add(lambda: (self.sw.get_vadjustment().set_value(0), False)[-1])

    def _loaded(self, res):
        for slot in (self.summary_slot, self.docs_slot):
            while (c := slot.get_first_child()) is not None:
                slot.remove(c)
        if res.get("error"):
            self.summary_slot.append(Gtk.Label(label=f"No se pudo cargar la previsualización: {res['error']}",
                                               wrap=True, xalign=0))
            return False
        if res["blocks"]:
            self._summary(res)
        # Galería: portada, luego vídeos de preview (bucle, sin sonido), luego capturas.
        imgs = res.get("images", [])
        if self.dot.cover:
            imgs = imgs[:1] + res.get("videos", []) + imgs[1:]
        else:
            imgs = res.get("videos", []) + imgs
        imgs = imgs[:12]
        if imgs:
            car = Adw.Carousel(spacing=16, allow_scroll_wheel=False, height_request=430)
            car.add_css_class("gallery")
            for src in imgs:
                pic = AsyncPicture(src, res["clone"], max_w=940, max_h=410, css=None,
                                   allow_video=True, autoplay=True,
                                   on_fail=lambda w, c=car: self._gallery_fail(c, w))
                pic.set_halign(Gtk.Align.CENTER)
                pic.set_valign(Gtk.Align.CENTER)
                pic.set_hexpand(True)
                car.append(pic)
            nav = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
            prev = Gtk.Button(icon_name="go-previous-symbolic")
            nxt = Gtk.Button(icon_name="go-next-symbolic")
            for b in (prev, nxt):
                b.add_css_class("circular")
                b.add_css_class("flat")
            prev.connect("clicked", lambda *_: self._step(car, -1))
            nxt.connect("clicked", lambda *_: self._step(car, 1))
            nav.append(prev)
            nav.append(Adw.CarouselIndicatorDots(carousel=car))
            nav.append(nxt)
            self.gallery_slot.append(car)
            self.gallery_slot.append(nav)
        return False

    def _summary(self, res):
        """Presentación limpia del README: resumen, tarjetas de funciones y secciones plegadas."""
        dot = self.dot
        sm = readme.summarize(res["blocks"])
        base = res["readme"].rsplit("/", 1)[0] if "/" in res["readme"] else "."

        if dot.about:
            sm["intro"] = [GLib.markup_escape_text(p.strip()) for p in dot.about.split("\n\n") if p.strip()]
        if sm["intro"]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            card.add_css_class("about-card")
            t = Gtk.Label(label="Acerca de", xalign=0)
            t.add_css_class("section-title")
            card.append(t)
            for m in sm["intro"]:
                lbl = Gtk.Label(xalign=0, wrap=True, focusable=False)
                lbl.set_markup(m if _valid(m) else GLib.markup_escape_text(readme.plain(m)))
                lbl.add_css_class("lead")
                card.append(lbl)
            self.summary_slot.append(card)

        if sm["features"]:
            t = Gtk.Label(label="Lo más destacado", xalign=0)
            t.add_css_class("section-title")
            self.summary_slot.append(t)
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                               max_children_per_line=3, min_children_per_line=1,
                               column_spacing=8, row_spacing=8, valign=Gtk.Align.START)
            feats = sm["features"]
            for i, (title, desc) in enumerate(feats):
                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                tile.add_css_class("feature-tile")
                h = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                              max_width_chars=24, width_chars=10)
                h.add_css_class("feature-title")
                tile.append(h)
                # max/width_chars acotan el ancho natural: si no, cada tarjeta ocupa una fila entera
                d = Gtk.Label(label=desc, xalign=0, yalign=0, wrap=True, lines=2,
                              ellipsize=Pango.EllipsizeMode.END, max_width_chars=30, width_chars=10)
                d.set_tooltip_text(desc)
                d.add_css_class("dim-label")
                d.add_css_class("caption")
                tile.append(d)
                flow.append(tile)
                if i >= 6:  # solo 6 a la vista
                    flow.get_child_at_index(i).set_visible(False)
            if len(feats) > 6:
                more = Gtk.Button(label=f"Ver las {len(feats)}", halign=Gtk.Align.START)
                more.add_css_class("flat")
                more.add_css_class("caption")

                def toggle(b, flow=flow, n=len(feats)):  # noqa: B008 — se evalúa una vez por tarjeta, a propósito
                    show = b.get_label().startswith("Ver las")
                    for j in range(6, n):
                        flow.get_child_at_index(j).set_visible(show)
                    b.set_label("Ver menos" if show else f"Ver las {n}")

                more.connect("clicked", toggle)
                self.summary_slot.append(flow)
                self.summary_slot.append(more)
                flow = None
            if flow is not None:
                self.summary_slot.append(flow)

        g = Adw.PreferencesGroup(title="Documentación",
                                 description="El README del autor, por secciones. Ábrelas cuando las necesites.")
        gh = Gtk.Button(valign=Gtk.Align.CENTER)
        gh.set_child(Adw.ButtonContent(icon_name="web-browser-symbolic", label="Ver en GitHub"))
        gh.add_css_class("flat")
        gh.connect("clicked", lambda *_: Gtk.UriLauncher.new(
            f"{dot.repo.rstrip('/')}#readme").launch(self.win, None, None, None))
        g.set_header_suffix(gh)
        for title, blocks, preview in sm["sections"]:
            er = Adw.ExpanderRow(title=GLib.markup_escape_text(title),
                                 subtitle=GLib.markup_escape_text(preview))
            er.set_subtitle_lines(1)

            def expand(row, _p, blocks=blocks, built=[]):  # noqa: B006 — «built» marca la carga perezosa
                if row.get_expanded() and not built:
                    built.append(True)
                    view = ReadmeView(blocks, res["clone"], res["readme"], dot.repo,
                                      resolve_image=lambda s: resolve_image(dot, base, s))
                    for m in ("top", "bottom", "start", "end"):
                        getattr(view, f"set_margin_{m}")(16)
                    row.add_row(view)

            er.connect("notify::expanded", expand)
            g.add(er)
        if sm["sections"]:
            self.docs_slot.append(g)

    def _gallery_fail(self, car, w):
        """Quita de la galería lo que no carga; si no queda nada, la oculta entera."""
        if w.get_parent():
            car.remove(w)
        if car.get_n_pages() == 0:
            self.gallery_slot.set_visible(False)

    @staticmethod
    def _step(car: Adw.Carousel, d: int):
        n = car.get_n_pages()
        if n:
            i = (round(car.get_position()) + d) % n
            car.scroll_to(car.get_nth_page(i), True)

    def _info(self, st) -> Gtk.Widget:
        dot = self.dot
        g = Adw.PreferencesGroup(title="Detalles técnicos")
        g.add(Adw.ActionRow(title="Método de instalación", subtitle=GLib.markup_escape_text(
            f"Script del autor en una terminal: {dot.command}" if dot.method == "script" else
            "Enlaces simbólicos a un checkout del repo (reversible y sin copias)")))
        ver = f"Se siguen los {TRACK[dot.track]}"
        if st:
            ver += f" · instalada {st['version']['label']}"
        g.add(Adw.ActionRow(title="Versionado", subtitle=GLib.markup_escape_text(ver)))
        if dot.method == "files":
            er = Adw.ExpanderRow(title="Rutas que enlaza", subtitle=f"{len(dot.links)} rutas · se respaldan antes")
            for l in dot.links:
                er.add_row(Adw.ActionRow(title=GLib.markup_escape_text(f"{l.dest}"),
                                         subtitle=GLib.markup_escape_text(
                                             f"← {l.src}" + (" · opcional" if l.optional else "")
                                             + (f" · {l.note}" if l.note else ""))))
            g.add(er)
        if dot.touches:
            er = Adw.ExpanderRow(title="Rutas que toca (se respaldan antes)", subtitle=f"{len(dot.touches)} rutas")
            for t in dot.touches:
                er.add_row(Adw.ActionRow(title=GLib.markup_escape_text(t)))
            g.add(er)
        self.deps_row = Adw.ExpanderRow(title="Dependencias",
                                        subtitle=GLib.markup_escape_text(dot.deps_note or "comprobando…"))
        g.add(self.deps_row)  # las filas se añaden al saber qué falta (_deps_bg)
        threading.Thread(target=self._deps_bg, daemon=True).start()
        if dot.reload_note or dot.reload:
            g.add(Adw.ActionRow(title="Después de instalar", subtitle=GLib.markup_escape_text(
                dot.reload_note or "; ".join(dot.reload))))
        if dot.detected_auto or dot.detected_manual:
            g.add(Adw.ActionRow(title="Definición", subtitle=GLib.markup_escape_text(
                f"detectado solo: {', '.join(dot.detected_auto) or '—'} · revisado/rellenado por ti: "
                f"{', '.join(dot.detected_manual) or '—'}")))
        if dot.path:
            g.add(Adw.ActionRow(title="Archivo de definición", subtitle=GLib.markup_escape_text(str(dot.path))))
        return g

    def _deps_bg(self):
        miss = deps.missing(self.dot.pacman + self.dot.aur)
        total = len(self.dot.pacman) + len(self.dot.aur)
        plan = deps.plan(self.dot) if miss else None

        def show():
            if plan and plan.commands():
                self._deps_cmd_row(plan)
            for pkgs, origin in ((self.dot.pacman, "repos oficiales"), (self.dot.aur, "AUR")):
                for p in sorted(pkgs, key=lambda p: p not in miss):
                    r = Adw.ActionRow(title=p, subtitle=origin + (" · NO instalado" if p in miss else " · instalado"))
                    r.add_prefix(Gtk.Image(icon_name="dialog-error-symbolic" if p in miss else "emblem-ok-symbolic",
                                           css_classes=["status-missing" if p in miss else "status-auto"]))
                    self.deps_row.add_row(r)
            txt = (f"{total} paquetes · " if total else "") + (
                f"faltan: {', '.join(miss)}" if miss else ("todas instaladas" if total else "ninguna declarada"))
            if self.dot.deps_note:
                txt += " · " + self.dot.deps_note
            self.deps_row.set_subtitle(GLib.markup_escape_text(txt))
            return False

        GLib.idle_add(show)

    def _deps_cmd_row(self, plan):
        """Fila con el comando para instalar las dependencias que faltan (copiar o ejecutar)."""
        cmd = " && ".join(plan.commands())
        row = Adw.ActionRow(title="Instalar las que faltan", subtitle=GLib.markup_escape_text(cmd),
                            subtitle_selectable=True)
        cp = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copiar el comando")
        cp.add_css_class("flat")
        cp.connect("clicked", lambda *_: (self.get_clipboard().set(cmd), self.win.toast("Comando copiado")))
        row.add_suffix(cp)
        run = Gtk.Button(label="Instalar", valign=Gtk.Align.CENTER,
                         tooltip_text="Abre una terminal; sudo te pedirá la contraseña allí")
        run.add_css_class("suggested-action")
        run.connect("clicked", lambda *_: self._install_deps(plan))
        row.add_suffix(run)
        self.deps_row.add_row(row)
        self.deps_row.set_expanded(True)

    def _install_deps(self, plan):
        dot = self.dot

        def work(ui):
            for c in plan.commands():
                rc = ui.run_terminal(c, config.home(), f"dependencias de {dot.name}")
                if rc != 0:
                    return engine.Result(False, f"El comando terminó con código {rc}", [c])
            left = deps.missing(dot.pacman + dot.aur)
            if left:
                return engine.Result(False, "Siguen faltando paquetes", left)
            return engine.Result(True, f"Dependencias de {dot.name} instaladas")

        alert(self.win, "Instalar dependencias",
              "Se abrirá una terminal con estos comandos; sudo te pedirá la contraseña allí.",
              plan.commands(), ok="Instalar paquetes",
              callback=lambda ok: ok and Operation(self.win, "Instalando dependencias", work).start())

    def _delete(self):
        if self.dot.id in self.win.states:
            alert(self.win, "Está instalado", "Desinstálalo antes de eliminarlo del catálogo.",
                  cancel=None, ok="Entendido")
            return

        def go(ok):
            if ok:
                catalog.delete_user_dot(self.dot)
                self.win.refresh()
                self.win.nav.pop_to_tag("main")
                self.win.toast(f"{self.dot.name} eliminado del catálogo")

        alert(self.win, f"Eliminar {self.dot.name}", "Se borrará su definición del catálogo "
              f"({config.contract(self.dot.path)}).", ok="Eliminar", danger=True, callback=go)


def _valid(markup: str) -> bool:
    try:
        Pango.parse_markup(strip_links(markup), -1, "\0")
        return True
    except GLib.Error:
        return False
