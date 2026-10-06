"""Dependencias: qué falta y cómo instalarlo (siempre con confirmación del usuario)."""
from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

from .catalog import Dot
from .config import log


@dataclass
class DepPlan:
    missing_repo: list[str] = field(default_factory=list)
    missing_aur: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)   # ni en repos ni AUR helper disponible
    aur_helper: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.missing_repo or self.missing_aur or self.unknown)

    def commands(self) -> list[str]:
        cmds = []
        if self.missing_repo:
            cmds.append("sudo pacman -S --needed " + " ".join(map(shlex.quote, self.missing_repo)))
        if self.missing_aur and self.aur_helper:
            cmds.append(f"{self.aur_helper} -S --needed " + " ".join(map(shlex.quote, self.missing_aur)))
        return cmds


def aur_helper() -> str | None:
    for h in ("paru", "yay"):
        if shutil.which(h):
            return h
    return None


def missing(pkgs: list[str]) -> list[str]:
    """``pacman -T`` devuelve los que no satisfacen la base local (repos y AUR)."""
    if not pkgs:
        return []
    if not shutil.which("pacman"):
        log.warning("pacman no disponible: no se pueden comprobar dependencias")
        return []
    r = subprocess.run(["pacman", "-T", *pkgs], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def in_sync_db(pkg: str) -> bool:
    r = subprocess.run(["pacman", "-Sp", "--print-format", "%n", pkg],
                       capture_output=True, text=True)
    return r.returncode == 0


def plan(dot: Dot) -> DepPlan:
    p = DepPlan(aur_helper=aur_helper())
    for pkg in missing(dot.pacman):
        # Algunos autores listan como "pacman" paquetes que en realidad son AUR.
        if in_sync_db(pkg):
            p.missing_repo.append(pkg)
        else:
            (p.missing_aur if p.aur_helper else p.unknown).append(pkg)
    for pkg in missing(dot.aur):
        (p.missing_aur if p.aur_helper else p.unknown).append(pkg)
    log.info("dependencias %s: repo=%s aur=%s desconocidas=%s",
             dot.id, p.missing_repo, p.missing_aur, p.unknown)
    return p
