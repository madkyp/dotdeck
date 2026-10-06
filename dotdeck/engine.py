"""Motor de instalación / actualización / desinstalación.

Comparte con backup.py y orphans.py un único sistema de registro:
  * installed/<id>.json  → qué tiene instalado cada dot ahora mismo
  * ledger.jsonl         → TODO lo que DotDeck ha creado alguna vez (base de la
                           limpieza de huérfanos: nada fuera de aquí se borra)
  * history.jsonl        → historial legible de operaciones

Las operaciones reciben un objeto ``ui`` (ver UI más abajo) para mostrar
progreso, pedir confirmaciones y abrir terminales; así el mismo motor sirve a
la interfaz GTK, a la CLI y a los tests.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import backup, config, deps, fsutil, gitrepo, runner
from .catalog import Dot, Link
from .config import log


class EngineError(Exception):
    pass


class Cancelled(EngineError):
    pass


# ------------------------------------------------------------------ UI
class UI:
    """Interfaz mínima que el motor necesita. La CLI y la GUI la implementan."""

    skip_deps = False  # solo informar de dependencias que faltan, sin instalar nada

    def progress(self, msg: str) -> None:
        log.info("· %s", msg)

    def confirm(self, title: str, body: str, details: list[str] | None = None,
                ok: str = "Continuar", danger: bool = False) -> bool:
        return True

    def run_terminal(self, cmd: str, cwd: Path, title: str) -> int:
        return runner.run(cmd, cwd, title, interactive=True, on_stall=self.on_stall)

    def on_stall(self, stall) -> bool:
        """El instalador lleva >20 s bloqueado por un proceso que él mismo dejó en segundo plano."""
        from .watchdog import short
        names = ", ".join(short(c) for _, c in stall.blockers)
        return self.confirm(
            "El instalador se ha quedado esperando",
            f"«{short(stall.waiting_cmd)}» está bloqueado por {names}: un proceso que el propio instalador "
            "lanzó en segundo plano y que no suelta su salida. Si lo detienes, el instalador continúa "
            "(es un fallo conocido de algunos scripts, p.ej. HyDE con awww-daemon).",
            stall.describe(), ok="Detener y continuar")


@dataclass
class Result:
    ok: bool
    message: str
    warnings: list[str] = field(default_factory=list)
    backup_id: str | None = None
    reboot: bool = False      # instalación/actualización aplicada: conviene reiniciar


# ------------------------------------------------------------------ estado
def state_path(dot_id: str) -> Path:
    return config.installed_dir() / f"{dot_id}.json"


def load_state(dot_id: str) -> dict | None:
    try:
        return json.loads(state_path(dot_id).read_text())
    except FileNotFoundError:
        return None


def all_states() -> dict[str, dict]:
    out = {}
    d = config.installed_dir()
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                out[p.stem] = json.loads(p.read_text())
            except (OSError, ValueError) as e:
                log.error("estado ilegible %s: %s", p, e)
    return out


def save_state(st: dict) -> None:
    p = state_path(st["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    tmp.replace(p)


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_add(dot_id: str, path: Path, kind: str | None, target: str | None = None,
               fp: dict | None = None) -> None:
    _append(config.ledger_file(), {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dot": dot_id,
                                   "path": str(path), "kind": kind, "target": target, "fp": fp})


def history_add(dot_id: str, action: str, ok: bool, message: str, **extra) -> None:
    _append(config.history_file(), {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dot": dot_id,
                                    "action": action, "ok": ok, "message": message, **extra})


def read_history(limit: int = 300) -> list[dict]:
    try:
        lines = config.history_file().read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for l in lines[-limit:]:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return list(reversed(out))


def remove_deploy(st: dict) -> None:
    """Borra el checkout de un dot SOLO si está dentro de la carpeta de DotDeck.

    Una instalación adoptada (p.ej. ~/HyDE) es del usuario: nunca se borra.
    """
    d = Path(st.get("deploy", ""))
    root = config.deploy_dir()
    if st.get("external") or root not in d.parents:
        log.info("no se borra %s (no es un checkout de DotDeck)", d)
        return
    fsutil.remove_any(d)


# ------------------------------------------------------------------ instalaciones existentes
def _git_out(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=20)
    return r.stdout.strip() if r.returncode == 0 else ""


def _same_repo(a: str, b: str) -> bool:
    norm = lambda u: gitrepo.normalize_url(u).lower().removesuffix(".git").rstrip("/")
    return norm(a) == norm(b)


def sync_external(dots: dict) -> list[str]:
    """Adopta dots instalados fuera de DotDeck (install.existing) y olvida los que ya no están.

    Devuelve los ids adoptados en esta llamada.
    """
    adopted = []
    try:
        ignored = set((config.state_dir() / "ignored_external").read_text().split())
    except FileNotFoundError:
        ignored = set()
    for dot in dots.values():
        if not dot.existing or dot.id in ignored:
            continue
        path = config.expand(dot.existing)
        st = load_state(dot.id)
        ok = (path / ".git").exists() and _same_repo(_git_out(path, "remote", "get-url", "origin"), dot.repo)
        if st and st.get("external"):
            if not ok:
                log.info("%s ya no está en %s: deja de seguirse", dot.id, path)
                state_path(dot.id).unlink(missing_ok=True)
                continue
            head = _git_out(path, "rev-parse", "HEAD")
            if head and head != st["version"].get("commit"):  # actualizado por fuera
                st["version"] = _external_version(path)
                save_state(st)
            continue
        if st or not ok:
            continue
        st = {"id": dot.id, "name": dot.name, "method": "script", "external": True,
              "version": _external_version(path),
              "installed_at": "fuera de DotDeck (adoptado " + time.strftime("%Y-%m-%d %H:%M") + ")",
              "updated_at": None, "deploy": str(path), "backup_id": None, "backups": [],
              "backed_up": [str(config.expand(t)) for t in dot.touches], "links": [], "created": [],
              "modified": [], "optional": []}
        save_state(st)
        history_add(dot.id, "adopt", True, f"instalación existente adoptada: {config.contract(path)} "
                                           f"({st['version']['label']})")
        adopted.append(dot.id)
    return adopted


def _external_version(path: Path) -> dict:
    commit = _git_out(path, "rev-parse", "HEAD")
    branch = _git_out(path, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    desc = _git_out(path, "describe", "--tags", "--always")
    return {"ref": branch, "commit": commit, "branch": branch,
            "label": f"{desc} ({commit[:7]})" if desc and not desc.startswith(commit[:7]) else f"{branch}@{commit[:7]}"}


def owned_paths(st: dict) -> list[Path]:
    """Rutas que un dot instalado considera suyas (para conflictos y huérfanos)."""
    paths = [Path(l["dest"]) for l in st.get("links", [])]
    paths += [Path(c["path"]) for c in st.get("created", [])]
    return paths


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def conflicting_dots(dot: Dot, paths: list[Path]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for other_id, st in all_states().items():
        if other_id == dot.id or st.get("external"):
            continue  # una instalación adoptada (HyDE…) no se puede «reemplazar» automáticamente
        theirs = owned_paths(st) + [Path(p) for p in st.get("backed_up", [])]
        hits = sorted({config.contract(p) for p in paths for q in theirs if _overlaps(p, q)})
        if hits:
            out[other_id] = hits
    return out


# ------------------------------------------------------------------ snapshots (modo script)
SHALLOW_ROOTS = ["~", "~/.config", "~/.local/share", "~/.local/bin", "~/.local/src",
                 "~/.local/state", "~/.cache", "~/Pictures", "~/.themes", "~/.icons"]
RC_FILES = ["~/.bashrc", "~/.zshrc", "~/.profile", "~/.bash_profile", "~/.zprofile",
            "~/.config/fish/config.fish"]


def _own_dirs() -> list[Path]:
    return [config.state_dir(), config.data_dir(), config.cache_dir(), config.config_dir()]


def snapshot(deep: list[Path]) -> dict[str, dict]:
    snap: dict[str, dict] = {}
    own = _own_dirs()
    for r in [config.expand(s) for s in SHALLOW_ROOTS]:
        if not r.is_dir() or r.is_symlink():
            continue
        try:
            names = os.listdir(r)
        except OSError:
            continue
        for n in names:
            p = r / n
            if any(_overlaps(p, o) for o in own):
                continue
            fp = fsutil.fingerprint(p)
            if fp:
                snap[str(p)] = fp
    for d in deep:
        if fsutil.kind(d) == "dir":
            for root, dirs, files in os.walk(d):
                for n in dirs + files:
                    p = os.path.join(root, n)
                    fp = fsutil.fingerprint(Path(p))
                    if fp:
                        snap[p] = fp
        fp = fsutil.fingerprint(d)
        if fp:
            snap[str(d)] = fp
    return snap


def diff_snapshots(before: dict, after: dict) -> tuple[list[str], list[str], list[str]]:
    """→ (creados [solo el ancestro más alto], modificados [no dirs], borrados)."""
    new = sorted(p for p in after if p not in before)
    top: list[str] = []
    for p in new:
        if not any(p.startswith(t + "/") for t in top):
            top.append(p)
    modified = sorted(p for p in after if p in before and after[p]["kind"] != "dir"
                      and after[p] != before[p])
    deleted = sorted(p for p in before if p not in after)
    return top, modified, deleted


# ------------------------------------------------------------------ recarga
def _hyprctl_reload(ui: UI) -> str:
    """hyprctl reload + comprobación de errores de configuración → mensaje legible."""
    ui.progress("Recargando Hyprland…")
    subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=20)
    time.sleep(1.0)
    r = subprocess.run(["hyprctl", "-j", "configerrors"], capture_output=True, text=True, timeout=20)
    try:
        errs = [e for e in json.loads(r.stdout or "[]") if e.strip()]
    except ValueError:
        errs = [r.stdout.strip()] if r.stdout.strip() else []
    if errs:
        log.warning("hyprctl configerrors: %s", errs)
        return ("Hyprland ha recargado con errores de configuración:\n" + "\n".join(errs[:8])
                + "\nPuedes revertir desde Historial.")
    return "Hyprland recargado correctamente."


def reload_hyprland(ui: UI) -> Result:
    """Botón «Recargar Hyprland» de la interfaz."""
    if not (config.is_real_home() and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")):
        raise EngineError("No hay una sesión de Hyprland activa para este usuario")
    if not shutil.which("hyprctl"):
        raise EngineError("No se encuentra hyprctl")
    msg = _hyprctl_reload(ui)
    ok = msg.startswith("Hyprland recargado")
    history_add("hyprland", "reload", ok, msg.splitlines()[0])
    return Result(ok, msg.splitlines()[0], msg.splitlines()[1:])


def reload_after(dot: Dot, ui: UI) -> list[str]:
    """Recarga Hyprland/componentes si es seguro; devuelve avisos para el usuario."""
    notes: list[str] = []
    s = config.load_settings()
    live = config.is_real_home() and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if dot.touches_hypr():
        if live and s.get("auto_reload_hyprland", True) and shutil.which("hyprctl"):
            notes.append(_hyprctl_reload(ui))
        else:
            notes.append("Recarga Hyprland a mano (hyprctl reload) o cierra sesión para aplicar el dot.")
    if dot.reload:
        if live:
            for c in dot.reload:
                ui.progress(f"Recargando: {c}")
                subprocess.Popen(["bash", "-c", c], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            notes.append("Componentes recargados: " + "; ".join(dot.reload))
        else:
            notes.append("Ejecuta para recargar: " + "; ".join(dot.reload))
    if dot.reload_note:
        notes.append(dot.reload_note)
    return notes


# ------------------------------------------------------------------ pasos comunes
def _check_deps(dot: Dot, ui: UI) -> list[str]:
    ui.progress("Comprobando dependencias…")
    plan = deps.plan(dot)
    warnings: list[str] = []
    if plan.empty:
        return warnings
    if ui.skip_deps:
        return [missing_deps_message(plan, "Dependencias sin instalar (omitidas)")]
    body = "Este dot necesita paquetes que no tienes instalados."
    details = [f"repos oficiales: {', '.join(plan.missing_repo)}" if plan.missing_repo else "",
               f"AUR: {', '.join(plan.missing_aur)}" if plan.missing_aur else "",
               f"sin helper AUR (instala paru/yay): {', '.join(plan.unknown)}" if plan.unknown else "",
               *[f"$ {c}" for c in plan.commands()]]
    details = [d for d in details if d]
    if plan.commands() and ui.confirm("Instalar dependencias", body + "\nSe abrirá una terminal; "
                                      "sudo te pedirá la contraseña allí.", details, ok="Instalar paquetes"):
        for c in plan.commands():
            rc = ui.run_terminal(c, config.home(), f"dependencias de {dot.name}")
            if rc != 0:
                log.warning("instalación de dependencias terminó con %s", rc)
    still = deps.plan(dot)
    if not still.empty:
        left = still.missing_repo + still.missing_aur + still.unknown
        if not ui.confirm("Faltan dependencias",
                          "No se han podido instalar todas las dependencias. El dot puede no "
                          "funcionar bien. ¿Continuar igualmente?", left, ok="Continuar sin ellas", danger=True):
            raise Cancelled("Instalación cancelada: faltan dependencias (" + ", ".join(left) + ")")
        warnings.append(missing_deps_message(still, "Dependencias sin instalar"))
    return warnings


def missing_deps_message(plan: deps.DepPlan, title: str) -> str:
    """Aviso con los paquetes que faltan y el comando exacto para instalarlos después."""
    left = plan.missing_repo + plan.missing_aur + plan.unknown
    msg = f"{title}: {', '.join(left)}\nPara instalarlas más tarde:"
    msg += "".join(f"\n  $ {c}" for c in plan.commands())
    if plan.unknown:
        msg += f"\n  (sin helper AUR: instala paru o yay para {', '.join(plan.unknown)})"
    return msg + "\n(o desde la ficha del dot → Dependencias → Instalar las que faltan)"


def _prepare_deploy(dot: Dot, ref: str, links: list[Link], ui: UI) -> tuple[Path, str]:
    dest = gitrepo.deploy_path(dot)
    if dest.exists():
        if load_state(dot.id):
            raise EngineError(f"{dot.name} ya está instalado")
        log.info("eliminando checkout huérfano propio %s", dest)
        fsutil.remove_any(dest)
    tmp = dest.with_name(dest.name + ".partial")
    fsutil.remove_any(tmp)
    ui.progress(f"Descargando {dot.name} ({ref})…")
    try:
        commit = gitrepo.checkout(dot, ref, tmp, links)
    except gitrepo.GitError as e:
        fsutil.remove_any(tmp)
        raise EngineError(f"No se pudo descargar el repositorio: {e}") from e
    missing = [l.src for l in links if fsutil.kind(tmp / l.src) is None]
    if missing:
        fsutil.remove_any(tmp)
        raise EngineError("El repositorio no contiene rutas de la definición: " + ", ".join(missing)
                          + ". Edita el dot y corrige install.links.")
    tmp.rename(dest)
    return dest, commit


def _select_links(dot: Dot, optional: list[str] | None) -> list[Link]:
    chosen = set(optional or [])
    return [l for l in dot.links if not l.optional or l.src in chosen or l.dest in chosen]


def _confirm_overwrite(dot: Dot, paths: list[Path], ui: UI, verb: str) -> None:
    existing = [config.contract(p) for p in paths if fsutil.kind(p) is not None]
    if existing and not ui.confirm(
            f"{verb} {dot.name}",
            "Se sustituirá esta configuración. Antes se guarda un backup completo y verificado "
            "que podrás restaurar desde Historial.", existing, ok=verb):
        raise Cancelled(f"{verb} cancelado")


# ------------------------------------------------------------------ instalar
def install(dot: Dot, ui: UI, ref: str | None = None, optional: list[str] | None = None,
            replace: bool = False) -> Result:
    if load_state(dot.id):
        raise EngineError(f"{dot.name} ya está instalado; usa Actualizar")
    if dot.existing and (config.expand(dot.existing) / ".git").exists():
        # Ya instalado por fuera (p.ej. ~/HyDE): se vuelve a adoptar en vez de instalar encima.
        ign = config.state_dir() / "ignored_external"
        if ign.exists():
            ign.write_text("".join(l + "\n" for l in ign.read_text().split() if l != dot.id))
        if sync_external({dot.id: dot}):
            st = load_state(dot.id)
            return Result(True, f"{dot.name} ya estaba instalado en {dot.existing}: DotDeck lo sigue desde ahora "
                                f"({st['version']['label']})")
        raise EngineError(f"{dot.existing} existe pero no es un clon de {dot.repo}")
    links = _select_links(dot, optional) if dot.method == "files" else []
    to_backup = dot.backup_paths(links)
    if dot.method == "script":
        to_backup += [config.expand(r) for r in RC_FILES if config.expand(r) not in to_backup]
    clashes = conflicting_dots(dot, dot.backup_paths(links))
    if clashes:
        listing = [f"{k}: {', '.join(v)}" for k, v in clashes.items()]
        if not replace:
            raise EngineError("Choca con dots instalados (desinstálalos antes o usa «Reemplazar»): "
                              + " | ".join(listing))
        if not ui.confirm("Reemplazar dots instalados",
                          "Se desinstalarán y restaurarán estos dots antes de continuar:", listing,
                          ok="Reemplazar", danger=True):
            raise Cancelled("Cancelado")
        for other in clashes:
            r = uninstall_by_id(other, ui, restore=True)
            if not r.ok:
                raise EngineError(f"No se pudo desinstalar {other}: {r.message}")

    ui.progress("Consultando la última versión…")
    try:
        ver = gitrepo.remote_version(dot)
    except gitrepo.GitError as e:
        raise EngineError(f"Repositorio no accesible: {e}") from e
    if ref:
        ver = {"ref": ref, "commit": "", "label": ref, "branch": ver.get("branch", "")}

    warnings = _check_deps(dot, ui)
    deploy, commit = _prepare_deploy(dot, ver["ref"], links, ui)
    ver["commit"] = commit
    if ver["label"] == ver["ref"] and ver["ref"] == ver.get("branch"):
        ver["label"] = f"{ver['ref']}@{commit[:7]}"

    try:
        _confirm_overwrite(dot, to_backup, ui, "Instalar")
        ui.progress("Creando backup de la configuración actual…")
        bk = backup.create(dot.id, "install", to_backup, note=f"antes de instalar {dot.name} {ver['label']}")
    except (Cancelled, backup.BackupError):
        fsutil.remove_any(deploy)
        raise

    st = {"id": dot.id, "name": dot.name, "method": dot.method, "version": ver,
          "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "updated_at": None,
          "deploy": str(deploy), "backup_id": bk.id, "backups": [bk.id],
          "backed_up": [str(p) for p in to_backup], "links": [], "created": [], "modified": [],
          "optional": optional or []}
    try:
        if dot.method == "files":
            _apply_links(dot, links, deploy, st, ui)
        else:
            _run_script(dot, dot.command, deploy, to_backup, st, ui, "instalar")
    except Exception as e:
        log.error("instalación de %s fallida: %s — revirtiendo", dot.id, e)
        ui.progress("Error: revirtiendo cambios…")
        rb = _rollback(dot, st, bk.id)
        history_add(dot.id, "install", False, str(e), backup=bk.id)
        raise EngineError(f"La instalación falló y se ha revertido ({rb}): {e}") from e

    save_state(st)
    warnings += reload_after(dot, ui)
    history_add(dot.id, "install", True, f"instalado {ver['label']}", backup=bk.id, version=ver["label"])
    return Result(True, f"{dot.name} {ver['label']} instalado", warnings, bk.id, reboot=True)


def _apply_links(dot: Dot, links: list[Link], deploy: Path, st: dict, ui: UI) -> None:
    ui.progress("Enlazando archivos del dot…")
    for l in links:
        dest = config.expand(l.dest)
        target = deploy / l.src
        if fsutil.kind(dest) is not None:
            fsutil.remove_any(dest)  # ya respaldado y verificado
        fsutil.symlink(target, dest)
        rec = {"src": l.src, "dest": str(dest), "target": str(target)}
        st["links"].append(rec)
        ledger_add(dot.id, dest, "symlink", target=str(target))


def _run_script(dot: Dot, cmd: str, deploy: Path, backed: list[Path], st: dict, ui: UI, verb: str) -> None:
    ui.progress("Tomando instantánea del sistema antes del instalador…")
    before = snapshot(backed)
    ui.progress(f"Ejecutando el instalador del autor en una terminal ({cmd})…")
    rc = ui.run_terminal(cmd, deploy, f"{verb} {dot.name}")
    after = snapshot(backed)
    created, modified, deleted = diff_snapshots(before, after)
    known = {c["path"] for c in st.get("created", [])}
    for p in created:
        if p in known:
            continue
        fp = fsutil.fingerprint(Path(p))
        st["created"].append({"path": p, "kind": fp["kind"] if fp else None, "fp": fp})
        ledger_add(dot.id, Path(p), fp["kind"] if fp else None, fp=fp)
    unprotected = [p for p in modified + deleted
                   if not any(_overlaps(Path(p), b) for b in backed)]
    st["modified"] = sorted(set(st.get("modified", [])) | set(modified))
    st["unprotected"] = sorted(set(st.get("unprotected", [])) | set(unprotected))
    log.info("instalador %s: rc=%s creados=%d modificados=%d borrados=%d sin-backup=%s",
             dot.id, rc, len(created), len(modified), len(deleted), unprotected)
    if rc != 0:
        raise EngineError(f"el instalador del autor terminó con código {rc}")


def _rollback(dot: Dot, st: dict, backup_id: str) -> str:
    """Deja el sistema como antes: quita lo creado y restaura el backup."""
    steps = []
    for l in st.get("links", []):
        p = Path(l["dest"])
        if fsutil.kind(p) == "symlink" and os.readlink(p) == l["target"]:
            fsutil.remove_any(p)
    for c in st.get("created", []):
        p = Path(c["path"])
        if not any(_overlaps(p, Path(b)) for b in st.get("backed_up", [])):
            fsutil.remove_any(p)
            steps.append(f"borrado {config.contract(p)}")
    try:
        steps += backup.restore(backup_id, safety=False)
    except backup.BackupError as e:
        steps.append(f"ERROR restaurando backup {backup_id}: {e}")
        log.error("rollback incompleto: %s", e)
    remove_deploy(st)
    return f"{len(steps)} acciones"


# ------------------------------------------------------------------ actualizar
def check_update(dot: Dot) -> dict | None:
    """→ versión remota si es distinta de la instalada; None si está al día."""
    st = load_state(dot.id)
    if not st:
        return None
    rv = gitrepo.remote_version(dot)
    iv = st["version"]
    if rv["ref"] == iv["ref"] and (not rv["commit"] or rv["commit"] == iv.get("commit")):
        return None
    return rv


def update(dot: Dot, ui: UI, ref: str | None = None, keep_local: bool | None = None) -> Result:
    st = load_state(dot.id)
    if not st:
        raise EngineError(f"{dot.name} no está instalado")
    ui.progress("Consultando la última versión…")
    try:
        rv = gitrepo.remote_version(dot)
    except gitrepo.GitError as e:
        raise EngineError(f"Repositorio no accesible: {e}") from e
    if ref:
        rv = {"ref": ref, "commit": "", "label": ref, "branch": rv.get("branch", "")}
    elif rv["ref"] == st["version"]["ref"] and rv["commit"] == st["version"].get("commit"):
        return Result(True, f"{dot.name} ya está al día ({st['version']['label']})")
    deploy = Path(st["deploy"])
    warnings = _check_deps(dot, ui)

    if dot.method == "files":
        changes = gitrepo.local_changes(deploy) if (deploy / ".git").exists() else []
        if changes and keep_local is None:
            keep_local = ui.confirm(
                "Has modificado archivos de este dot",
                "Se han detectado cambios manuales. ¿Intentar conservarlos sobre la nueva versión? "
                "(Si eliges no, se sobrescriben; en ambos casos hay backup.)",
                changes[:30], ok="Conservar mis cambios")
        selected = _select_links(dot, st.get("optional"))
        new_links = [l for l in selected if str(config.expand(l.dest)) not in {x["dest"] for x in st["links"]}]
        to_backup = [deploy] + [config.expand(l.dest) for l in new_links]
        _confirm_overwrite(dot, [config.expand(l.dest) for l in new_links], ui, "Actualizar")
        ui.progress("Backup previo a la actualización…")
        bk = backup.create(dot.id, "update", to_backup, note=f"antes de actualizar a {rv['label']}")
        try:
            # Ajusta el sparse-checkout por si la definición cambió de rutas.
            pats = gitrepo.sparse_patterns(dot, selected)
            if pats:
                gitrepo.git("sparse-checkout", "set", "--no-cone", *pats, cwd=deploy)
            ui.progress(f"Descargando {rv['label']}…")
            commit, conflicts = gitrepo.update_checkout(deploy, rv["ref"], bool(keep_local))
            if conflicts:
                if ui.confirm("Conflictos al conservar tus cambios",
                              "Tus cambios chocan con la nueva versión en estos archivos. "
                              "¿Revertir la actualización?", conflicts, ok="Revertir", danger=True):
                    backup.restore(bk.id, safety=False)
                    history_add(dot.id, "update", False, "revertida por conflictos", backup=bk.id)
                    return Result(False, "Actualización revertida por conflictos", conflicts, bk.id)
                warnings.append("Conflictos sin resolver (marcas <<<<<<< en): " + ", ".join(conflicts))
            missing = [l.src for l in selected if fsutil.kind(deploy / l.src) is None]
            if missing:
                raise EngineError("la nueva versión ya no contiene: " + ", ".join(missing))
            _apply_links(dot, new_links, deploy, st, ui)
            wanted = {str(config.expand(l.dest)) for l in selected}
            dropped = [x for x in st["links"] if x["dest"] not in wanted]
            if dropped:
                # Ya no forman parte del dot: quedan registrados en el ledger → huérfanos.
                st["links"] = [x for x in st["links"] if x["dest"] in wanted]
                warnings.append("Rutas que la nueva definición ya no usa (ver Huérfanos): "
                                + ", ".join(config.contract(x["dest"]) for x in dropped))
        except Exception as e:
            log.error("actualización de %s fallida: %s — restaurando", dot.id, e)
            backup.restore(bk.id, safety=False)
            history_add(dot.id, "update", False, str(e), backup=bk.id)
            raise EngineError(f"La actualización falló y se ha revertido: {e}") from e
    else:
        to_backup = [Path(p) for p in st["backed_up"]] + [deploy]
        if st.get("external"):
            # Instalación adoptada: se sigue la guía del autor (fetch + reset --hard), que
            # descarta cambios locales del clon → avisar y respaldar el clon entero antes.
            changes = gitrepo.local_changes(deploy)
            if not ui.confirm(f"Actualizar {dot.name}",
                              f"Se seguirán los pasos oficiales del autor en {config.contract(deploy)}: "
                              f"descargar {rv['label']}, «git reset --hard» y «{dot.update_command or dot.command}» "
                              "en una terminal. Antes se hace un backup verificado de tu configuración y del "
                              "propio clon (revertible desde Historial)."
                              + ("\n\n⚠ Tienes cambios sin guardar en el clon que el autor descarta al "
                                 "actualizar (quedan en el backup):" if changes else ""),
                              changes[:25] or [config.contract(p) for p in to_backup[:25]],
                              ok="Actualizar", danger=bool(changes)):
                raise Cancelled("Actualización cancelada")
        ui.progress("Backup previo a la actualización…")
        bk = backup.create(dot.id, "update", to_backup, note=f"antes de actualizar a {rv['label']}")
        try:
            ui.progress(f"Descargando {rv['label']}…")
            if st.get("external"):
                commit = gitrepo.update_external(deploy, rv["ref"])
            else:
                commit, _ = gitrepo.update_checkout(deploy, rv["ref"], keep_local=False)
            _run_script(dot, dot.update_command or dot.command, deploy,
                        [Path(p) for p in st["backed_up"]], st, ui, "actualizar")
        except Exception as e:
            log.error("actualización de %s fallida: %s — restaurando", dot.id, e)
            backup.restore(bk.id, safety=False)
            history_add(dot.id, "update", False, str(e), backup=bk.id)
            raise EngineError(f"La actualización falló y se ha revertido: {e}") from e

    rv["commit"] = commit
    if st.get("external"):
        rv = _external_version(deploy)
    elif rv["label"] == rv["ref"] and rv["ref"] == rv.get("branch"):
        rv["label"] = f"{rv['ref']}@{commit[:7]}"
    old = st["version"]["label"]
    st["version"] = rv
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["backups"].append(bk.id)
    save_state(st)
    _clear_update_flag(dot.id)
    warnings += reload_after(dot, ui)
    history_add(dot.id, "update", True, f"{old} → {rv['label']}", backup=bk.id, version=rv["label"])
    return Result(True, f"{dot.name} actualizado: {old} → {rv['label']}", warnings, bk.id, reboot=True)


def _clear_update_flag(dot_id: str) -> None:
    p = config.updates_file()
    try:
        d = json.loads(p.read_text())
        d.get("available", {}).pop(dot_id, None)
        p.write_text(json.dumps(d, indent=1))
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ desinstalar
def uninstall_by_id(dot_id: str, ui: UI, restore: bool = True, run_author: bool = False,
                    dot: Dot | None = None) -> Result:
    st = load_state(dot_id)
    if not st:
        raise EngineError(f"{dot_id} no está instalado")
    name = st.get("name", dot_id)
    warnings: list[str] = []
    deploy = Path(st["deploy"])
    if st.get("external"):
        return _forget_external(st, ui, run_author, dot)

    if run_author and dot and dot.uninstall_command and deploy.exists():
        rc = ui.run_terminal(dot.uninstall_command, deploy, f"desinstalar {name}")
        if rc != 0:
            warnings.append(f"El desinstalador del autor terminó con código {rc}")

    created_outside = [c for c in st.get("created", [])
                       if not any(_overlaps(Path(c["path"]), Path(b)) for b in st["backed_up"])]
    if created_outside and not ui.confirm(
            f"Desinstalar {name}",
            "El instalador creó estas rutas fuera de las zonas respaldadas. Se borrarán "
            "(están registradas en el manifiesto del dot):",
            [config.contract(c["path"]) for c in created_outside], ok="Desinstalar", danger=True):
        raise Cancelled("Desinstalación cancelada")

    restorable: list[Path] = []
    for l in st.get("links", []):
        p = Path(l["dest"])
        k = fsutil.kind(p)
        if k == "symlink" and os.readlink(p) == l["target"]:
            fsutil.remove_any(p)
            restorable.append(p)
        elif k is None:
            restorable.append(p)
        else:
            warnings.append(f"{config.contract(p)} ya no es el enlace de DotDeck (lo cambiaste tú): no se toca")
    if st["method"] == "script":
        restorable += [Path(b) for b in st["backed_up"]]
        # Sin restaurar ("dejar limpio") también se quita lo creado dentro de zonas respaldadas.
        for c in (created_outside if restore else st.get("created", [])):
            p = Path(c["path"])
            if fsutil.kind(p) is not None:
                fsutil.remove_any(p)
                ledger_add(dot_id, p, None)  # registro de borrado (kind None)

    if restore:
        ui.progress("Restaurando tu configuración anterior…")
        try:
            backup.restore(st["backup_id"], only=restorable)
        except backup.BackupError as e:
            warnings.append(f"No se pudo restaurar el backup: {e}")
    if deploy.exists():
        remove_deploy(st)
    state_path(dot_id).unlink()
    _clear_update_flag(dot_id)
    if dot:
        warnings += reload_after(dot, ui)
    msg = f"{name} desinstalado" + (" y configuración anterior restaurada" if restore else " (sin restaurar)")
    history_add(dot_id, "uninstall", True, msg, backup=st["backup_id"])
    return Result(True, msg, warnings, st["backup_id"])


def _forget_external(st: dict, ui: UI, run_author: bool, dot: Dot | None) -> Result:
    """Una instalación adoptada no tiene backup «de antes»: DotDeck deja de seguirla y,
    si se pide, ejecuta el desinstalador del autor. El clon del usuario nunca se borra."""
    name, deploy = st.get("name", st["id"]), Path(st["deploy"])
    warnings = [f"{config.contract(deploy)} no se ha tocado. Para volver a seguirlo pulsa «Instalar»."]
    if run_author and dot and dot.uninstall_command and deploy.exists():
        to_backup = [Path(p) for p in st["backed_up"]]
        bk = backup.create(st["id"], "uninstall", to_backup, note=f"antes del desinstalador de {name}")
        rc = ui.run_terminal(dot.uninstall_command, deploy, f"desinstalar {name}")
        warnings.append(f"Desinstalador del autor: código {rc}. Backup previo: {bk.id}")
    # Marca para que sync_external no lo vuelva a adoptar enseguida.
    ignore = config.state_dir() / "ignored_external"
    ignore.parent.mkdir(parents=True, exist_ok=True)
    with open(ignore, "a") as f:
        f.write(st["id"] + "\n")
    state_path(st["id"]).unlink()
    history_add(st["id"], "forget", True, f"{name}: DotDeck deja de seguirlo")
    return Result(True, f"DotDeck ya no sigue {name}", warnings)


def uninstall(dot: Dot, ui: UI, restore: bool = True, run_author: bool = False) -> Result:
    return uninstall_by_id(dot.id, ui, restore=restore, run_author=run_author, dot=dot)


def revert_backup(bid: str, ui: UI) -> Result:
    """Restaura un backup concreto desde Historial (crea antes un backup de seguridad)."""
    b = backup.load(bid)
    if not ui.confirm("Revertir backup",
                      f"Se restaurarán exactamente estas rutas al estado del {b.created}. "
                      "El estado actual se guarda antes en un backup de seguridad.",
                      [e["display"] for e in b.entries], ok="Revertir", danger=True):
        raise Cancelled("Cancelado")
    done = backup.restore(bid)
    # Si el backup era el de instalación de un dot aún instalado, ese dot deja de estarlo.
    for dot_id, st in all_states().items():
        if st.get("backup_id") == bid and not st.get("external"):
            remove_deploy(st)
            state_path(dot_id).unlink()
            done.append(f"{st.get('name', dot_id)} marcado como desinstalado")
    history_add(b.dot, "revert", True, f"revertido backup {bid}", backup=bid)
    return Result(True, f"Backup {bid} restaurado", done, bid)
