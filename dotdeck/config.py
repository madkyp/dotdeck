"""Rutas, ajustes y logging de DotDeck.

Todas las rutas se calculan en cada llamada a partir de $HOME / $XDG_*, de forma
que las pruebas pueden usar un $HOME de sandbox sin tocar la configuración real.
"""
from __future__ import annotations

import json
import logging
import os
import pwd
from pathlib import Path

APP_ID = "io.github.madkyp.DotDeck"
APP_NAME = "DotDeck"
VERSION = "0.1.0"

log = logging.getLogger("dotdeck")


def home() -> Path:
    return Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)


def is_real_home() -> bool:
    """True si $HOME es el home real del usuario (no un sandbox de pruebas)."""
    return home().resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else home() / default


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def config_dir() -> Path:
    return config_home() / "dotdeck"


def user_catalog_dir() -> Path:
    """Dots añadidos por el usuario (desde la app o a mano)."""
    return config_dir() / "dots.d"


def builtin_catalog_dir() -> Path:
    """Catálogo inicial que se distribuye con la app."""
    env = os.environ.get("DOTDECK_CATALOG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "dots.d"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "dotdeck"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "dotdeck"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "dotdeck"


def installed_dir() -> Path:
    return state_dir() / "installed"


def backups_dir() -> Path:
    return state_dir() / "backups"


def deploy_dir() -> Path:
    """Checkouts de los dots instalados (destino de los symlinks)."""
    return data_dir() / "deploy"


def preview_dir() -> Path:
    """Clones parciales (sin blobs) usados para previsualizar y detectar."""
    return cache_dir() / "preview"


def ledger_file() -> Path:
    return state_dir() / "ledger.jsonl"


def history_file() -> Path:
    return state_dir() / "history.jsonl"


def updates_file() -> Path:
    return state_dir() / "updates.json"


def log_file() -> Path:
    return state_dir() / "dotdeck.log"


DEFAULT_SETTINGS = {
    "accent": "#89b4fa",
    "check_on_login": True,
    "check_interval_hours": 6,
    "notify": True,
    "auto_reload_hyprland": True,
    "terminal": "auto",
    "translucent": True,
    "require_sudo": True,
    "ask_reboot": True,
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    p = config_dir() / "settings.json"
    try:
        s.update(json.loads(p.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("settings.json ilegible (%s); uso valores por defecto", e)
    return s


def save_settings(s: dict) -> None:
    p = config_dir() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    tmp.replace(p)


_logging_ready = False


def setup_logging(debug: bool = False) -> None:
    global _logging_ready
    if _logging_ready:
        if debug:
            log.setLevel(logging.DEBUG)
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.FileHandler(log_file(), encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    sh.setLevel(logging.DEBUG if debug else logging.WARNING)
    log.addHandler(sh)
    _logging_ready = True


def expand(p: str | Path) -> Path:
    """Expande ~ y $HOME contra el home actual (respeta el sandbox)."""
    s = str(p)
    if s == "~" or s.startswith("~/"):
        s = str(home()) + s[1:]
    s = s.replace("$HOME", str(home())).replace("${HOME}", str(home()))
    return Path(s)


def contract(p: str | Path) -> str:
    """Inverso de expand(): /home/x/.config → ~/.config."""
    s, h = str(p), str(home())
    if s == h:
        return "~"
    if s.startswith(h + "/"):
        return "~" + s[len(h):]
    return s
