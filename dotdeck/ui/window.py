"""Ventana principal: Biblioteca, Instalados, Historial (+ Huérfanos)."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .. import backup, catalog, config, engine, gitrepo, onboard, orphans, updates  # noqa: E402
from ..config import log  # noqa: E402
from . import images, readme  # noqa: E402
from .auth import require_sudo  # noqa: E402
from .bridge import Operation, alert  # noqa: E402

_preview_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="preview")


def pill(text: str, kind: str = "neutral") -> Gtk.Label:
    l = Gtk.Label(label=text)
    l.add_css_class("pill")
    l.add_css_class(kind)
    return l


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class PreviewCache:
    """Clones de previsualización + README parseado por dot (en segundo plano)."""

    def __init__(self):
        self.data: dict[str, dict] = {}
        self.waiters: dict[str, list] = {}
        self.lock = threading.Lock()

    def get(self, dot: catalog.Dot, callback, refresh: bool = False) -> None:
        key = dot.id + "|" + dot.repo
        with self.lock:
            if key in self.data and not refresh:
                GLib.idle_add(callback, self.data[key])
                return
            if key in self.waiters:
                self.waiters[key].append(callback)
                return
            self.waiters[key] = [callback]
        _preview_pool.submit(self._load, dot, key, refresh)

    def _load(self, dot, key, refresh):
        res = {"clone": None, "blocks": [], "readme": dot.readme, "images": [], "videos": [], "error": None,
               "commit": ""}
        try:
            clone = gitrepo.preview_clone(dot, refresh=refresh)
            res["clone"] = clone
            res["commit"] = gitrepo.head_commit(clone)
            text = ""
            if dot.readme:
                try:
                    text = gitrepo.read_file(clone, dot.readme).decode("utf-8", "replace")
                except gitrepo.GitError:
                    names = gitrepo.ls_files(clone)
                    alt = [n for n in names if n.lower() in ("readme.md", ".github/readme.md", "readme")]
                    if alt:
                        res["readme"] = alt[0]
                        text = gitrepo.read_file(clone, alt[0]).decode("utf-8", "replace")
            if dot.owner_repo:
                images.register_assets("/".join(dot.owner_repo), text)
            is_md = res["readme"].lower().endswith((".md", ".markdown")) or "." not in res["readme"]
            res["blocks"] = readme.parse(text, is_md) if text else []
            base = str(Path(res["readme"]).parent)
            pics = [resolve_image(dot, base, s) for s in dot.images + readme.image_list(res["blocks"])]
            vids = [resolve_image(dot, base, s) for s in readme.media_list(res["blocks"])]
            vids += [p for p in pics if images.is_video_src(p)]
            imgs = [p for p in pics if p not in vids]
            cover = dot.cover_source()
            if cover:
                imgs.insert(0, cover if cover.startswith(("/", "http")) else resolve_image(dot, base, cover))

            def dedup(xs):
                seen, out = set(), []
                for i in xs:
                    if i not in seen and "avatars.githubusercontent" not in i:
                        seen.add(i)
                        out.append(i)
                return out

            res["images"] = dedup(imgs)      # miniaturas: solo imágenes, portada primero
            res["videos"] = dedup(vids)
        except Exception as e:  # noqa: BLE001
            log.warning("previsualización de %s: %s", dot.id, e)
            res["error"] = str(e)
        with self.lock:
            self.data[key] = res
            cbs = self.waiters.pop(key, [])
        for cb in cbs:
            GLib.idle_add(cb, res)


def resolve_image(dot: catalog.Dot, base: str, src: str) -> str:
    """Normaliza una referencia de imagen: URL absoluta o ruta dentro del repo."""
    import re
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/([^/]+)/(.+)$", src)
    if m:
        orr = dot.owner_repo
        if orr and orr[0].lower() == m.group(1).lower() and orr[1].lower() == m.group(2).lower():
            return m.group(4).split("?")[0]
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
    if src.startswith(("http://", "https://")):
        return src
    if src.startswith("//"):
        return "https:" + src
    return onboard.resolve_rel(base, src)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=config.APP_NAME, default_width=1220, default_height=820)
        self.add_css_class("dotdeck")
        self.previews = PreviewCache()
        self.dots: dict[str, catalog.Dot] = {}
        self.states: dict[str, dict] = {}
        self.upd: dict = {}
        self.load_errors: list[str] = []

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        # ---------- página principal
        self.stack = Adw.ViewStack()
        self.library = LibraryView(self)
        self.installed = InstalledView(self)
        self.history = HistoryView(self)
        self.stack.add_titled_with_icon(self.library, "library", "Biblioteca", "view-grid-symbolic")
        self.stack.add_titled_with_icon(self.installed, "installed", "Instalados", "emblem-ok-symbolic")
        self.stack.add_titled_with_icon(self.history, "history", "Historial", "document-open-recent-symbolic")
        from .discover_view import DiscoverView
        self.discover = DiscoverView(self)
        self.stack.add_titled_with_icon(self.discover, "discover", "Descubrir", "system-search-symbolic")
        # La búsqueda en GitHub solo se lanza al abrir la pestaña por primera vez.
        self.stack.connect("notify::visible-child-name",
                           lambda st, _p: st.get_visible_child_name() == "discover" and self.discover.ensure_loaded())

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        add = Gtk.Button()
        add.set_child(Adw.ButtonContent(icon_name="dotdeck-add-symbolic", label="Añadir dot"))
        add.add_css_class("suggested-action")
        add.set_tooltip_text("Añadir un dot nuevo al catálogo a partir de la URL de su repositorio")
        add.connect("clicked", lambda *_: self.open_editor())
        header.pack_start(add)

        menu = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menú")
        pop = Gtk.Popover()
        mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                       margin_start=6, margin_end=6)
        for label, cb in (("Buscar actualizaciones", self.check_updates), ("Huérfanos…", self.open_orphans),
                          ("Ajustes", self.open_settings), ("Abrir log", self.open_log),
                          ("Acerca de DotDeck", self.open_about)):
            b = Gtk.Button(label=label, has_frame=False)
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, c=cb: (pop.popdown(), c()))
            mbox.append(b)
        pop.set_child(mbox)
        menu.set_popover(pop)
        header.pack_end(menu)
        hreload = Gtk.Button(icon_name="system-reboot-symbolic", tooltip_text="Recargar Hyprland (hyprctl reload)")
        hreload.connect("clicked", lambda *_: self.do_reload())
        header.pack_end(hreload)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Recargar catálogo")
        refresh.connect("clicked", lambda *_: self.refresh(reload_previews=True))
        header.pack_end(refresh)

        self.banner = Adw.Banner(button_label="Actualizar")
        self.banner.add_css_class("update-banner")
        self.banner.connect("button-clicked", lambda *_: self._banner_update())

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.add_top_bar(self.banner)
        tv.set_content(self.stack)
        bar = Adw.ViewSwitcherBar(stack=self.stack)
        tv.add_bottom_bar(bar)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
        bp.add_setter(bar, "reveal", True)
        bp.add_setter(switcher, "visible", False)
        self.add_breakpoint(bp)
        self.nav.add(Adw.NavigationPage(title="DotDeck", tag="main", child=tv))

        self.refresh()
        GLib.timeout_add_seconds(2, self._startup_check)

    # ---------------------------------------------------------- datos
    def refresh(self, reload_previews: bool = False) -> None:
        self.dots, self.load_errors = catalog.load_all()
        for d in engine.sync_external(self.dots):
            GLib.idle_add(self.toast, f"{self.dots[d].name}: instalación existente detectada")
        self.states = engine.all_states()
        self.upd = updates.load()
        if reload_previews:
            self.previews = PreviewCache()
        av = self.upd.get("available", {})
        n = len(av)
        if n == 1:
            k, v = next(iter(av.items()))
            self.banner.set_title(GLib.markup_escape_text(
                f"Actualización disponible para {v['name']}: {v['installed']} → {v['latest']}"))
            self.banner.set_button_label("Actualizar")
        elif n:
            self.banner.set_title(f"Hay {n} actualizaciones disponibles: "
                                  + GLib.markup_escape_text(", ".join(v["name"] for v in av.values())))
            self.banner.set_button_label("Actualizar todo")
        self.banner.set_revealed(n > 0)
        self.library.rebuild()
        self.installed.rebuild()
        self.history.rebuild()
        top = self.nav.get_visible_page()
        if top is not None and hasattr(top, "dot") and hasattr(top, "build"):
            if top.dot.id in self.dots:
                top.dot = self.dots[top.dot.id]
                top.build()
        if self.load_errors:
            self.toast(f"{len(self.load_errors)} definición(es) inválida(s): ver log")

    def toast(self, msg: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=5))

    def show_page(self, name: str) -> None:
        if name == "settings":
            self.open_settings()
            return
        if self.stack.get_child_by_name(name):
            self.nav.pop_to_tag("main")
            self.stack.set_visible_child_name(name)

    def state_of(self, dot_id: str) -> str:
        if dot_id in self.upd.get("available", {}) and dot_id in self.states:
            return "update"
        return "installed" if dot_id in self.states else "none"

    def _banner_update(self) -> None:
        ids = [i for i in self.upd.get("available", {}) if i in self.dots and i in self.states]
        if len(ids) == 1:
            self.do_update(self.dots[ids[0]])
        elif ids:
            self.installed._update_all(ids)

    # ---------------------------------------------------------- acciones
    def _startup_check(self) -> bool:
        last = self.upd.get("checked_at")
        stale = True
        if last:
            try:
                stale = time.time() - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S")) > 3600
            except ValueError:
                pass
        if self.states and stale:
            self.check_updates(quiet=True)
        return False

    def check_updates(self, quiet: bool = False) -> None:
        if not quiet:
            self.toast("Buscando actualizaciones…")

        def work():
            try:
                res = updates.check_all()
            except Exception as e:  # noqa: BLE001
                res = {"available": {}, "errors": {"*": str(e)}}
            GLib.idle_add(done, res)

        def done(res):
            self.refresh()
            n = len(res.get("available", {}))
            if n or not quiet:
                self.toast(f"{n} actualización(es) disponible(s)" if n else "Todo al día")
            for k, v in res.get("errors", {}).items():
                log.warning("comprobación %s: %s", k, v)
            return False

        threading.Thread(target=work, daemon=True).start()

    def open_detail(self, dot: catalog.Dot) -> None:
        from .detail import DetailPage
        self.nav.push(DetailPage(self, dot))

    def open_repo_preview(self, repo) -> None:
        """Ficha de vista previa de un repo de Descubrir (sin añadirlo al catálogo)."""
        from .detail import DetailPage
        import re as _re
        owner, name = repo.full_name.split("/", 1)
        tmp = catalog.Dot(id="preview-" + _re.sub(r"[^a-z0-9._-]+", "-", repo.full_name.lower()),
                          name=name, repo=repo.url, author=owner, description=repo.description,
                          license=repo.license, method="script", command="-", touches=["~/.x"],
                          readme="README.md", origin="preview")
        self.nav.push(DetailPage(self, tmp, preview=repo))

    def open_editor(self, dot: catalog.Dot | None = None, url: str | None = None) -> None:
        from .editor import EditorPage
        page = EditorPage(self, dot)
        self.nav.push(page)
        if url and dot is None:
            page.url.set_text(url)
            page.analyze()

    def open_changes(self, dot: catalog.Dot) -> None:
        from .changes_page import ChangesPage
        self.nav.push(ChangesPage(self, dot))

    def open_orphans(self) -> None:
        self.nav.push(OrphansPage(self))

    def open_settings(self) -> None:
        from .settings import SettingsDialog
        SettingsDialog(self).present(self)

    def open_log(self) -> None:
        Gtk.FileLauncher.new(__import__("gi.repository.Gio", fromlist=["Gio"]).File.new_for_path(
            str(config.log_file()))).launch(self, None, None, None)

    def open_about(self) -> None:
        Adw.AboutDialog(application_name=config.APP_NAME, version=config.VERSION,
                        application_icon="preferences-desktop-theme-symbolic",
                        developer_name="madkyp", license_type=Gtk.License.MIT_X11,
                        comments="Instala, actualiza, revierte y limpia dots de Hyprland "
                                 "con backups verificados.").present(self)

    # Operaciones del motor (con diálogo de progreso) ---------------
    def do_install(self, dot: catalog.Dot) -> None:
        require_sudo(self, f"instalar {dot.name}", lambda: self._do_install(dot))

    def _do_install(self, dot: catalog.Dot) -> None:
        optional = [l for l in dot.links if l.optional]
        clashes = engine.conflicting_dots(dot, dot.backup_paths([l for l in dot.links if not l.optional]))

        def go(opt_selected: list[str], replace: bool):
            Operation(self, f"Instalando {dot.name}",
                      lambda ui: engine.install(dot, ui, optional=opt_selected, replace=replace)).start()

        def after_opts(opt_selected):
            if clashes:
                names = [self.dots[k].name if k in self.dots else k for k in clashes]
                alert(self, "Choca con dots instalados",
                      "Este dot usa rutas que ya pertenecen a otro dot instalado. Para instalarlo, "
                      "DotDeck desinstalará antes ese dot y restaurará tu configuración previa.",
                      [f"{n}: {', '.join(v)}" for n, v in zip(names, clashes.values(), strict=True)],
                      ok="Reemplazar", danger=True, callback=lambda ok: ok and go(opt_selected, True))
            else:
                go(opt_selected, False)

        if optional:
            d = Adw.AlertDialog(heading=f"Instalar {dot.name}",
                                body="Elementos opcionales (sustituyen archivos personales):")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            checks = []
            for l in optional:
                c = Gtk.CheckButton(label=f"{l.dest}  ({l.note or l.src})")
                checks.append((c, l))
                box.append(c)
            d.set_extra_child(box)
            d.add_response("cancel", "Cancelar")
            d.add_response("ok", "Continuar")
            d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            d.connect("response", lambda _d, r: r == "ok" and after_opts(
                [l.src for c, l in checks if c.get_active()]))
            d.present(self)
        else:
            after_opts([])

    def do_update(self, dot: catalog.Dot) -> None:
        require_sudo(self, f"actualizar {dot.name}", lambda: Operation(
            self, f"Actualizando {dot.name}", lambda ui: engine.update(dot, ui)).start())

    def do_reload(self) -> None:
        require_sudo(self, "recargar Hyprland", lambda: Operation(
            self, "Recargando Hyprland", engine.reload_hyprland).start())

    def do_uninstall(self, dot_id: str) -> None:
        dot = self.dots.get(dot_id)
        st = self.states.get(dot_id, {})
        if st.get("external"):
            alert(self, f"Dejar de seguir {st.get('name', dot_id)}",
                  f"Está instalado fuera de DotDeck en {config.contract(st['deploy'])}. DotDeck dejará de "
                  "comprobar sus actualizaciones, pero no borrará ni cambiará nada de tu sistema. "
                  "Para quitarlo de verdad usa el desinstalador de su autor.",
                  ok="Dejar de seguir", callback=lambda ok: ok and Operation(
                      self, "Dejando de seguir", lambda ui: engine.uninstall_by_id(dot_id, ui, dot=dot)).start())
            return
        d = Adw.AlertDialog(heading=f"Desinstalar {st.get('name', dot_id)}",
                            body="¿Qué hacemos con tu configuración?")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        r1 = Gtk.CheckButton(label="Restaurar mi configuración de antes de instalarlo (recomendado)", active=True)
        r2 = Gtk.CheckButton(label="Dejarlo limpio (quitar el dot sin restaurar nada)", group=r1)
        box.append(r1)
        box.append(r2)
        author = None
        if dot and dot.uninstall_command:
            author = Gtk.CheckButton(label=f"Ejecutar también el desinstalador del autor ({dot.uninstall_command})")
            box.append(author)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancelar")
        d.add_response("ok", "Desinstalar")
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r != "ok":
                return
            restore = r1.get_active()
            run_author = bool(author and author.get_active())
            Operation(self, "Desinstalando", lambda ui: engine.uninstall_by_id(
                dot_id, ui, restore=restore, run_author=run_author, dot=dot)).start()

        d.connect("response", resp)
        d.present(self)

    def do_revert(self, bid: str) -> None:
        Operation(self, "Revirtiendo backup", lambda ui: engine.revert_backup(bid, ui)).start()


# =================================================================== Biblioteca
FILTERS = [("all", "Todos"), ("installed", "Instalados"), ("update", "Con actualización"),
           ("initial", "Lista inicial"), ("user", "Añadidos por mí"), ("example", "Ejemplos temporales")]


class DotCard(Gtk.Button):
    def __init__(self, win: MainWindow, dot: catalog.Dot):
        super().__init__()
        self.win, self.dot = win, dot
        self.add_css_class("dot-card")
        self.add_css_class("flat")
        self.set_size_request(300, -1)
        self.connect("clicked", lambda *_: win.open_detail(dot))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.thumb = Gtk.Overlay()
        ph = Gtk.Box()
        ph.add_css_class("thumb-placeholder")
        ph.set_size_request(300, 168)
        letter = Gtk.Label(label=dot.name[:1].upper(), hexpand=True, vexpand=True)
        letter.add_css_class("thumb-letter")
        ph.append(letter)
        self.thumb.set_child(ph)
        box.append(self.thumb)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=12, margin_bottom=14,
                       margin_start=16, margin_end=16)
        t = Gtk.Label(label=dot.name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        t.add_css_class("card-title")
        info.append(t)
        a = Gtk.Label(label=f"por {dot.author or '—'}", xalign=0)
        a.add_css_class("dim-label")
        a.add_css_class("caption")
        info.append(a)
        desc = Gtk.Label(label=dot.description or " ", xalign=0, wrap=True, lines=2,
                         ellipsize=Pango.EllipsizeMode.END, max_width_chars=34, height_request=36, yalign=0)
        desc.add_css_class("caption")
        info.append(desc)
        pills = Gtk.Box(spacing=6, margin_top=6)
        st = win.state_of(dot.id)
        if st == "update":
            pills.append(pill("↑ Actualización", "update"))
        elif st == "installed":
            pills.append(pill("✓ Instalado", "installed"))
        else:
            pills.append(pill("No instalado"))
        if dot.example:
            pills.append(pill("Ejemplo temporal", "example"))
        elif dot.origin == "user":
            pills.append(pill("Añadido por ti", "user"))
        else:
            pills.append(pill("Lista inicial"))
        pills.append(pill("script" if dot.method == "script" else "archivos"))
        info.append(pills)
        box.append(info)
        self.set_child(box)
        win.previews.get(dot, self._preview)

    def _preview(self, res):
        if res.get("images"):
            self._try_thumb(res, 0)
        return False

    def _try_thumb(self, res, i):
        if i >= min(len(res["images"]), 4):
            return
        src = res["images"][i]

        def done(tex, err):
            if tex is None:
                self._try_thumb(res, i + 1)
                return False
            pic = Gtk.Picture.new_for_paintable(tex)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            pic.set_size_request(300, 168)
            pic.add_css_class("thumb")
            pic.set_overflow(Gtk.Overflow.HIDDEN)
            self.thumb.add_overlay(pic)
            return False

        from .images import load_async
        load_async(src, res["clone"], done)


class LibraryView(Gtk.Box):
    def __init__(self, win: MainWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        bar = Gtk.Box(spacing=10, margin_start=24, margin_end=24, margin_top=12)
        bar.add_css_class("filter-bar")
        self.search = Gtk.SearchEntry(placeholder_text="Buscar dots…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        bar.append(self.search)
        self.filter = Gtk.DropDown.new_from_strings([f[1] for f in FILTERS])
        self.filter.connect("notify::selected", lambda *_: self.flow.invalidate_filter())
        bar.append(self.filter)
        self.count = Gtk.Label()
        self.count.add_css_class("dim-label")
        bar.append(self.count)
        self.append(bar)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                column_spacing=18, row_spacing=18, min_children_per_line=1,
                                max_children_per_line=6, valign=Gtk.Align.START,
                                margin_start=24, margin_end=24, margin_top=12, margin_bottom=24)
        self.flow.set_filter_func(self._filter)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.flow)
        self.append(sw)

    def rebuild(self):
        self.flow.remove_all()
        for dot in sorted(self.win.dots.values(), key=lambda d: (d.example, d.name.lower())):
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(DotCard(self.win, dot))
            child.dot = dot
            self.flow.append(child)
        self.count.set_label(f"{len(self.win.dots)} dots")
        self.flow.invalidate_filter()

    def _filter(self, child) -> bool:
        dot = child.dot
        q = self.search.get_text().strip().lower()
        if q and q not in f"{dot.name} {dot.author} {dot.description} {dot.repo}".lower():
            return False
        f = FILTERS[self.filter.get_selected()][0]
        st = self.win.state_of(dot.id)
        return (f == "all" or (f == "installed" and st != "none") or (f == "update" and st == "update")
                or (f == "initial" and dot.origin == "initial" and not dot.example)
                or (f == "user" and dot.origin == "user") or (f == "example" and dot.example))


# =================================================================== Instalados
class InstalledView(Gtk.ScrolledWindow):
    def __init__(self, win: MainWindow):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self.page = None

    def rebuild(self):
        page = Adw.PreferencesPage()
        states = self.win.states
        av = self.win.upd.get("available", {})
        top = Adw.PreferencesGroup(title="Comprobación de actualizaciones")
        row = Adw.ActionRow(title=f"Última comprobación: {self.win.upd.get('checked_at', 'nunca')}",
                            subtitle="En segundo plano según Ajustes (temporizador systemd de usuario)"
                            + (" · activo" if _timer_on() else " · inactivo"))
        b = Gtk.Button(label="Comprobar ahora", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.win.check_updates())
        row.add_suffix(b)
        top.add(row)
        if av:
            ball = Gtk.Button(label=f"Actualizar todo ({len(av)})", valign=Gtk.Align.CENTER)
            ball.add_css_class("suggested-action")
            ball.connect("clicked", lambda *_: self._update_all(list(av)))
            row.add_suffix(ball)
        page.add(top)
        if not states:
            g = Adw.PreferencesGroup()
            g.add(Adw.StatusPage(icon_name="folder-download-symbolic", title="Nada instalado todavía",
                                 description="Elige un dot en la Biblioteca para previsualizarlo e instalarlo."))
            page.add(g)
        else:
            g = Adw.PreferencesGroup(title="Dots instalados")
            for dot_id, st in sorted(states.items()):
                dot = self.win.dots.get(dot_id)
                r = Adw.ActionRow(title=GLib.markup_escape_text(st.get("name", dot_id)),
                                  subtitle=GLib.markup_escape_text(
                                      f"{st['version']['label']} · instalado {st['installed_at']}"
                                      + (f" · actualizado {st['updated_at']}" if st.get("updated_at") else "")
                                      + f" · {'script del autor' if st['method'] == 'script' else 'symlinks'}"))
                if dot_id in av:
                    r.add_suffix(pill(f"↑ {av[dot_id]['latest']}", "update"))
                    cb = Gtk.Button(icon_name="view-list-bullet-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Ver qué cambia en la actualización")
                    cb.add_css_class("flat")
                    cb.connect("clicked", lambda _b, d=dot: d and self.win.open_changes(d))
                    r.add_suffix(cb)
                    ub = Gtk.Button(label="Actualizar", valign=Gtk.Align.CENTER)
                    ub.add_css_class("suggested-action")
                    ub.connect("clicked", lambda _b, d=dot: d and self.win.do_update(d))
                    r.add_suffix(ub)
                else:
                    r.add_suffix(pill("Al día", "installed"))
                xb = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Desinstalar")
                xb.add_css_class("flat")
                xb.connect("clicked", lambda _b, i=dot_id: self.win.do_uninstall(i))
                r.add_suffix(xb)
                if dot:
                    r.set_activatable(True)
                    r.connect("activated", lambda _r, d=dot: self.win.open_detail(d))
                    r.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                else:
                    r.add_suffix(pill("definición eliminada", "danger"))
                g.add(r)
            page.add(g)
        errs = self.win.upd.get("errors", {})
        if errs:
            eg = Adw.PreferencesGroup(title="No se pudieron comprobar")
            for k, v in errs.items():
                eg.add(Adw.ActionRow(title=k, subtitle=GLib.markup_escape_text(v)))
            page.add(eg)
        self.set_child(page)

    def _update_all(self, ids):
        dots = [self.win.dots[i] for i in ids if i in self.win.dots]

        def run(ui):
            msgs, warns = [], []
            for d in dots:
                try:
                    r = engine.update(d, ui)
                    msgs.append(r.message)
                    warns += r.warnings
                except engine.EngineError as e:
                    warns.append(f"{d.name}: {e}")
            return engine.Result(True, "; ".join(msgs) or "Nada que actualizar", warns,
                                 reboot=any(m and "actualizado" in m for m in msgs))

        require_sudo(self.win, "actualizar todos los dots",
                     lambda: Operation(self.win, "Actualizando todo", run).start())


def _timer_on() -> bool:
    try:
        return updates.timer_active()
    except Exception:  # noqa: BLE001
        return False


# =================================================================== Historial
class HistoryView(Gtk.ScrolledWindow):
    def __init__(self, win: MainWindow):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win

    def rebuild(self):
        page = Adw.PreferencesPage()
        bks = backup.list_all()
        g = Adw.PreferencesGroup(title="Backups",
                                 description="Cada instalación, actualización, reversión y limpieza guarda "
                                             "antes un backup verificado. «Revertir» restaura sus rutas "
                                             "exactamente como estaban.")
        ob = Gtk.Button(label="Huérfanos…", valign=Gtk.Align.CENTER)
        ob.connect("clicked", lambda *_: self.win.open_orphans())
        g.set_header_suffix(ob)
        if not bks:
            g.add(Adw.ActionRow(title="Todavía no hay backups"))
        acts = {"install": "instalación", "update": "actualización", "pre-restore": "antes de revertir",
                "clean": "limpieza de huérfanos"}
        for b in bks[:200]:
            name = self.win.dots[b.dot].name if b.dot in self.win.dots else b.dot
            er = Adw.ExpanderRow(
                title=GLib.markup_escape_text(f"{name} · {acts.get(b.action, b.action)}"),
                subtitle=GLib.markup_escape_text(f"{b.created} · {len(b.entries)} rutas · {fmt_size(b.size())}"
                                                 + ("" if b.complete else f" · {b.manifest.get('status')}")))
            for e in b.entries:
                er.add_row(Adw.ActionRow(title=GLib.markup_escape_text(e["display"]),
                                         subtitle="guardado" if e["existed"] else
                                         "no existía (revertir lo elimina)"))
            if b.complete:
                rb = Gtk.Button(label="Revertir", valign=Gtk.Align.CENTER)
                rb.connect("clicked", lambda _b, i=b.id: self.win.do_revert(i))
                er.add_suffix(rb)
            db = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Borrar backup")
            db.add_css_class("flat")
            db.connect("clicked", lambda _b, i=b.id: self._delete(i))
            er.add_suffix(db)
            g.add(er)
        page.add(g)
        h = Adw.PreferencesGroup(title="Registro de operaciones")
        hist = engine.read_history(80)
        if not hist:
            h.add(Adw.ActionRow(title="Sin operaciones todavía"))
        for rec in hist:
            r = Adw.ActionRow(title=GLib.markup_escape_text(f"{rec['dot']} · {rec['action']} · {rec['message']}"),
                              subtitle=rec["ts"])
            r.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic" if rec["ok"] else "dialog-error-symbolic"))
            h.add(r)
        page.add(h)
        self.set_child(page)

    def _delete(self, bid):
        used = [st.get("name", k) for k, st in self.win.states.items() if bid in st.get("backups", [])]
        body = "Se borrará el backup del disco. No podrás revertir a él."
        if used:
            body += f"\n⚠ Es el backup de {', '.join(used)}: sin él no se podrá restaurar tu config al desinstalar."

        def go(ok):
            if ok:
                backup.delete(bid)
                self.win.refresh()
                self.win.toast("Backup eliminado")

        alert(self.win, "Borrar backup", body, ok="Borrar", danger=True, callback=go)


# =================================================================== Huérfanos
class OrphansPage(Adw.NavigationPage):
    def __init__(self, win: MainWindow):
        super().__init__(title="Huérfanos")
        self.win = win
        self.tv = Adw.ToolbarView()
        self.tv.add_top_bar(Adw.HeaderBar())
        self.set_child(self.tv)
        self.load()

    def load(self):
        sp = Adw.Spinner(width_request=32, height_request=32, valign=Gtk.Align.CENTER)
        self.tv.set_content(sp)

        def work():
            try:
                res = orphans.find()
            except Exception as e:  # noqa: BLE001
                res = ([], [f"Error: {e}"])
            GLib.idle_add(self.show, *res)

        threading.Thread(target=work, daemon=True).start()

    def show(self, found, notes):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            title="Restos registrados",
            description="Solo aparecen rutas que DotDeck creó (según sus manifiestos) y que ya no usa "
                        "ningún dot instalado. Nada fuera de esos registros se borra nunca.")
        self.checks = []
        if not found:
            g.add(Adw.ActionRow(title="No hay huérfanos ✓"))
        for o in found:
            r = Adw.ActionRow(title=GLib.markup_escape_text(o.display),
                              subtitle=GLib.markup_escape_text(f"{o.dot} · {o.reason}"))
            c = Gtk.CheckButton(active=o.safe, sensitive=o.safe, valign=Gtk.Align.CENTER)
            r.add_prefix(c)
            r.set_activatable_widget(c)
            if not o.safe:
                r.add_suffix(pill("no verificable: no se borra", "danger"))
            self.checks.append((c, o))
            g.add(r)
        if found:
            b = Gtk.Button(label="Limpiar seleccionados", valign=Gtk.Align.CENTER)
            b.add_css_class("destructive-action")
            b.connect("clicked", lambda *_: self.clean())
            g.set_header_suffix(b)
        page.add(g)
        if notes:
            n = Adw.PreferencesGroup(title="Avisos (no se tocan)",
                                     description="Posibles restos que DotDeck no registró: revísalos tú.")
            for t in notes:
                n.add(Adw.ActionRow(title=GLib.markup_escape_text(t)))
            page.add(n)
        self.tv.set_content(page)
        return False

    def clean(self):
        paths = [o.path for c, o in self.checks if c.get_active() and o.safe]
        if not paths:
            return

        def go(ok):
            if ok:
                Operation(self.win, "Limpiando huérfanos",
                          lambda ui: engine.Result(True, f"{len(paths)} huérfano(s) eliminados",
                                                   orphans.clean(paths)),
                          on_done=lambda *_: self.load()).start()

        alert(self.win, "Limpiar huérfanos", "Se borrarán (los que no son enlaces se respaldan antes):",
              [config.contract(p) for p in paths], ok="Borrar", danger=True, callback=go)
