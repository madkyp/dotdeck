"""Ejecución de comandos interactivos (instaladores de autores, pacman/yay).

Todo lo que pueda pedir sudo o hacer preguntas se ejecuta en una terminal
visible para que el usuario vea el comando y teclee su contraseña él mismo.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config
from .config import log

TERMINALS = {
    "kitty": ["kitty", "--title", "{title}", "--"],
    "ghostty": ["ghostty", "--gtk-single-instance=false", "--title={title}", "-e"],
    "alacritty": ["alacritty", "--title", "{title}", "-e"],
    "foot": ["foot", "--title", "{title}"],
    "wezterm": ["wezterm", "start", "--"],
    "konsole": ["konsole", "-e"],
    "xterm": ["xterm", "-T", "{title}", "-e"],
}


def pick_terminal() -> str | None:
    pref = config.load_settings().get("terminal", "auto")
    if pref != "auto" and shutil.which(pref):
        return pref
    for t in TERMINALS:
        if shutil.which(t):
            return t
    return None


def _supervise(proc: subprocess.Popen, on_stall) -> None:
    """Espera a la terminal vigilando bloqueos (ver watchdog.py).

    ``on_stall(stall) -> bool``: si devuelve True se detienen los procesos que bloquean.
    """
    from . import watchdog
    seen: dict[tuple, float] = {}
    asked: set[tuple] = set()
    while proc.poll() is None:
        try:
            proc.wait(timeout=4)
            break
        except subprocess.TimeoutExpired:
            pass
        try:
            st = watchdog.check(proc.pid)
        except Exception as e:  # noqa: BLE001 — el vigilante nunca debe romper la instalación
            log.debug("watchdog: %s", e)
            continue
        if not st:
            seen.clear()
            continue
        key = (st.waiting_pid, tuple(p for p, _ in st.blockers))
        first = seen.setdefault(key, time.monotonic())
        if time.monotonic() - first < 20 or key in asked:
            continue
        asked.add(key)
        log.warning("instalador bloqueado: %s", st.describe())
        if on_stall is None:
            continue
        if on_stall(st):
            watchdog.unblock(st)


def run(cmd: str, cwd: Path, title: str, interactive: bool = True,
        env: dict | None = None, on_stall=None) -> int:
    """Ejecuta ``cmd`` (shell) y devuelve su código de salida.

    interactive=True  → nueva ventana de terminal (bloquea hasta que se cierre).
    interactive=False → en el proceso actual (stdin/stdout heredados; CLI y tests).
    """
    full_env = {**os.environ, **(env or {})}
    log.info("ejecutando [%s] en %s: %s", title, cwd, cmd)
    if not interactive:
        proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, env=full_env)
        _supervise(proc, on_stall)
        return proc.wait()
    term = pick_terminal()
    if not term:
        raise RuntimeError("No se encontró ninguna terminal (kitty, ghostty, alacritty…)")
    with tempfile.TemporaryDirectory(prefix="dotdeck-") as td:
        status = Path(td) / "status"
        script = (
            f"cd {shlex.quote(str(cwd))} || exit 99\n"
            f"printf '\\e[1;36m▶ DotDeck — %s\\e[0m\\n$ %s\\n\\n' {shlex.quote(title)} {shlex.quote(cmd)}\n"
            f"( {cmd} )\n"
            f"rc=$?\necho $rc > {shlex.quote(str(status))}\n"
            "echo\nif [ $rc -eq 0 ]; then printf '\\e[1;32m✔ Terminado (código 0)\\e[0m\\n';"
            " else printf '\\e[1;31m✘ Falló (código %s)\\e[0m\\n' $rc; fi\n"
            "read -rp 'Pulsa Enter para volver a DotDeck… ' _\n"
        )
        sf = Path(td) / "run.sh"
        sf.write_text(script)
        argv = [a.replace("{title}", f"DotDeck: {title}") for a in TERMINALS[term]]
        argv += ["bash", str(sf)]
        log.debug("terminal: %s", argv)
        proc = subprocess.Popen(argv, cwd=cwd, env=full_env)
        _supervise(proc, on_stall)
        try:
            return int(status.read_text().strip())
        except (OSError, ValueError):
            log.warning("la terminal se cerró sin código de salida; se trata como fallo")
            return 130
