"""Operaciones de ficheros con log (modo --debug) y huellas para verificar."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

from .config import log


def kind(p: Path) -> str | None:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "special"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_tree(p: Path) -> dict[str, str]:
    """Huella completa de un árbol: rel → 'file:<sha>:<mode>' | 'link:<target>' | 'dir:<mode>'.

    Se usa para verificar backups y para comprobar que una restauración deja
    todo exactamente igual.
    """
    out: dict[str, str] = {}

    def one(path: Path, rel: str) -> None:
        k = kind(path)
        if k == "symlink":
            out[rel] = "link:" + os.readlink(path)
        elif k == "file":
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            out[rel] = f"file:{sha256(path)}:{mode:o}"
        elif k == "dir":
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            out[rel] = f"dir:{mode:o}"
            for child in sorted(os.listdir(path)):
                one(path / child, f"{rel}/{child}" if rel != "." else child)
        elif k == "special":
            out[rel] = "special"

    if kind(p) is not None:
        one(p, ".")
    return out


def tree_size(p: Path) -> int:
    k = kind(p)
    if k in ("file",):
        return os.lstat(p).st_size
    if k == "dir":
        total = 0
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total
    return 0


def copy_any(src: Path, dst: Path) -> None:
    """Copia preservando symlinks, permisos y fechas. Omite ficheros especiales."""
    k = kind(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    log.debug("copy %s → %s (%s)", src, dst, k)
    if k == "symlink":
        os.symlink(os.readlink(src), dst)
    elif k == "dir":
        shutil.copytree(src, dst, symlinks=True, ignore=_ignore_special, copy_function=shutil.copy2)
    elif k == "file":
        shutil.copy2(src, dst, follow_symlinks=False)
    else:
        log.warning("omitido fichero especial %s", src)


def _ignore_special(dirpath: str, names: list[str]) -> list[str]:
    skip = []
    for n in names:
        if kind(Path(dirpath) / n) == "special":
            log.warning("omitido fichero especial %s/%s", dirpath, n)
            skip.append(n)
    return skip


def remove_any(p: Path) -> None:
    k = kind(p)
    log.debug("remove %s (%s)", p, k)
    if k is None:
        return
    if k == "dir":
        shutil.rmtree(p)
    else:
        os.unlink(p)


def symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    log.debug("symlink %s → %s", link, target)
    os.symlink(target, link)


def fingerprint(p: Path) -> dict | None:
    """Huella ligera (sin hash) usada por los snapshots antes/después."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    k = kind(p)
    fp = {"kind": k, "size": st.st_size, "mtime": st.st_mtime_ns}
    if k == "symlink":
        fp["target"] = os.readlink(p)
    return fp


def free_bytes(p: Path) -> int:
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free
