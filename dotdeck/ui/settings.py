"""Ajustes: aspecto, terminal y comprobación automática de actualizaciones."""
from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from .. import config, runner, updates  # noqa: E402

ACCENTS = ["#89b4fa", "#cba6f7", "#f5c2e7", "#a6e3a1", "#fab387", "#94e2d5", "#f38ba8", "#f9e2af"]


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Ajustes")
        self.win = win
        self.s = config.load_settings()

        p = Adw.PreferencesPage(title="Aspecto", icon_name="applications-graphics-symbolic")
        g = Adw.PreferencesGroup(title="Interfaz")
        row = Adw.ActionRow(title="Color de acento")
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for c in ACCENTS:
            b = Gtk.Button(width_request=26, height_request=26, tooltip_text=c)
            b.add_css_class("circular")
            prov = Gtk.CssProvider()
            prov.load_from_string(f"button {{ background: {c}; min-width: 22px; min-height: 22px; }}")
            b.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            b.connect("clicked", lambda _b, col=c: self._accent(col))
            box.append(b)
        cdb = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.parse(self.s["accent"])
        cdb.set_rgba(rgba)
        cdb.connect("notify::rgba", lambda b, *_: self._accent(_hex(b.get_rgba())))
        box.append(cdb)
        row.add_suffix(box)
        g.add(row)
        tr = Adw.SwitchRow(title="Fondo translúcido", subtitle="Hyprland aplica el desenfoque si lo tienes activo",
                           active=self.s.get("translucent", True))
        tr.connect("notify::active", lambda r, *_: self._set("translucent", r.get_active(), restyle=True))
        g.add(tr)
        p.add(g)
        g = Adw.PreferencesGroup(title="Terminal",
                                 description="Los instaladores de los autores y pacman/AUR se ejecutan en una "
                                             "terminal visible: ves el comando y tecleas tú la contraseña de sudo.")
        terms = ["auto"] + [t for t in runner.TERMINALS if __import__("shutil").which(t)]
        cr = Adw.ComboRow(title="Terminal", model=Gtk.StringList.new(terms))
        cur = self.s.get("terminal", "auto")
        cr.set_selected(terms.index(cur) if cur in terms else 0)
        cr.connect("notify::selected", lambda r, *_: self._set("terminal", terms[r.get_selected()]))
        g.add(cr)
        p.add(g)
        self.add(p)

        p = Adw.PreferencesPage(title="Actualizaciones", icon_name="software-update-available-symbolic")
        g = Adw.PreferencesGroup(title="Comprobación automática",
                                 description="Un temporizador systemd de usuario (dotdeck-update-check.timer) "
                                             "consulta los repositorios de tus dots instalados y te avisa con "
                                             "una notificación.")
        self.enabled = Adw.SwitchRow(title="Activar comprobación en segundo plano", active=_timer())
        self.enabled.connect("notify::active", lambda *_: self._timer())
        g.add(self.enabled)
        self.login = Adw.SwitchRow(title="Comprobar al iniciar sesión", active=self.s["check_on_login"])
        self.login.connect("notify::active", lambda r, *_: (self._set("check_on_login", r.get_active()), self._timer()))
        g.add(self.login)
        self.hours = Adw.SpinRow.new_with_range(1, 168, 1)
        self.hours.set_title("Cada cuántas horas")
        self.hours.set_value(self.s["check_interval_hours"])
        self.hours.connect("notify::value", lambda r, *_: (self._set("check_interval_hours", int(r.get_value())),
                                                           self._timer()))
        g.add(self.hours)
        nt = Adw.SwitchRow(title="Notificación del sistema", active=self.s["notify"])
        nt.connect("notify::active", lambda r, *_: self._set("notify", r.get_active()))
        g.add(nt)
        p.add(g)
        g = Adw.PreferencesGroup(title="Seguridad")
        sd = Adw.SwitchRow(title="Pedir contraseña de sudo",
                           subtitle="Antes de instalar, actualizar o recargar Hyprland",
                           active=self.s.get("require_sudo", True))
        sd.connect("notify::active", lambda r, *_: self._set("require_sudo", r.get_active()))
        g.add(sd)
        p.add(g)
        g = Adw.PreferencesGroup(title="Tras instalar o actualizar")
        rb = Adw.SwitchRow(title="Proponer reiniciar el equipo",
                           subtitle="Al terminar, pregunta si quieres reiniciar ahora o más tarde",
                           active=self.s.get("ask_reboot", True))
        rb.connect("notify::active", lambda r, *_: self._set("ask_reboot", r.get_active()))
        g.add(rb)
        rl = Adw.SwitchRow(title="Recargar Hyprland automáticamente",
                           subtitle="Ejecuta hyprctl reload y comprueba errores de configuración",
                           active=self.s["auto_reload_hyprland"])
        rl.connect("notify::active", lambda r, *_: self._set("auto_reload_hyprland", r.get_active()))
        g.add(rl)
        p.add(g)
        self.add(p)

    def _set(self, k, v, restyle=False):
        self.s[k] = v
        config.save_settings(self.s)
        if restyle:
            self.win.get_application().apply_style()

    def _accent(self, col: str):
        self._set("accent", col, restyle=True)

    def _timer(self):
        if self.enabled.get_active():
            updates.install_timer(self.s["check_interval_hours"], self.s["check_on_login"])
        else:
            updates.remove_timer()
        self.win.installed.rebuild()


def _hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))


def _timer() -> bool:
    try:
        return updates.timer_active()
    except Exception:  # noqa: BLE001
        return False
