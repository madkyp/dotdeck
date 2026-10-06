"""Punto de entrada: sin subcomando abre la interfaz; con subcomando, CLI."""
from __future__ import annotations

import argparse
import sys

from . import backup, catalog, config, engine, onboard, orphans, runner, updates
from .config import log


class CliUI(engine.UI):
    def __init__(self, yes: bool = False, no_terminal: bool = False):
        self.yes, self.no_terminal = yes, no_terminal

    def progress(self, msg: str) -> None:
        print(f"  · {msg}")
        log.info("· %s", msg)

    def confirm(self, title, body, details=None, ok="Continuar", danger=False) -> bool:
        print(f"\n\033[1m{title}\033[0m\n{body}")
        for d in details or []:
            print(f"    - {d}")
        if self.yes:
            print(f"  → {ok} (--yes)")
            return True
        try:
            return input(f"  ¿{ok}? [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")
        except EOFError:
            return False

    def run_terminal(self, cmd, cwd, title) -> int:
        return runner.run(cmd, cwd, title, interactive=not self.no_terminal, on_stall=self.on_stall)


def _dot(dots: dict, dot_id: str) -> catalog.Dot:
    if dot_id not in dots:
        sys.exit(f"No existe el dot '{dot_id}'. Usa: dotdeck list")
    return dots[dot_id]


def _report(r: engine.Result) -> int:
    print(("✔ " if r.ok else "✘ ") + r.message)
    for w in r.warnings:
        print(f"  ! {w}")
    if r.backup_id:
        print(f"  backup: {r.backup_id}")
    if r.reboot:
        print("  ↻ Se recomienda reiniciar el equipo para aplicar todos los cambios (systemctl reboot).")
    return 0 if r.ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dotdeck", description="Gestor de dots de Hyprland")
    ap.add_argument("--debug", action="store_true", help="log detallado de cada operación de archivos")
    ap.add_argument("--page", help="página inicial de la interfaz (library|installed|history|settings)")
    ap.add_argument("--check-updates", action="store_true", help="alias de 'check-updates'")
    ap.add_argument("--notify", action="store_true", help="con --check-updates: notificación del sistema")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="catálogo y estado")
    s = sub.add_parser("show", help="definición de un dot"); s.add_argument("id")
    s = sub.add_parser("install"); s.add_argument("id"); s.add_argument("--ref")
    s.add_argument("--optional", nargs="*", default=[]); s.add_argument("--replace", action="store_true")
    s = sub.add_parser("update"); s.add_argument("id"); s.add_argument("--ref")
    g = s.add_mutually_exclusive_group(); g.add_argument("--keep-local", action="store_true", default=None)
    g.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("uninstall"); s.add_argument("id")
    s.add_argument("--no-restore", action="store_true"); s.add_argument("--author", action="store_true")
    sub.add_parser("backups", help="lista de backups")
    s = sub.add_parser("restore", help="revertir un backup"); s.add_argument("backup_id")
    s = sub.add_parser("orphans", help="buscar (y limpiar) huérfanos"); s.add_argument("--clean", action="store_true")
    sub.add_parser("check-updates")
    s = sub.add_parser("add", help="añadir dot desde URL"); s.add_argument("url")
    s.add_argument("--id"); s.add_argument("--touches", nargs="*")
    s = sub.add_parser("remove", help="eliminar un dot añadido por ti"); s.add_argument("id")
    s = sub.add_parser("timer", help="temporizador de comprobación"); s.add_argument("state", choices=["on", "off"])
    sub.add_parser("history")
    for p in sub.choices.values():
        p.add_argument("-y", "--yes", action="store_true", help="aceptar confirmaciones")
        p.add_argument("--no-terminal", action="store_true", help="ejecutar instaladores aquí mismo")
        p.add_argument("--skip-deps", action="store_true", help="no instalar dependencias (solo avisar)")
    a = ap.parse_args(argv)
    config.setup_logging(a.debug)
    log.debug("argv=%s home=%s", argv or sys.argv[1:], config.home())

    if a.check_updates or a.cmd == "check-updates":
        res = updates.check_all()
        for v in res["available"].values():
            print(f"↑ {v['name']}: {v['installed']} → {v['latest']}")
        for k, v in res["errors"].items():
            print(f"! {k}: {v}")
        if not res["available"]:
            print("Todo al día.")
        if a.notify and config.load_settings().get("notify", True):
            updates.notify(res)
        return 0
    if not a.cmd:
        from .ui.app import run_app
        return run_app(page=a.page, debug=a.debug)

    ui = CliUI(yes=a.yes, no_terminal=a.no_terminal)
    ui.skip_deps = a.skip_deps
    dots, errors = catalog.load_all()
    for e in errors:
        print(f"! definición inválida: {e}", file=sys.stderr)
    engine.sync_external(dots)
    states = engine.all_states()
    try:
        if a.cmd == "list":
            av = updates.load().get("available", {})
            for d in dots.values():
                st = states.get(d.id)
                state = "no instalado"
                if st:
                    state = f"instalado {st['version']['label']}" + (" ↑ actualización" if d.id in av else "")
                tag = " [ejemplo temporal]" if d.example else (" [añadido por ti]" if d.origin == "user" else "")
                print(f"{d.id:22} {d.method:6} {state:36} {d.repo}{tag}")
        elif a.cmd == "show":
            from . import tomlw
            print(tomlw.dumps(_dot(dots, a.id).to_dict()))
        elif a.cmd == "install":
            return _report(engine.install(_dot(dots, a.id), ui, ref=a.ref, optional=a.optional, replace=a.replace))
        elif a.cmd == "update":
            keep = True if a.keep_local else (False if a.overwrite else None)
            return _report(engine.update(_dot(dots, a.id), ui, ref=a.ref, keep_local=keep))
        elif a.cmd == "uninstall":
            return _report(engine.uninstall_by_id(a.id, ui, restore=not a.no_restore,
                                                  run_author=a.author, dot=dots.get(a.id)))
        elif a.cmd == "backups":
            for b in backup.list_all():
                print(f"{b.id:48} {b.created}  {'OK ' if b.complete else b.manifest.get('status')}  "
                      + ", ".join(e["display"] for e in b.entries))
        elif a.cmd == "restore":
            return _report(engine.revert_backup(a.backup_id, ui))
        elif a.cmd == "orphans":
            found, notes = orphans.find()
            for o in found:
                print(f"{'✔' if o.safe else '?'} {o.display:50} [{o.dot}] {o.reason}")
            for n in notes:
                print(f"  i {n}")
            if not found:
                print("No hay huérfanos registrados.")
            if a.clean and found:
                safe = [o.path for o in found if o.safe]
                if ui.confirm("Limpiar huérfanos", "Se borrarán (con backup previo):",
                              [config.contract(p) for p in safe], ok="Borrar", danger=True):
                    for line in orphans.clean(safe):
                        print("  " + line)
        elif a.cmd == "add":
            pr = onboard.propose(a.url, progress=ui.progress)
            if a.id:
                pr.dot.id = a.id
            if a.touches is not None:
                pr.dot.touches = a.touches
                pr.status["touches"] = ("auto", "indicado por el usuario")
            for k, (stt, note) in pr.status.items():
                mark = {"auto": "✔ auto  ", "review": "? revisar", "missing": "✘ falta  "}[stt]
                print(f"  {mark} {k:18} {note}")
            for w in pr.warnings:
                print(f"  ! {w}")
            from . import tomlw
            print("\n" + tomlw.dumps(pr.dot.to_dict()))
            missing = [k for k, (s_, _) in pr.status.items() if s_ == "missing"]
            if missing:
                print(f"Faltan campos obligatorios ({', '.join(missing)}): complétalos en la interfaz.")
                return 2
            if ui.confirm("Guardar dot", "¿Añadir esta definición al catálogo?", ok="Guardar"):
                print("Guardado en", catalog.save_user_dot(pr.dot))
        elif a.cmd == "remove":
            d = _dot(dots, a.id)
            if a.id in states:
                sys.exit("Desinstálalo antes de eliminarlo del catálogo.")
            catalog.delete_user_dot(d)
            print("Eliminado", a.id)
        elif a.cmd == "timer":
            s = config.load_settings()
            if a.state == "on":
                updates.install_timer(s["check_interval_hours"], s["check_on_login"])
            else:
                updates.remove_timer()
            print("Temporizador", a.state)
        elif a.cmd == "history":
            for h in engine.read_history():
                print(f"{h['ts']}  {'✔' if h['ok'] else '✘'} {h['dot']:16} {h['action']:10} {h['message']}")
    except engine.Cancelled as e:
        print(f"Cancelado: {e}")
        return 3
    except (engine.EngineError, backup.BackupError, onboard.OnboardError, catalog.DotError) as e:
        log.error("%s", e)
        print(f"✘ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
