"""Motor de backups.

Estructura de un backup:
  ~/.local/state/dotdeck/backups/<YYYYmmdd-HHMMSS>_<dot>_<accion>/
      manifest.json   ← qué se guardó, de dónde, si existía y su huella completa
      files/<n>       ← copia exacta de la ruta n (symlinks incluidos)

Un backup solo se marca "complete" tras verificar que la copia coincide hash a
hash con el original. Las rutas que NO existían también se registran
(existed=false): restaurar significa entonces borrar lo que haya ahí ahora, de
modo que el sistema queda exactamente como estaba.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, fsutil
from .config import log

MARGIN = 64 * 1024 * 1024


class BackupError(Exception):
    pass


@dataclass
class Backup:
    id: str
    path: Path
    manifest: dict

    @property
    def dot(self) -> str:
        return self.manifest.get("dot", "")

    @property
    def action(self) -> str:
        return self.manifest.get("action", "")

    @property
    def created(self) -> str:
        return self.manifest.get("created", "")

    @property
    def entries(self) -> list[dict]:
        return self.manifest.get("entries", [])

    @property
    def complete(self) -> bool:
        return self.manifest.get("status") == "complete"

    def size(self) -> int:
        return fsutil.tree_size(self.path / "files")


def _normalize(paths: list[Path]) -> list[Path]:
    """Quita duplicados y rutas contenidas en otras (ya respaldadas por el padre)."""
    uniq = sorted({Path(p) for p in paths}, key=lambda p: len(p.parts))
    out: list[Path] = []
    for p in uniq:
        if not any(p == q or q in p.parents for q in out):
            out.append(p)
    return out


def create(dot: str, action: str, paths: list[Path], note: str = "") -> Backup:
    paths = _normalize(paths)
    need = sum(fsutil.tree_size(p) for p in paths)
    root = config.backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    free = fsutil.free_bytes(root)
    if free < need + MARGIN:
        raise BackupError(
            f"Espacio insuficiente para el backup: hacen falta {need/1e6:.1f} MB "
            f"(+{MARGIN/1e6:.0f} MB de margen) y hay {free/1e6:.1f} MB libres en {root}")
    bid = f"{time.strftime('%Y%m%d-%H%M%S')}_{dot}_{action}"
    n = 1
    while (root / bid).exists():
        n += 1
        bid = f"{time.strftime('%Y%m%d-%H%M%S')}_{dot}_{action}-{n}"
    bdir = root / bid
    (bdir / "files").mkdir(parents=True)
    manifest = {
        "id": bid, "dot": dot, "action": action, "note": note,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "home": str(config.home()),
        "status": "incomplete", "entries": [],
    }
    _write(bdir, manifest)
    log.info("backup %s: %d rutas, %.1f MB", bid, len(paths), need / 1e6)
    try:
        for i, p in enumerate(paths):
            k = fsutil.kind(p)
            entry = {"path": str(p), "display": config.contract(p), "existed": k is not None,
                     "kind": k, "stored": None, "digest": {}}
            if k is not None:
                stored = f"files/{i}"
                fsutil.copy_any(p, bdir / stored)
                entry["stored"] = stored
                entry["digest"] = fsutil.digest_tree(p)
            manifest["entries"].append(entry)
        verify(Backup(bid, bdir, manifest))
    except Exception as e:
        log.error("backup %s fallido: %s", bid, e)
        manifest["status"] = "failed"
        manifest["error"] = str(e)
        _write(bdir, manifest)
        fsutil.remove_any(bdir / "files")
        raise BackupError(f"Backup incompleto, no se ha tocado nada: {e}") from e
    manifest["status"] = "complete"
    _write(bdir, manifest)
    return Backup(bid, bdir, manifest)


def verify(b: Backup) -> None:
    """Comprueba que cada copia coincide con la huella registrada."""
    for e in b.entries:
        if not e["existed"]:
            continue
        got = fsutil.digest_tree(b.path / e["stored"])
        if got != e["digest"]:
            missing = set(e["digest"]) ^ set(got)
            diff = [k for k in e["digest"] if k in got and got[k] != e["digest"][k]]
            raise BackupError(
                f"la copia de {e['display']} no coincide con el original "
                f"(faltan/sobran: {sorted(missing)[:5]}, distintos: {diff[:5]})")


def _write(bdir: Path, manifest: dict) -> None:
    tmp = bdir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    tmp.replace(bdir / "manifest.json")


def load(bid: str) -> Backup:
    p = config.backups_dir() / bid
    try:
        return Backup(bid, p, json.loads((p / "manifest.json").read_text()))
    except (OSError, ValueError) as e:
        raise BackupError(f"backup {bid} ilegible: {e}") from e


def list_all() -> list[Backup]:
    out = []
    root = config.backups_dir()
    if root.is_dir():
        for d in root.iterdir():
            if (d / "manifest.json").exists():
                try:
                    out.append(load(d.name))
                except BackupError as e:
                    log.warning("%s", e)
    out.sort(key=lambda b: b.id, reverse=True)
    return out


def restore(bid: str, only: list[Path] | None = None, safety: bool = True) -> list[str]:
    """Restaura un backup completo (o solo las rutas ``only``).

    Antes de tocar nada verifica el backup y, si ``safety``, crea un backup de
    seguridad del estado actual para que la propia reversión sea reversible.
    Devuelve la lista de acciones realizadas (legibles).
    """
    b = load(bid)
    if not b.complete:
        raise BackupError(f"el backup {bid} no está completo ({b.manifest.get('status')}); no se restaura")
    verify(b)
    entries = [e for e in b.entries if only is None or Path(e["path"]) in only]
    if safety and entries:
        create(b.dot or "sistema", "pre-restore", [Path(e["path"]) for e in entries],
               note=f"estado antes de restaurar {bid}")
    done = []
    for e in entries:
        p = Path(e["path"])
        if fsutil.kind(p) is not None:
            fsutil.remove_any(p)
        if e["existed"]:
            fsutil.copy_any(b.path / e["stored"], p)
            if fsutil.digest_tree(p) != e["digest"]:
                raise BackupError(f"tras restaurar, {e['display']} no coincide con el backup")
            done.append(f"restaurado {e['display']}")
        else:
            done.append(f"eliminado {e['display']} (no existía antes)")
    log.info("restaurado backup %s: %s", bid, done)
    return done


def delete(bid: str) -> None:
    p = config.backups_dir() / bid
    if p.parent != config.backups_dir() or not (p / "manifest.json").exists():
        raise BackupError(f"{bid} no es un backup de DotDeck")
    fsutil.remove_any(p)
    log.info("backup eliminado %s", bid)
