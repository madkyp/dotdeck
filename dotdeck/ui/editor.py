"""«Añadir dot» / «Editar dot»: análisis automático de la URL + formulario de revisión.

Cada campo muestra de dónde sale su valor:
  ✔ verde    detectado automáticamente con garantías
  ? ámbar    detectado por heurística → revísalo
  ✘ rojo     no se pudo detectar → rellénalo tú
  ✎ acento   lo has escrito/cambiado tú
"""
from __future__ import annotations

import copy
import re
import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import catalog, onboard  # noqa: E402
from ..catalog import Dot, Link  # noqa: E402
from .bridge import alert  # noqa: E402

ICONS = {"auto": ("emblem-ok-symbolic", "status-auto", "Detectado automáticamente"),
         "review": ("dialog-warning-symbolic", "status-review", "Detectado por heurística: revísalo"),
         "missing": ("dialog-error-symbolic", "status-missing", "No se pudo detectar: rellénalo"),
         "manual": ("document-edit-symbolic", "status-manual", "Rellenado o cambiado por ti")}
METHODS = [("files", "Enlazar carpetas del repo (symlinks)"), ("script", "Ejecutar el instalador del autor")]
TRACKS = [("release", "Releases de GitHub"), ("tag", "Tags de git"), ("commit", "Commits de la rama")]


def split_list(s: str) -> list[str]:
    return [x for x in re.split(r"[\s,]+", s.strip()) if x]


class EditorPage(Adw.NavigationPage):
    def __init__(self, win, dot: Dot | None):
        super().__init__(title="Editar dot" if dot else "Añadir dot")
        self.win = win
        self.editing = dot
        self.proposal: onboard.Proposal | None = None
        self.status: dict[str, tuple[str, str]] = {}
        self.icons: dict[str, Gtk.Image] = {}
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.save_btn = Gtk.Button(label="Guardar en el catálogo", sensitive=False)
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save())
        hb.pack_end(self.save_btn)
        tv.add_top_bar(hb)
        self.page = Adw.PreferencesPage()
        tv.set_content(self.page)
        self.set_child(tv)

        if dot is None:
            g = Adw.PreferencesGroup(
                title="Repositorio",
                description="Pega la URL del repositorio (GitHub u otro git). DotDeck lo analiza: README, "
                            "capturas, método de instalación, rutas que toca, dependencias y versionado. "
                            "Lo que no pueda detectar con garantías te lo pedirá aquí.")
            self.url = Adw.EntryRow(title="URL del repositorio", show_apply_button=True)
            self.url.connect("apply", lambda *_: self.analyze())
            self.url.connect("entry-activated", lambda *_: self.analyze())
            g.add(self.url)
            self.an_status = Adw.ActionRow(title="Esperando una URL…")
            self.an_spinner = Adw.Spinner(visible=False)
            self.an_status.add_suffix(self.an_spinner)
            g.add(self.an_status)
            self.page.add(g)
        else:
            d = copy.deepcopy(dot)
            if dot.origin != "user":
                self.note = Adw.PreferencesGroup(
                    description="Este dot es de la lista inicial: al guardar se crea una copia tuya en "
                                "~/.config/dotdeck/dots.d que tendrá prioridad sobre la original.")
                self.page.add(self.note)
            st = {k: ("auto", "") for k in d.detected_auto}
            st.update({k: ("manual", "") for k in d.detected_manual})
            self.build_form(d, st, [], [])

    # ------------------------------------------------------------ análisis
    def analyze(self):
        url = self.url.get_text().strip()
        if not url:
            return
        self.an_spinner.set_visible(True)
        self.an_status.set_title("Analizando…")
        self.an_status.set_subtitle("")

        def prog(m):
            GLib.idle_add(self.an_status.set_title, m)

        def work():
            try:
                pr = onboard.propose(url, progress=prog)
                GLib.idle_add(self._analyzed, pr, None)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._analyzed, None, e)

        threading.Thread(target=work, daemon=True).start()

    def _analyzed(self, pr, err):
        self.an_spinner.set_visible(False)
        if err:
            self.an_status.set_title("No se pudo analizar")
            self.an_status.set_subtitle(GLib.markup_escape_text(str(err)))
            return False
        self.proposal = pr
        need = pr.needs_user()
        self.an_status.set_title("Análisis completo")
        self.an_status.set_subtitle(GLib.markup_escape_text(
            f"{len(pr.status) - len(need)} campos detectados solos · {len(need)} por revisar/rellenar"))
        self.build_form(pr.dot, dict(pr.status), pr.warnings, pr.alt_links)
        return False

    # ------------------------------------------------------------ formulario
    def _clear_form(self):
        for g in getattr(self, "form_groups", []):
            self.page.remove(g)
        self.form_groups = []
        self.icons = {}

    def _group(self, **kw) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(**kw)
        self.page.add(g)
        self.form_groups.append(g)
        return g

    def _mark(self, row, key: str):
        st = self.status.get(key, ("auto", ""))[0] if key in self.status else None
        img = Gtk.Image()
        self.icons[key] = img
        row.add_suffix(img)
        self._set_icon(key, st)

    def _set_icon(self, key: str, st: str | None):
        img = self.icons.get(key)
        if not img:
            return
        for c in ("status-auto", "status-review", "status-missing", "status-manual"):
            img.remove_css_class(c)
        if not st:
            img.set_visible(False)
            return
        icon, css, tip = ICONS[st]
        note = self.status.get(key, ("", ""))[1]
        img.set_from_icon_name(icon)
        img.add_css_class(css)
        img.set_tooltip_text(tip + (f" — {note}" if note else ""))
        img.set_visible(True)

    def _touched(self, key: str):
        if key in self.status and self.status[key][0] == "manual":
            return
        self.status[key] = ("manual", "")
        self._set_icon(key, "manual")
        self.validate()

    def _entry(self, g, title: str, key: str, value: str, subtitle_key: str | None = None) -> Adw.EntryRow:
        r = Adw.EntryRow(title=title, text=value or "")
        self._mark(r, subtitle_key or key)
        r.connect("changed", lambda *_: self._touched(subtitle_key or key))
        g.add(r)
        return r

    def build_form(self, dot: Dot, status: dict, warnings: list[str], alt_links: list[Link]):
        self._clear_form()
        self.dot = dot
        self.status = status
        self.alt_links = alt_links
        self.links: list[Link] = list(dot.links)
        self.touches: list[str] = list(dot.touches)

        if warnings:
            g = self._group(title="Avisos del análisis")
            for w in warnings:
                r = Adw.ActionRow(title=GLib.markup_escape_text(w))
                r.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["status-review"]))
                g.add(r)

        g = self._group(title="Identidad")
        self.e_id = self._entry(g, "Identificador (id)", "id", dot.id)
        self.e_name = self._entry(g, "Nombre", "name", dot.name)
        self.e_author = self._entry(g, "Autor", "author", dot.author)
        self.e_desc = self._entry(g, "Descripción", "description", dot.description)
        self.e_license = self._entry(g, "Licencia", "license", dot.license)
        self.e_repo = Adw.EntryRow(title="Repositorio", text=dot.repo, editable=self.editing is not None)
        g.add(self.e_repo)
        self.e_branch = self._entry(g, "Rama (vacío = la principal)", "branch", dot.branch)

        g = self._group(title="Instalación")
        self.c_method = Adw.ComboRow(title="Método", model=Gtk.StringList.new([m[1] for m in METHODS]))
        self.c_method.set_selected([m[0] for m in METHODS].index(dot.method))
        self._mark(self.c_method, "method")
        self.c_method.connect("notify::selected", lambda *_: (self._touched("method"), self._method_changed()))
        g.add(self.c_method)
        if alt_links and dot.method == "script":
            r = Adw.ActionRow(title="Alternativa detectada",
                              subtitle=f"El repo tiene {len(alt_links)} carpetas de configuración que se pueden "
                                       "enlazar directamente en lugar de ejecutar su instalador.")
            b = Gtk.Button(label="Usar carpetas", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._use_alt())
            r.add_suffix(b)
            g.add(r)
        self.e_cmd = self._entry(g, "Comando de instalación (se ejecuta en una terminal)", "command", dot.command)
        self.e_upd = self._entry(g, "Comando de actualización (vacío = el de instalación)", "update_command",
                                 dot.update_command)
        self.e_unc = self._entry(g, "Desinstalador del autor (opcional)", "uninstall_command", dot.uninstall_command)
        self.script_rows = [self.e_cmd, self.e_upd, self.e_unc]

        self.g_links = self._group(title="Rutas a enlazar",
                                   description="Carpeta o archivo del repo → destino en tu $HOME. Todo lo que "
                                               "exista en el destino se respalda antes.")
        self.g_touch = self._group(title="Rutas que toca el instalador",
                                   description="Solo se respalda (y se puede revertir) lo que figure aquí. "
                                               "Revísalo: se dedujo leyendo el instalador y el repo.")
        self.review_touch = Adw.SwitchRow(title="He revisado las rutas que toca",
                                          subtitle="Obligatorio cuando se detectaron por heurística")
        self.review_touch.connect("notify::active", lambda *_: self.validate())
        self._rebuild_links()
        self._rebuild_touches()

        g = self._group(title="Versionado y dependencias")
        self.c_track = Adw.ComboRow(title="Detectar actualizaciones por",
                                    model=Gtk.StringList.new([t[1] for t in TRACKS]))
        self.c_track.set_selected([t[0] for t in TRACKS].index(dot.track))
        self._mark(self.c_track, "track")
        self.c_track.connect("notify::selected", lambda *_: self._touched("track"))
        g.add(self.c_track)
        self.e_pac = self._entry(g, "Paquetes pacman (separados por espacios)", "deps", " ".join(dot.pacman))
        self.e_aur = Adw.EntryRow(title="Paquetes AUR", text=" ".join(dot.aur))
        self.e_aur.connect("changed", lambda *_: self._touched("deps"))
        g.add(self.e_aur)
        self.e_depnote = Adw.EntryRow(title="Nota sobre dependencias", text=dot.deps_note)
        g.add(self.e_depnote)

        g = self._group(title="Previsualización y recarga")
        self.e_readme = self._entry(g, "README", "readme", dot.readme)
        self.e_images = self._entry(g, "Capturas y vídeos (rutas del repo o URLs)", "images", " ".join(dot.images))
        self.e_cover = self._entry(g, "Portada (ruta del repo, URL o archivo local)", "cover",
                                   dot.cover_source() if dot.cover and not dot.cover.startswith("http")
                                   and dot.path else dot.cover)
        pick = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Elegir una imagen de tu equipo")
        pick.add_css_class("flat")
        pick.connect("clicked", lambda *_: self._pick_cover())
        self.e_cover.add_suffix(pick)
        self.e_reload = Adw.EntryRow(title="Comandos de recarga (separados por ;)", text="; ".join(dot.reload))
        g.add(self.e_reload)
        self.e_rnote = Adw.EntryRow(title="Nota tras instalar", text=dot.reload_note)
        g.add(self.e_rnote)
        self.e_warn = Adw.EntryRow(title="Avisos (separados por |)", text=" | ".join(dot.warnings))
        g.add(self.e_warn)

        if self.editing and self.editing.origin == "user":
            g = self._group()
            r = Adw.ButtonRow(title="Eliminar este dot del catálogo")
            r.add_css_class("destructive-action")
            r.connect("activated", lambda *_: self._delete())
            g.add(r)
        self._method_changed()
        self.validate()

    def _method_changed(self):
        script = self.c_method.get_selected() == 1
        for r in self.script_rows:
            r.set_visible(script)
        self.g_links.set_visible(not script)
        self.g_touch.set_visible(script)
        self.validate()

    def _use_alt(self):
        self.links = list(self.alt_links)
        self.c_method.set_selected(0)
        self.status["links"] = ("review", "carpetas detectadas en el repo: quita las que no quieras")
        self._rebuild_links()

    def _rebuild_links(self):
        g = self.g_links
        for r in getattr(self, "_link_rows", []):
            g.remove(r)
        self._link_rows = []
        head = Adw.ActionRow(title=f"{len(self.links)} rutas")
        self._mark(head, "links")
        self._link_rows.append(head)
        g.add(head)
        for i, l in enumerate(self.links):
            r = Adw.ActionRow(title=GLib.markup_escape_text(l.dest), subtitle=GLib.markup_escape_text(f"← {l.src}"))
            opt = Gtk.CheckButton(label="opcional", active=l.optional, valign=Gtk.Align.CENTER,
                                  tooltip_text="Opcional: no se instala salvo que lo marques al instalar")
            opt.connect("toggled", lambda c, i=i: (setattr(self.links[i], "optional", c.get_active()),
                                                   self._touched("links")))
            r.add_suffix(opt)
            rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Quitar")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, i=i: (self.links.pop(i), self._touched("links"), self._rebuild_links()))
            r.add_suffix(rm)
            self._link_rows.append(r)
            g.add(r)
        add = Adw.ActionRow(title="Añadir ruta")
        src = Gtk.Entry(placeholder_text="ruta en el repo (p.ej. .config/waybar)", valign=Gtk.Align.CENTER,
                        hexpand=True)
        dst = Gtk.Entry(placeholder_text="destino (p.ej. ~/.config/waybar)", valign=Gtk.Align.CENTER, hexpand=True)
        b = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)

        def do_add(*_):
            s, d = src.get_text().strip().strip("/"), dst.get_text().strip()
            if s and not d:
                d = "~/" + s if s.startswith(".") else f"~/.config/{s.split('/')[-1]}"
            if s:
                self.links.append(Link(src=s, dest=d))
                self._touched("links")
                self._rebuild_links()

        b.connect("clicked", do_add)
        src.connect("activate", do_add)
        dst.connect("activate", do_add)
        box = Gtk.Box(spacing=6, hexpand=True)
        box.append(src)
        box.append(dst)
        box.append(b)
        add.add_suffix(box)
        self._link_rows.append(add)
        g.add(add)
        self.validate()

    def _rebuild_touches(self):
        g = self.g_touch
        for r in getattr(self, "_touch_rows", []):
            g.remove(r)
        self._touch_rows = []
        head = Adw.ActionRow(title=f"{len(self.touches)} rutas")
        self._mark(head, "touches")
        self._touch_rows.append(head)
        g.add(head)
        for i, t in enumerate(self.touches):
            r = Adw.ActionRow(title=GLib.markup_escape_text(t))
            rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Quitar")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, i=i: (self.touches.pop(i), self._touched("touches"),
                                                  self._rebuild_touches()))
            r.add_suffix(rm)
            self._touch_rows.append(r)
            g.add(r)
        add = Adw.EntryRow(title="Añadir ruta (p.ej. ~/.config/quickshell)", show_apply_button=True)

        def do_add(*_):
            t = add.get_text().strip()
            if t:
                self.touches.append(t)
                self._touched("touches")
                self._rebuild_touches()

        add.connect("apply", do_add)
        add.connect("entry-activated", do_add)
        self._touch_rows.append(add)
        g.add(add)
        if self.status.get("touches", ("", ""))[0] == "review":
            self._touch_rows.append(self.review_touch)
            g.add(self.review_touch)
        self.validate()

    def _pick_cover(self):
        f = Gtk.FileFilter(name="Imágenes")
        f.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg = Gtk.FileDialog(title="Elegir portada", filters=filters)

        def done(d, r):
            try:
                file = d.open_finish(r)
            except GLib.Error:
                return
            if file and file.get_path():
                self.e_cover.set_text(file.get_path())

        dlg.open(self.win, None, done)

    # ------------------------------------------------------------ guardar
    def collect(self) -> Dot:
        d = copy.deepcopy(self.dot)
        d.id = self.e_id.get_text().strip()
        d.name = self.e_name.get_text().strip()
        d.author = self.e_author.get_text().strip()
        d.description = self.e_desc.get_text().strip()
        d.license = self.e_license.get_text().strip()
        d.repo = self.e_repo.get_text().strip()
        d.branch = self.e_branch.get_text().strip()
        d.method = METHODS[self.c_method.get_selected()][0]
        d.track = TRACKS[self.c_track.get_selected()][0]
        d.command = self.e_cmd.get_text().strip() if d.method == "script" else ""
        d.update_command = self.e_upd.get_text().strip() if d.method == "script" else ""
        d.uninstall_command = self.e_unc.get_text().strip() if d.method == "script" else ""
        d.links = list(self.links) if d.method == "files" else []
        d.touches = list(self.touches) if d.method == "script" else []
        d.pacman = split_list(self.e_pac.get_text())
        d.aur = split_list(self.e_aur.get_text())
        d.deps_note = self.e_depnote.get_text().strip()
        d.readme = self.e_readme.get_text().strip()
        d.images = split_list(self.e_images.get_text())
        d.cover = self.e_cover.get_text().strip()
        d.reload = [c.strip() for c in self.e_reload.get_text().split(";") if c.strip()]
        d.reload_note = self.e_rnote.get_text().strip()
        d.warnings = [w.strip() for w in self.e_warn.get_text().split("|") if w.strip()]
        d.detected_auto = sorted(k for k, (s, _) in self.status.items() if s == "auto")
        d.detected_manual = sorted(k for k, (s, _) in self.status.items() if s != "auto")
        return d

    def validate(self) -> bool:
        if not hasattr(self, "e_id"):
            return False
        problems = []
        try:
            d = self.collect()
            catalog.validate(d)
        except catalog.DotError as e:
            problems.append(str(e))
        except AttributeError:
            return False
        script = self.c_method.get_selected() == 1
        if script and self.status.get("touches", ("", ""))[0] == "review" and not self.review_touch.get_active():
            problems.append("confirma que has revisado las rutas que toca el instalador")
        if not self.e_name.get_text().strip():
            problems.append("falta el nombre")
        self.save_btn.set_sensitive(not problems)
        self.save_btn.set_tooltip_text("; ".join(problems) if problems else "Guardar en ~/.config/dotdeck/dots.d")
        return not problems

    def save(self):
        if not self.validate():
            return
        d = self.collect()
        old = self.editing
        clash = self.win.dots.get(d.id)
        if clash and (old is None or clash.id != old.id):
            alert(self.win, "Ese id ya existe", f"Ya hay un dot «{clash.name}» con id «{d.id}». Cambia el id.",
                  cancel=None, ok="Entendido")
            return
        if old and old.id != d.id and old.id in self.win.states:
            alert(self.win, "Está instalado", "No se puede cambiar el id de un dot instalado.",
                  cancel=None, ok="Entendido")
            return
        try:
            catalog.save_user_dot(d)
            if old and old.origin == "user" and old.id != d.id and old.path:
                old.path.unlink(missing_ok=True)
        except catalog.DotError as e:
            alert(self.win, "Definición no válida", str(e), cancel=None, ok="Entendido")
            return
        self.win.refresh()
        self.win.nav.pop_to_tag("main")
        self.win.toast(f"{d.name} guardado en el catálogo")
        self.win.open_detail(self.win.dots[d.id])

    def _delete(self):
        dot = self.editing
        if dot.id in self.win.states:
            alert(self.win, "Está instalado", "Desinstálalo antes de eliminarlo.", cancel=None, ok="Entendido")
            return

        def go(ok):
            if ok:
                catalog.delete_user_dot(dot)
                self.win.refresh()
                self.win.nav.pop_to_tag("main")
                self.win.toast(f"{dot.name} eliminado")

        alert(self.win, f"Eliminar {dot.name}", "Se borrará su definición del catálogo.", ok="Eliminar",
              danger=True, callback=go)
