"""Limpieza de huérfanos basada exclusivamente en manifiestos propios.

Un huérfano es una ruta que el ledger dice que DotDeck creó, que ningún dot
instalado reclama ya, y que sigue en disco *tal y como DotDeck la dejó*
(mismo symlink / misma huella). Nunca se busca por nombre o patrón.

Aparte se informa (sin borrar nunca) de symlinks rotos no registrados.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import backup, config, engine, fsutil
from .config import log


@dataclass
class Orphan:
    path: Path
    dot: str
    kind: str
    reason: str
    safe: bool          # coincide exactamente con lo registrado → seleccionable
    app_owned: bool = False  # carpeta interna de DotDeck (deploy sin estado)

    @property
    def display(self) -> str:
        return config.contract(self.path)


def _ledger_latest() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    try:
        lines = config.ledger_file().read_text().splitlines()
    except FileNotFoundError:
        return latest
    for l in lines:
        try:
            rec = json.loads(l)
        except ValueError:
            continue
        latest[rec["path"]] = rec
    return latest


def find() -> tuple[list[Orphan], list[str]]:
    """→ (huérfanos registrados, avisos de restos NO registrados que no se tocarán)."""
    states = engine.all_states()
    owned = {str(p) for st in states.values() for p in engine.owned_paths(st)}
    out: list[Orphan] = []
    for path, rec in _ledger_latest().items():
        if rec.get("kind") is None or path in owned:
            continue
        p = Path(path)
        k = fsutil.kind(p)
        if k is None:
            continue
        if rec["kind"] == "symlink":
            # Si ya no es nuestro enlace (p.ej. se restauró la config original), no es un resto.
            if k == "symlink" and os.readlink(p) == rec.get("target"):
                out.append(Orphan(p, rec["dot"], k, "enlace creado por DotDeck", True))
        else:
            fp, old = fsutil.fingerprint(p), rec.get("fp") or {}
            same = k == rec["kind"] and (k == "dir" or (fp and fp.get("size") == old.get("size")
                                                         and fp.get("mtime") == old.get("mtime")))
            out.append(Orphan(p, rec["dot"], k, "creado por el instalador del dot" if same
                              else "creado por el dot pero modificado después", bool(same)))
    dep = config.deploy_dir()
    if dep.is_dir():
        for d in dep.iterdir():
            if d.name.removesuffix(".partial") not in states or d.name.endswith(".partial"):
                out.append(Orphan(d, d.name.removesuffix(".partial"), "dir",
                                  "checkout de un dot que ya no está instalado", True, app_owned=True))
    # Solo informativo: symlinks rotos que DotDeck no registró.
    notes: list[str] = []
    ledger_paths = set(_ledger_latest())
    for root in (config.config_home(), config.home() / ".local/bin"):
        if root.is_dir():
            for n in os.listdir(root):
                p = root / n
                if p.is_symlink() and not p.exists() and str(p) not in ledger_paths:
                    notes.append(f"{config.contract(p)} → {os.readlink(p)} (symlink roto no creado por "
                                 "DotDeck: no se borra)")
    return sorted(out, key=lambda o: (not o.safe, o.display)), notes


def clean(paths: list[Path]) -> list[str]:
    """Borra solo rutas que find() sigue considerando huérfanas seguras."""
    current = {o.path: o for o in find()[0] if o.safe}
    chosen = [current[p] for p in paths if p in current]
    rejected = [config.contract(p) for p in paths if p not in current]
    if rejected:
        log.warning("rechazado borrar (no son huérfanos seguros): %s", rejected)
    real = [o.path for o in chosen if not o.app_owned and o.kind != "symlink"]
    if real:
        backup.create("huerfanos", "clean", real, note="antes de limpiar huérfanos")
    done = []
    for o in chosen:
        fsutil.remove_any(o.path)
        if not o.app_owned:
            engine.ledger_add(o.dot, o.path, None)
        done.append(f"borrado {o.display}")
    engine.history_add("huerfanos", "clean", True, f"{len(done)} huérfanos eliminados", items=done)
    return done + [f"NO borrado (no verificable): {r}" for r in rejected]
