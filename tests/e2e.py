#!/usr/bin/env python3
"""Prueba de extremo a extremo de DotDeck en un $HOME de sandbox.

Nunca toca la configuración real: HOME y XDG_* apuntan a un directorio temporal
y las dependencias se omiten (--skip-deps), así que no se instala ningún paquete.

Uso:  python3 tests/e2e.py [--keep] [--only NOMBRE]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name + (f"  — {detail}" if detail and not cond else ""))


class Sandbox:
    def __init__(self, base: Path):
        self.h = base / "home"
        self.h.mkdir(parents=True)
        self.env = {**os.environ, "HOME": str(self.h),
                    "XDG_CONFIG_HOME": str(self.h / ".config"), "XDG_DATA_HOME": str(self.h / ".local/share"),
                    "XDG_STATE_HOME": str(self.h / ".local/state"), "XDG_CACHE_HOME": str(self.h / ".cache"),
                    "PYTHONPATH": str(ROOT)}
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)  # nunca recargar el Hyprland real

    def populate(self) -> None:
        """Configuración "del usuario" previa a cualquier dot."""
        f = {
            ".config/hypr/hyprland.lua": "-- config real del usuario\nlocal terminal = 'kitty'\n",
            ".config/hypr/monitors.lua": "hl.monitor({output='', mode='preferred'})\n",
            ".config/kitty/kitty.conf": "font_size 11\n",
            ".config/rofi/theme.rasi": "* { bg: #000; }\n",
            ".config/dunst/dunstrc": "[global]\nfont = Cantarell 10\n",
            ".config/waybar/config.jsonc": "{ \"layer\": \"top\" }\n",
            ".zshrc": "export EDITOR=micro\n",
            ".bashrc": "alias ll='ls -l'\n",
            ".local/bin/mi-script": "#!/bin/sh\necho hola\n",
        }
        for rel, txt in f.items():
            p = self.h / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt)
        os.chmod(self.h / ".local/bin/mi-script", 0o755)
        for d in (".local/state", ".cache"):  # existen en cualquier home real
            (self.h / d).mkdir(parents=True, exist_ok=True)
        (self.h / ".local/share/themes/Cat/gtk-4.0").mkdir(parents=True)
        os.symlink(self.h / ".local/share/themes/Cat/gtk-4.0", self.h / ".config/gtk-4.0")
        os.symlink("/nonexistent/target", self.h / ".config/roto-del-usuario")

    def dd(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, "-m", "dotdeck", "--debug", *args], env=self.env,
                           capture_output=True, text=True, cwd=ROOT)
        out = (r.stdout + r.stderr).strip()
        print("    $ dotdeck " + " ".join(args) + f"  → rc={r.returncode}")
        if ok and r.returncode != 0:
            print("      " + out.replace("\n", "\n      ")[-3000:])
        r.out = out  # type: ignore[attr-defined]
        return r

    def digest(self) -> dict:
        """Huella de todo el $HOME salvo las carpetas propias de DotDeck."""
        from dotdeck import fsutil
        own = {".local/state/dotdeck", ".local/share/dotdeck", ".cache/dotdeck", ".config/dotdeck"}
        d = fsutil.digest_tree(self.h)
        return {k: v for k, v in d.items() if not any(k == o or k.startswith(o + "/") for o in own)}

    def state(self, dot: str) -> dict | None:
        p = self.h / f".local/state/dotdeck/installed/{dot}.json"
        return json.loads(p.read_text()) if p.exists() else None


def diff(a: dict, b: dict) -> str:
    ks = sorted(set(a) ^ set(b)) + sorted(k for k in set(a) & set(b) if a[k] != b[k])
    return ", ".join(ks[:10])


# ---------------------------------------------------------------- escenarios
def t_files_athena(s: Sandbox) -> None:
    print("\n[1] athena-eww (lista real, método files): instalar → backup → desinstalar/restaurar")
    before = s.digest()
    r = s.dd("install", "athena-eww", "--yes", "--skip-deps")
    check("instala athena-eww", r.returncode == 0, r.out[-400:])
    st = s.state("athena-eww")
    check("estado registrado con versión", bool(st and st["version"]["commit"]))
    eww = s.h / ".config/eww"
    check("~/.config/eww es symlink al checkout", eww.is_symlink() and (eww / "eww.yuck").exists())
    check("~/.zshrc opcional NO tocado", not (s.h / ".zshrc").is_symlink())
    os.environ.update({k: s.env[k] for k in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")})
    from dotdeck import backup
    b = backup.load(st["backup_id"])
    check("backup completo y verificado", b.complete)
    ex = {e["display"]: e["existed"] for e in b.entries}
    check("backup registra rutas que no existían (eww)", ex.get("~/.config/eww") is False, str(ex))
    check("backup guarda kitty/rofi/dunst existentes",
          all(ex.get(f"~/.config/{x}") for x in ("kitty", "rofi", "dunst")), str(ex))
    backup.verify(b)
    check("huérfanos: ninguno con el dot instalado", "No hay huérfanos" in s.dd("orphans").out)
    r = s.dd("uninstall", "athena-eww", "--yes")
    check("desinstala athena-eww", r.returncode == 0, r.out[-300:])
    after = s.digest()
    check("REVERTIR deja $HOME exactamente igual", before == after, diff(before, after))


def t_update_jakoolit(s: Sandbox) -> None:
    print("\n[2] JaKooLit (ejemplo temporal): instalar v2.3.17 → cambio manual → actualizar conservando → revertir backup")
    before = s.digest()
    r = s.dd("install", "jakoolit-hyprland-dots", "--ref", "v2.3.17", "--yes", "--skip-deps")
    check("instala v2.3.17", r.returncode == 0, r.out[-400:])
    r = s.dd("check-updates")
    check("detecta actualización disponible", "↑" in r.out, r.out)
    mine = s.h / ".config/kitty/kitty.conf"
    with open(mine, "a") as f:
        f.write("\n# cambio manual del usuario\n")
    r = s.dd("update", "jakoolit-hyprland-dots", "--keep-local", "--yes", "--skip-deps")
    st = s.state("jakoolit-hyprland-dots")
    check("actualiza a la última release", r.returncode == 0 and st["version"]["ref"] != "v2.3.17",
          r.out[-500:])
    check("conserva el cambio manual del usuario", "cambio manual del usuario" in mine.read_text())
    check("backup previo a la actualización", len(st["backups"]) == 2)
    r = s.dd("check-updates")
    check("tras actualizar: todo al día", "Todo al día" in r.out, r.out)
    r = s.dd("restore", st["backup_id"], "--yes")
    check("revertir backup de instalación", r.returncode == 0, r.out[-300:])
    check("dot marcado como desinstalado", s.state("jakoolit-hyprland-dots") is None)
    r = s.dd("orphans")
    check("checkout del dot revertido limpio (sin huérfanos)", "No hay huérfanos" in r.out, r.out)
    after = s.digest()
    check("REVERTIR deja $HOME exactamente igual", before == after, diff(before, after))


def t_orphans(s: Sandbox) -> None:
    print("\n[3] huérfanos: un dot cambia de definición → enlaces que sobran → limpieza segura")
    before = s.digest()
    r = s.dd("install", "jakoolit-hyprland-dots", "--ref", "v2.3.17", "--yes", "--skip-deps")
    check("instala v2.3.17", r.returncode == 0, r.out[-300:])
    # Definición de usuario que ya no incluye wlogout ni swaync (sobrescribe la inicial).
    src = (ROOT / "dots.d/jakoolit-hyprland-dots.toml").read_text()
    cut = src.replace('[[install.links]]\nsrc = "config/swaync"\ndest = "~/.config/swaync"\n\n', "")
    cut = cut.replace('[[install.links]]\nsrc = "config/wlogout"\ndest = "~/.config/wlogout"\n\n', "")
    ud = s.h / ".config/dotdeck/dots.d"
    ud.mkdir(parents=True, exist_ok=True)
    (ud / "jakoolit-hyprland-dots.toml").write_text(cut)
    r = s.dd("update", "jakoolit-hyprland-dots", "--overwrite", "--yes", "--skip-deps")
    check("actualiza con la definición reducida", r.returncode == 0, r.out[-300:])
    r = s.dd("orphans")
    check("detecta ~/.config/wlogout y ~/.config/swaync como huérfanos",
          "~/.config/wlogout" in r.out and "~/.config/swaync" in r.out, r.out)
    check("NO propone el symlink roto ajeno (solo informa)",
          "roto-del-usuario" in r.out and "no se borra" in r.out, r.out)
    # El usuario sustituye un huérfano por algo suyo → ya no es seguro borrarlo.
    sw = s.h / ".config/swaync"
    sw.unlink()
    sw.mkdir()
    (sw / "mio.css").write_text("/* mío */")
    r = s.dd("orphans", "--clean", "--yes")
    check("limpia el huérfano verificado (wlogout)", not (s.h / ".config/wlogout").exists(), r.out)
    check("NO borra lo que el usuario cambió (swaync)", (sw / "mio.css").exists(), r.out)
    check("NO borra el symlink roto no registrado", (s.h / ".config/roto-del-usuario").is_symlink())
    shutil.rmtree(sw)
    (ud / "jakoolit-hyprland-dots.toml").unlink()
    r = s.dd("uninstall", "jakoolit-hyprland-dots", "--yes")
    check("desinstala", r.returncode == 0, r.out[-300:])
    # swaync y wlogout no existían antes: el backup de instalación los "restaura" a no-existir.
    after = s.digest()
    check("$HOME vuelve a quedar exactamente igual", before == after, diff(before, after))


def make_fake_script_repo(base: Path, fail: bool = False) -> Path:
    repo = base / ("fake-shell-fail" if fail else "fake-shell")
    repo.mkdir()
    (repo / "README.md").write_text("# Fake Shell\nInstalador de prueba.\n![shot](shot.png)\n")
    (repo / "install.sh").write_text(f"""#!/bin/bash
set -e
mkdir -p "$HOME/.config/fakeshell" "$HOME/.local/share/fakeshell-data"
echo 'bar=1' > "$HOME/.config/fakeshell/shell.conf"
echo 'cache' > "$HOME/.local/share/fakeshell-data/x"
echo '-- añadido por fakeshell' >> "$HOME/.config/hypr/hyprland.lua"
echo 'source ~/.fakeshellrc' >> "$HOME/.bashrc"
rm -f "$HOME/.config/rofi/theme.rasi"
{'exit 3' if fail else 'exit 0'}
""")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "v1.0"], cwd=repo, check=True)
    return repo


def t_script(s: Sandbox, base: Path) -> None:
    print("\n[4] método script (instalador del autor) con repo local de prueba: diff antes/después y reversión")
    before = s.digest()
    ud = s.h / ".config/dotdeck/dots.d"
    ud.mkdir(parents=True, exist_ok=True)
    for fail in (True, False):
        repo = make_fake_script_repo(base, fail)
        did = "fake-shell-fail" if fail else "fake-shell"
        (ud / f"{did}.toml").write_text(f'''id = "{did}"
name = "Fake Shell"
repo = "file://{repo}"
[track]
mode = "tag"
[install]
method = "script"
command = "bash install.sh"
touches = ["~/.config/hypr", "~/.config/fakeshell", "~/.config/rofi"]
''')
    r = s.dd("install", "fake-shell-fail", "--yes", "--skip-deps", "--no-terminal", ok=False)
    check("instalador que falla → error claro", r.returncode != 0 and "revertido" in r.out, r.out[-300:])
    mid = s.digest()
    check("instalador fallido: rollback automático exacto", before == mid, diff(before, mid))
    r = s.dd("install", "fake-shell", "--yes", "--skip-deps", "--no-terminal")
    check("instala con el script del autor", r.returncode == 0, r.out[-300:])
    st = s.state("fake-shell")
    created = {c["path"].replace(str(s.h), "~") for c in st["created"]}
    check("registra lo creado fuera de touches", "~/.local/share/fakeshell-data" in created, str(created))
    check("detecta ~/.bashrc modificado (cubierto por backup de rc)", any(".bashrc" in m for m in st["modified"]),
          str(st["modified"]))
    r = s.dd("uninstall", "fake-shell", "--yes")
    check("desinstala y restaura", r.returncode == 0, r.out[-300:])
    after = s.digest()
    check("$HOME exactamente igual tras desinstalar", before == after, diff(before, after))
    for did in ("fake-shell", "fake-shell-fail"):
        (ud / f"{did}.toml").unlink()


def t_add(s: Sandbox) -> None:
    print("\n[5] «añadir dot» desde URL real (end-4/dots-hyprland) → instalar → actualizar → desinstalar")
    before = s.digest()
    from dotdeck import catalog, onboard
    pr = onboard.propose("https://github.com/end-4/dots-hyprland?tab=readme-ov-file")
    st = pr.status
    print("    estado detección:", {k: v[0] for k, v in st.items()})
    check("README detectado", st["readme"][0] == "auto")
    check("versionado detectado (release)", pr.dot.track == "release", pr.dot.track)
    check("instalador propio detectado (script)", pr.dot.method == "script" and bool(pr.installers),
          f"{pr.dot.method} {pr.installers}")
    check("rutas que toca marcadas para revisión del usuario", st["touches"][0] in ("review", "missing"))
    check("alternativa por archivos detectada (dots/.config/*)",
          any(l.src.startswith("dots/.config/") for l in pr.alt_links), str([l.src for l in pr.alt_links][:5]))
    # El usuario elige la alternativa "files" (la UI ofrece ese cambio) con un subconjunto.
    d = pr.dot
    d.method, d.command, d.touches = "files", "", []
    d.links = [l for l in pr.alt_links if l.src in ("dots/.config/hypr", "dots/.config/kitty", "dots/.config/fuzzel")]
    d.detected_manual = sorted(set(d.detected_manual) | {"method", "links"})
    os.environ.update({k: s.env[k] for k in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")})
    p = catalog.save_user_dot(d)
    check("definición guardada en ~/.config/dotdeck/dots.d", p.exists(), str(p))
    r = s.dd("list")
    check("aparece en el catálogo como añadido por ti", "añadido por ti" in r.out and d.id in r.out, r.out)
    tag = "2026.05.11"
    r = s.dd("install", d.id, "--ref", tag, "--yes", "--skip-deps")
    check("instala el dot añadido", r.returncode == 0, r.out[-400:])
    r = s.dd("check-updates")
    check("detecta actualización", "↑" in r.out or "Todo al día" in r.out, r.out)
    r = s.dd("update", d.id, "--ref", "main", "--overwrite", "--yes", "--skip-deps")
    check("actualiza el dot añadido", r.returncode == 0, r.out[-300:])
    r = s.dd("uninstall", d.id, "--yes")
    check("desinstala el dot añadido", r.returncode == 0, r.out[-300:])
    r = s.dd("remove", d.id)
    check("elimina la definición añadida", r.returncode == 0 and not p.exists(), r.out)
    after = s.digest()
    check("$HOME exactamente igual", before == after, diff(before, after))


def t_surface(s: Sandbox) -> None:
    print("\n[7] surface-dots (lista real, files, repo de ~900 MB con sparse-checkout): ciclo completo")
    before = s.digest()
    r = s.dd("install", "surface-dots", "--yes", "--skip-deps", "--optional", ".config/color-schemes")
    check("instala surface-dots (solo descarga las carpetas enlazadas)", r.returncode == 0, r.out[-400:])
    st = s.state("surface-dots")
    dep = Path(st["deploy"])
    size = sum(f.stat().st_size for f in dep.rglob("*") if f.is_file() and ".git" not in f.parts)
    check("checkout parcial (< 150 MB)", size < 150e6, f"{size/1e6:.0f} MB")
    check("opcional elegido instalado", (s.h / ".config/color-schemes").is_symlink())
    check("hyprland.lua del dot visible", (s.h / ".config/hypr/hyprland.lua").exists())
    # Simula que la versión instalada es antigua (sigue commits: no hay versiones viejas a mano).
    p = s.h / ".local/state/dotdeck/installed/surface-dots.json"
    d = json.loads(p.read_text())
    d["version"]["commit"] = "0" * 40
    d["version"]["label"] = "main@0000000"
    p.write_text(json.dumps(d))
    r = s.dd("check-updates")
    check("detecta actualización (commit distinto)", "↑" in r.out, r.out)
    with open(s.h / ".config/kitty/kitty.conf", "a") as f:
        f.write("\n# retoque del usuario\n")
    r = s.dd("update", "surface-dots", "--keep-local", "--yes", "--skip-deps")
    check("actualiza conservando el retoque", r.returncode == 0 and
          "retoque del usuario" in (s.h / ".config/kitty/kitty.conf").read_text(), r.out[-300:])
    r = s.dd("uninstall", "surface-dots", "--yes")
    check("desinstala y restaura", r.returncode == 0, r.out[-300:])
    after = s.digest()
    check("$HOME exactamente igual", before == after, diff(before, after))


def t_external(s: Sandbox, base: Path) -> None:
    print("\n[8] instalación existente (tipo HyDE): adoptar → aviso de actualización → actualizar → dejar de seguir")
    origin = base / "ext-origin"
    origin.mkdir()
    (origin / "Scripts").mkdir()
    (origin / "README.md").write_text("# ExtShell\n")
    (origin / "Scripts/install.sh").write_text('#!/bin/bash\necho "v=$(cat ../VERSION)" > "$HOME/.config/kitty/ext.conf"\n'
                                               'echo "-- ext $(cat ../VERSION)" >> "$HOME/.config/hypr/hyprland.lua"\n')
    (origin / "VERSION").write_text("1")
    g = lambda *a, cwd=origin: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                              cwd=cwd, check=True, capture_output=True)
    g("init", "-q", "-b", "master"); g("add", "."); g("commit", "-qm", "v1"); g("tag", "v1")
    clone = s.h / "ExtShell"
    g("clone", "-q", f"file://{origin}", str(clone), cwd=base)
    (clone / "mis-notas.txt").write_text("archivo sin seguimiento del usuario")
    before = s.digest()
    ud = s.h / ".config/dotdeck/dots.d"
    ud.mkdir(parents=True, exist_ok=True)
    (ud / "extshell.toml").write_text(f'''id = "extshell"
name = "ExtShell"
repo = "file://{origin}"
[track]
mode = "commit"
[install]
method = "script"
command = "cd Scripts && bash install.sh"
update_command = "cd Scripts && bash install.sh"
existing = "~/ExtShell"
touches = ["~/.config/kitty", "~/.config/hypr"]
''')
    r = s.dd("list")
    st = s.state("extshell")
    check("adopta la instalación existente", bool(st and st.get("external")) and "instalado" in r.out, r.out)
    r = s.dd("check-updates")
    check("al día mientras no hay cambios", "Todo al día" in r.out, r.out)
    (origin / "VERSION").write_text("2"); g("commit", "-qam", "v2")
    r = s.dd("check-updates")
    check("aviso de actualización disponible", "↑ ExtShell" in r.out, r.out)
    r = s.dd("update", "extshell", "--yes", "--skip-deps", "--no-terminal")
    check("actualiza con los pasos del autor", r.returncode == 0, r.out[-400:])
    check("el clon avanza a v2", (clone / "VERSION").read_text() == "2")
    check("el script del autor se ejecutó", (s.h / ".config/kitty/ext.conf").read_text().strip() == "v=2")
    check("NO borra archivos sin seguimiento del clon", (clone / "mis-notas.txt").exists())
    st = s.state("extshell")
    r = s.dd("restore", st["backups"][-1], "--yes")
    check("revertir la actualización", r.returncode == 0, r.out[-300:])
    check("revertir devuelve el clon a v1", (clone / "VERSION").read_text() == "1")
    check("sigue adoptado tras revertir", s.state("extshell") is not None)
    r = s.dd("uninstall", "extshell", "--yes")
    check("«dejar de seguir»", r.returncode == 0 and s.state("extshell") is None, r.out[-300:])
    check("el clon del usuario NO se borra", (clone / ".git").exists())
    s.dd("list")
    check("no se re-adopta solo tras dejar de seguir", s.state("extshell") is None)
    r = s.dd("install", "extshell", "--yes", "--skip-deps")
    check("«Instalar» lo vuelve a adoptar", r.returncode == 0 and s.state("extshell") is not None, r.out[-300:])
    s.dd("uninstall", "extshell", "--yes")
    (ud / "extshell.toml").unlink()
    after = s.digest()
    check("$HOME igual que antes (salvo lo que el usuario pidió)", before == after, diff(before, after))


def t_errors(s: Sandbox) -> None:
    print("\n[6] errores: repo inválido, espacio insuficiente, choque entre dots")
    from dotdeck import onboard
    try:
        onboard.propose("https://github.com/madkyp/este-repo-no-existe-xyz")
        check("repo inexistente → error claro", False)
    except onboard.OnboardError as e:
        check("repo inexistente → error claro", "no accesible" in str(e), str(e))
    try:
        onboard.propose("hola que tal")
        check("URL inválida → error claro", False)
    except onboard.OnboardError as e:
        check("URL inválida → error claro", "no parece" in str(e), str(e))
    from dotdeck import backup
    old = backup.MARGIN
    backup.MARGIN = 1 << 60
    try:
        backup.create("t", "install", [s.h / ".config/kitty"])
        check("sin espacio → backup rechazado", False)
    except backup.BackupError as e:
        check("sin espacio → backup rechazado sin tocar nada", "Espacio insuficiente" in str(e), str(e))
    backup.MARGIN = old
    s.dd("install", "athena-eww", "--yes", "--skip-deps")
    r2 = s.dd("install", "surface-dots", "--yes", "--skip-deps", ok=False)
    check("choque entre dots detectado (rofi/kitty/dunst…)", r2.returncode != 0 and "Choca" in r2.out, r2.out[-300:])
    s.dd("uninstall", "athena-eww", "--yes")


def main() -> int:
    keep = "--keep" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    base = Path(tempfile.mkdtemp(prefix="dotdeck-e2e-", dir=os.environ.get("DOTDECK_TEST_TMP")))
    if not os.environ.get("GITHUB_TOKEN") and shutil.which("gh"):
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        if tok:
            os.environ["GITHUB_TOKEN"] = tok  # evita el límite de 60 consultas/h a la API
    s = Sandbox(base)
    # Todo el proceso de test (también las llamadas en-proceso) apunta al sandbox.
    for k in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        os.environ[k] = s.env[k]
    os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    s.populate()
    print(f"sandbox: {s.h}")
    tests = {"athena": lambda: t_files_athena(s), "update": lambda: t_update_jakoolit(s),
             "orphans": lambda: t_orphans(s), "script": lambda: t_script(s, base),
             "add": lambda: t_add(s), "surface": lambda: t_surface(s), "external": lambda: t_external(s, base), "errors": lambda: t_errors(s)}
    for name, fn in tests.items():
        if only and name != only:
            continue
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{name}: excepción inesperada", False, str(e))
    print(f"\nRESULTADO: {len(PASS)} OK, {len(FAIL)} fallos")
    for f in FAIL:
        print("  ✘", f)
    if not keep:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
