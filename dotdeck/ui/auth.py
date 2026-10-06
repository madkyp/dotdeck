"""Confirmación con contraseña de sudo antes de instalar, actualizar o recargar.

La contraseña solo se pasa por stdin a ``sudo -S -k -v`` para comprobarla:
no se guarda, no se registra en el log y se descarta en cuanto sudo responde.
"""
from __future__ import annotations

import shutil
import subprocess
import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import config  # noqa: E402
from ..config import log  # noqa: E402

MAX_TRIES = 3  # pam_faillock bloquea la cuenta tras varios fallos: no insistir


def check_password(password: str) -> bool:
    try:
        r = subprocess.run(["sudo", "-S", "-k", "-v", "-p", ""], input=password + "\n",
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        password = ""  # noqa: F841


def require_sudo(parent: Gtk.Widget, action: str, on_ok) -> None:
    """Pide la contraseña de sudo y llama a ``on_ok()`` solo si es correcta."""
    if not config.load_settings().get("require_sudo", True):
        on_ok()
        return
    if not shutil.which("sudo"):
        on_ok()
        return
    _ask(parent, action, on_ok, 1)


def _ask(parent, action, on_ok, attempt: int, error: str = "") -> None:
    d = Adw.AlertDialog(heading="Autorización requerida",
                        body=f"Introduce tu contraseña de sudo para {action}."
                             + (f"\n\n{error}" if error else ""))
    entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True,
                              placeholder_text="Contraseña de sudo")
    d.set_extra_child(entry)
    d.add_response("cancel", "Cancelar")
    d.add_response("ok", "Autorizar")
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")

    def resp(_d, r):
        pw = entry.get_text()
        entry.set_text("")
        if r != "ok":
            return
        spinner = Adw.Dialog(title="Comprobando…", content_width=260, can_close=False)
        spinner.set_child(Adw.Spinner(width_request=32, height_request=32, margin_top=24, margin_bottom=24))
        spinner.present(parent)

        def work(password=pw):
            ok = check_password(password)
            GLib.idle_add(done, ok)

        def done(ok):
            spinner.set_can_close(True)
            spinner.force_close()
            if ok:
                log.info("autorización sudo concedida para: %s", action)
                on_ok()
            elif attempt < MAX_TRIES:
                _ask(parent, action, on_ok, attempt + 1,
                     f"⚠ Contraseña incorrecta (intento {attempt} de {MAX_TRIES}).")
            else:
                log.warning("autorización sudo denegada para: %s", action)
                parent.toast("Autorización cancelada tras varios intentos fallidos")
            return False

        threading.Thread(target=work, daemon=True).start()

    d.connect("response", resp)
    d.present(parent)
    entry.grab_focus()
