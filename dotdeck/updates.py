"""Comprobación periódica de actualizaciones + temporizador systemd de usuario."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import catalog, config, engine, gitrepo
from .config import log

UNIT = "dotdeck-update-check"


def check_all() -> dict:
    """Consulta los repos de los dots instalados y guarda el resultado en updates.json."""
    dots, _ = catalog.load_all()
    engine.sync_external(dots)
    res = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "available": {}, "errors": {}}
    for dot_id, st in engine.all_states().items():
        dot = dots.get(dot_id)
        if not dot:
            res["errors"][dot_id] = "su definición ya no está en el catálogo"
            continue
        try:
            rv = engine.check_update(dot)
        except gitrepo.GitError as e:
            res["errors"][dot_id] = str(e)
            log.warning("no se pudo comprobar %s: %s", dot_id, e)
            continue
        if rv:
            res["available"][dot_id] = {"name": dot.name, "installed": st["version"]["label"],
                                        "latest": rv["label"]}
    p = config.updates_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    log.info("comprobación de actualizaciones: %s", list(res["available"]))
    return res


def load() -> dict:
    try:
        return json.loads(config.updates_file().read_text())
    except (OSError, ValueError):
        return {"available": {}, "errors": {}}


def notify(res: dict) -> None:
    av = res.get("available", {})
    if not av or not shutil.which("notify-send"):
        return
    body = "\n".join(f"• {v['name']}: {v['installed']} → {v['latest']}" for v in av.values())
    title = f"DotDeck: {len(av)} actualización(es) disponible(s)"
    try:
        r = subprocess.run(["notify-send", "-a", "DotDeck", "-i", "system-software-update",
                            "-A", "open=Abrir DotDeck", "-t", "60000", title, body],
                           capture_output=True, text=True, timeout=90)
        if r.stdout.strip() == "open":
            subprocess.Popen([sys.executable, "-m", "dotdeck", "--page", "installed"],
                             start_new_session=True)
    except subprocess.TimeoutExpired:
        pass


# ------------------------------------------------------------------ systemd --user
def _unit_dir() -> Path:
    return config.config_home() / "systemd" / "user"


def launcher() -> str:
    exe = shutil.which("dotdeck")
    if exe:
        return exe
    root = Path(__file__).resolve().parent.parent
    return f"/usr/bin/env PYTHONPATH={root} {sys.executable} -m dotdeck"


def install_timer(hours: float, on_login: bool) -> None:
    d = _unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{UNIT}.service").write_text(
        "[Unit]\nDescription=DotDeck: buscar actualizaciones de dots\n"
        "After=network-online.target\n\n"
        f"[Service]\nType=oneshot\nExecStart={launcher()} --check-updates --notify\n")
    boot = "OnStartupSec=3min\n" if on_login else f"OnActiveSec={int(hours * 60)}min\n"
    (d / f"{UNIT}.timer").write_text(
        "[Unit]\nDescription=DotDeck: comprobación periódica de actualizaciones\n\n"
        f"[Timer]\n{boot}OnUnitActiveSec={int(hours * 60)}min\nPersistent=false\n\n"
        "[Install]\nWantedBy=timers.target\n")
    if config.is_real_home():
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=False)
        subprocess.run(["systemctl", "--user", "restart", f"{UNIT}.timer"], check=False)
    log.info("temporizador instalado: cada %sh, al iniciar sesión=%s", hours, on_login)


def remove_timer() -> None:
    if config.is_real_home():
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
    for ext in ("service", "timer"):
        (_unit_dir() / f"{UNIT}.{ext}").unlink(missing_ok=True)
    if config.is_real_home():
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def timer_active() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-enabled", f"{UNIT}.timer"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "enabled"
