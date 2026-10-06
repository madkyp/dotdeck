"""Puente entre el motor (hilos de trabajo) y GTK (hilo principal)."""
from __future__ import annotations

import threading
import traceback

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import engine, runner  # noqa: E402
from ..config import log  # noqa: E402


def details_widget(details: list[str]) -> Gtk.Widget:
    lbl = Gtk.Label(label="\n".join(details), xalign=0, selectable=True, wrap=True)
    lbl.add_css_class("monospace")
    lbl.add_css_class("dim-label")
    sw = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=260,
                            hscrollbar_policy=Gtk.PolicyType.NEVER)
    sw.set_child(lbl)
    sw.add_css_class("details-box")
    return sw


def alert(parent: Gtk.Widget, title: str, body: str, details: list[str] | None = None,
          ok: str = "Continuar", danger: bool = False, cancel: str | None = "Cancelar",
          callback=None) -> Adw.AlertDialog:
    d = Adw.AlertDialog(heading=title, body=body)
    if cancel:
        d.add_response("cancel", cancel)
    d.add_response("ok", ok)
    d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if danger
                              else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok" if not danger else "cancel")
    d.set_close_response("cancel" if cancel else "ok")
    if details:
        d.set_extra_child(details_widget(details))
    d.connect("response", lambda _d, r: callback and callback(r == "ok"))
    d.present(parent)
    return d


def ask_reboot() -> bool:
    from .. import config
    return config.load_settings().get("ask_reboot", True) and config.is_real_home()


def reboot_dialog(parent, res) -> None:
    """Tras instalar/actualizar: proponer reiniciar (con el resumen de avisos)."""
    import subprocess
    d = Adw.AlertDialog(heading="Reiniciar el equipo",
                        body=f"{res.message}.\n\nPara que todos los cambios se apliquen bien (sesión, servicios, "
                             "temas) conviene reiniciar. Guarda antes tu trabajo.")
    if res.warnings:
        d.set_extra_child(details_widget(res.warnings))
    d.add_response("later", "Más tarde")
    d.add_response("reboot", "Reiniciar ahora")
    d.set_response_appearance("reboot", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("later")
    d.set_close_response("later")

    def resp(_d, r):
        if r == "reboot":
            log.info("reinicio solicitado por el usuario tras %s", res.message)
            subprocess.Popen(["systemctl", "reboot"], start_new_session=True)
        else:
            parent.toast("Recuerda reiniciar más tarde para aplicar todos los cambios")

    d.connect("response", resp)
    d.present(parent)


class GuiUI(engine.UI):
    """Implementación de engine.UI para la interfaz: bloquea el hilo de trabajo
    mientras el diálogo correspondiente espera respuesta en el hilo principal."""

    def __init__(self, op: "Operation"):
        self.op = op

    def progress(self, msg: str) -> None:
        log.info("· %s", msg)
        GLib.idle_add(self.op.set_status, msg)

    def confirm(self, title, body, details=None, ok="Continuar", danger=False) -> bool:
        ev = threading.Event()
        res = {"ok": False}

        def show():
            def done(v):
                res["ok"] = v
                ev.set()
            alert(self.op.dialog, title, body, details, ok=ok, danger=danger, callback=done)
            return False

        GLib.idle_add(show)
        ev.wait()
        return res["ok"]

    def run_terminal(self, cmd, cwd, title) -> int:
        GLib.idle_add(self.op.set_status, f"Esperando a la terminal: {title}…\n$ {cmd}")
        return runner.run(cmd, cwd, title, interactive=True, on_stall=self.on_stall)


class Operation:
    """Ejecuta una operación del motor en segundo plano con un diálogo de progreso."""

    def __init__(self, window, title: str, fn, on_done=None):
        self.window, self.fn, self.on_done = window, fn, on_done
        self.dialog = Adw.Dialog(title=title, content_width=460, can_close=False)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12, margin_bottom=24,
                      margin_start=24, margin_end=24)
        self.spinner = Adw.Spinner(width_request=40, height_request=40)
        box.append(self.spinner)
        self.status = Gtk.Label(label="Preparando…", wrap=True, justify=Gtk.Justification.CENTER)
        box.append(self.status)
        self.steps = Gtk.Label(xalign=0, wrap=True)
        self.steps.add_css_class("dim-label")
        self.steps.add_css_class("caption")
        box.append(self.steps)
        tv.set_content(box)
        self.dialog.set_child(tv)
        self._lines: list[str] = []

    def set_status(self, msg: str) -> bool:
        self.status.set_label(msg)
        self._lines = (self._lines + [msg])[-6:]
        self.steps.set_label("\n".join("✓ " + l for l in self._lines[:-1]))
        return False

    def start(self) -> None:
        self.dialog.present(self.window)
        ui = GuiUI(self)
        threading.Thread(target=self._run, args=(ui,), daemon=True).start()

    def _run(self, ui: GuiUI) -> None:
        try:
            res = self.fn(ui)
            GLib.idle_add(self._finish, res, None)
        except engine.Cancelled as e:
            GLib.idle_add(self._finish, None, e)
        except Exception as e:  # noqa: BLE001
            log.error("operación fallida: %s\n%s", e, traceback.format_exc())
            GLib.idle_add(self._finish, None, e)

    def _finish(self, res, err) -> bool:
        self.dialog.set_can_close(True)
        self.dialog.force_close()
        if isinstance(err, engine.Cancelled):
            self.window.toast(str(err) or "Cancelado")
        elif err is not None:
            alert(self.window, "Algo ha fallado", str(err), cancel=None, ok="Entendido")
        elif isinstance(res, engine.Result) and res.ok and res.reboot and ask_reboot():
            reboot_dialog(self.window, res)
        elif isinstance(res, engine.Result):
            if res.warnings:
                alert(self.window, "Hecho" if res.ok else "No completado", res.message, res.warnings,
                      cancel=None, ok="Entendido")
            else:
                self.window.toast(res.message)
        elif isinstance(res, str):
            self.window.toast(res)
        if self.on_done:
            self.on_done(res, err)
        self.window.refresh()
        return False
