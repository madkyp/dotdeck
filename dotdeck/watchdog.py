"""Detecta instaladores bloqueados por un proceso en segundo plano que ellos mismos lanzaron.

Caso real (HyDE): ``theme.switch.sh`` arranca ``awww-daemon`` dentro de una sustitución
de comandos ``$(...)``; el demonio hereda la salida del script, así que el script espera
para siempre un fin de datos que nunca llega. Patrón detectado:

  * un proceso del instalador, sin hijos, dormido leyendo de una tubería (pipe);
  * todos los demás extremos de esa tubería los tienen procesos que YA NO son
    descendientes del instalador (demonios que se separaron) y que nacieron después
    de empezar el instalador.

Solo se proponen para detener procesos que cumplen todo eso (nunca algo previo).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import log

CLK = os.sysconf("SC_CLK_TCK")


@dataclass
class Stall:
    waiting_pid: int
    waiting_cmd: str
    blockers: list[tuple[int, str]] = field(default_factory=list)

    def describe(self) -> list[str]:
        return ([f"Esperando: {self.waiting_cmd} (pid {self.waiting_pid})"]
                + [f"Lo bloquea: {c} (pid {p})" for p, c in self.blockers])


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _cmd(pid: int) -> str:
    return _read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()[:160]


def _stat(pid: int) -> tuple[int, str, int] | None:
    """→ (ppid, estado, inicio en ticks)."""
    s = _read(f"/proc/{pid}/stat")
    if not s:
        return None
    rest = s[s.rfind(")") + 2:].split()
    return int(rest[1]), rest[0], int(rest[19])


def _my_pids() -> list[int]:
    uid = os.getuid()
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                if os.stat(f"/proc/{d}").st_uid == uid:
                    out.append(int(d))
            except OSError:
                pass
    return out


def _pipes(pid: int) -> set[str]:
    res = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                t = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if t.startswith("pipe:"):
                res.add(t)
    except OSError:
        pass
    return res


def check(root_pid: int) -> Stall | None:
    """Busca el patrón de bloqueo bajo ``root_pid`` (la terminal del instalador)."""
    pids = _my_pids()
    info = {p: st for p in pids if (st := _stat(p))}
    if root_pid not in info:
        return None
    root_start = info[root_pid][2]
    children: dict[int, list[int]] = {}
    for p, (pp, _, _) in info.items():
        children.setdefault(pp, []).append(p)
    desc, stack = {root_pid}, [root_pid]  # la raíz también cuenta (bash -c puede hacer exec del script)
    while stack:
        p = stack.pop()
        for c in children.get(p, []):
            if c not in desc:
                desc.add(c)
                stack.append(c)
    for p in desc:
        if info[p][1] != "S" or children.get(p):
            continue
        if "pipe" not in _read(f"/proc/{p}/wchan"):
            continue
        mine = _pipes(p)
        if not mine:
            continue
        holders: dict[int, set[str]] = {}
        for q in pids:
            if q == p:
                continue
            common = _pipes(q) & mine
            if common:
                holders[q] = common
        if not holders:
            continue
        # Extremos en manos de procesos fuera del árbol del instalador y nacidos después de él.
        foreign = [q for q in holders if q not in desc and q in info and info[q][2] >= root_start]
        inside = [q for q in holders if q in desc]
        if foreign and not inside:
            return Stall(p, _cmd(p), [(q, _cmd(q)) for q in foreign])
    return None


def unblock(stall: Stall) -> list[str]:
    """Detiene (SIGTERM) los procesos que bloquean. Devuelve lo hecho."""
    done = []
    for pid, cmd in stall.blockers:
        try:
            os.kill(pid, 15)
            done.append(f"detenido {cmd} (pid {pid})")
            log.warning("watchdog: detenido %s (pid %s) que bloqueaba %s", cmd, pid, stall.waiting_cmd)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            done.append(f"no se pudo detener {cmd}: {e}")
    return done


def short(cmd: str) -> str:
    return re.sub(r"^\S*/", "", cmd.split(" ")[0]) if cmd else "?"
