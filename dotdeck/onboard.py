"""Incorporación de dots nuevos a partir de una URL de repositorio.

Genera una propuesta de definición y, para cada campo, indica si se detectó
con garantías ("auto"), por heurística ("review": el usuario debe revisarlo)
o si no se pudo detectar ("missing": el usuario debe rellenarlo).
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, config, deps, gitrepo
from .catalog import Dot, Link
from .config import log

IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif")
KNOWN_APPS = {
    "hypr", "waybar", "rofi", "wofi", "kitty", "alacritty", "foot", "ghostty", "wezterm",
    "dunst", "mako", "swaync", "eww", "ags", "quickshell", "fastfetch", "neofetch", "btop",
    "cava", "wlogout", "swaylock", "hyprlock", "hypridle", "hyprpaper", "starship", "fish",
    "zsh", "nvim", "gtk-3.0", "gtk-4.0", "qt5ct", "qt6ct", "Kvantum", "kvantum", "matugen",
    "wal", "fuzzel", "walker", "anyrun", "yazi", "zathura", "swappy", "nwg-dock-hyprland",
    "uwsm", "spicetify", "mpv", "tofi", "swayosd", "satty", "wleave", "elephant",
}
SKIP_CONFIG = {"Code", "VSCodium", ".vscode", "firefox", "zen", "chromium", "discord", "vesktop"}
INSTALLER_NAMES = ["install.sh", "setup.sh", "install", "setup", "copy.sh", "install/install.sh",
                   "scripts/install.sh", "installer.sh", "bootstrap.sh"]
HOME_RC = {".zshrc", ".bashrc", ".profile", ".zprofile", ".bash_profile", ".xprofile"}
PATH_RE = re.compile(r"(?:\$HOME|\$\{HOME\}|~|\$\{?XDG_CONFIG_HOME(?::-[^}]*)?\}?)"
                     r"(/\.config|/\.local/share|/\.local/bin|/\.local/state|/Pictures|/\.themes|/\.icons)?"
                     r"/([A-Za-z0-9._-]+)")


class OnboardError(Exception):
    pass


@dataclass
class Proposal:
    dot: Dot
    status: dict[str, tuple[str, str]] = field(default_factory=dict)  # campo → (auto|review|missing, nota)
    warnings: list[str] = field(default_factory=list)
    alt_links: list[Link] = field(default_factory=list)   # si el método es script: alternativa "files"
    installers: list[str] = field(default_factory=list)
    clone: Path | None = None

    def needs_user(self) -> list[str]:
        return [k for k, (s, _) in self.status.items() if s in ("review", "missing")]


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-.")
    return s[:60] or "dot"


def propose(url: str, progress=lambda m: None) -> Proposal:
    raw = url
    url = gitrepo.normalize_url(url)
    if not re.match(r"^(https?://|git@|file://|/)", url):
        raise OnboardError(f"«{raw}» no parece una URL de repositorio git")
    progress("Comprobando que el repositorio existe…")
    try:
        refs = gitrepo.ls_remote(url)
    except gitrepo.GitError as e:
        raise OnboardError(f"Repositorio no accesible o inválido: {e}") from e
    if not any(k.startswith("refs/heads/") for k in refs):
        raise OnboardError("El repositorio está vacío (no tiene ramas)")
    branch = gitrepo.branch_from_url(raw)
    default = gitrepo.default_branch(refs)
    if branch and f"refs/heads/{branch}" not in refs:
        branch = ""

    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)$", url)
    owner, name = (m.group(1), m.group(2)) if m else ("", url.rstrip("/").split("/")[-1].removesuffix(".git"))
    dot = Dot(id=_slug(name), name=name.replace("_", " ").replace("-", " ").strip().title() if name.islower() else name,
              repo=url, author=owner, branch=branch if branch and branch != default else "")
    pr = Proposal(dot)
    st = pr.status
    st["name"] = ("auto", "nombre del repositorio")
    st["author"] = ("auto", "propietario del repositorio") if owner else ("missing", "no se pudo deducir")

    meta = None
    if m:
        progress("Leyendo metadatos de GitHub…")
        try:
            meta = gitrepo.github_api(f"repos/{owner}/{name}")
        except gitrepo.GitError as e:
            pr.warnings.append(f"Sin metadatos de GitHub ({e}); algunos campos quedan por rellenar")
    if meta:
        dot.description = meta.get("description") or ""
        dot.license = ((meta.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", "")
        if meta.get("archived"):
            pr.warnings.append("El repositorio está ARCHIVADO: el autor ya no lo mantiene")
        if meta.get("full_name", "").lower() != f"{owner}/{name}".lower():
            pr.warnings.append(f"El repositorio se ha movido a {meta['full_name']}")
    st["description"] = ("auto", "descripción de GitHub") if dot.description else ("missing", "escribe una breve descripción")
    st["license"] = ("auto", "licencia declarada en GitHub") if dot.license else (
        "review", "el repo no declara licencia: revisa si puedes usarlo")

    progress("Descargando el índice de archivos (sin contenido)…")
    try:
        clone = gitrepo.preview_clone(url, refresh=True)
        files = gitrepo.ls_files_with_mode(clone)
    except gitrepo.GitError as e:
        raise OnboardError(f"No se pudo clonar el repositorio: {e}") from e
    pr.clone = clone
    names = list(files)

    # ---- README
    # Mismo orden que GitHub: raíz, .github/, docs/
    readmes = [f for f in names if re.fullmatch(r"(?i)(\.github/|docs/)?readme(\.(md|markdown|rst|txt))?", f)]
    readmes.sort(key=lambda f: (f.count("/"), f.startswith("docs/"), not f.lower().endswith(".md"), f))
    if readmes:
        dot.readme = readmes[0]
        st["readme"] = ("auto", readmes[0])
        readme_txt = gitrepo.read_file(clone, readmes[0]).decode("utf-8", "replace")
    else:
        dot.readme = ""
        st["readme"] = ("missing", "no hay README en la raíz")
        readme_txt = ""

    # ---- imágenes
    progress("Buscando capturas…")
    dot.images = _find_images(readme_txt, names, dot.readme)
    st["images"] = ("auto", f"{len(dot.images)} imágenes") if dot.images else (
        "review", "no se encontraron capturas; puedes añadir rutas o URLs")

    # ---- versionado
    progress("Detectando cómo versiona el autor…")
    rel = None
    if m:
        try:
            rel = gitrepo.github_api(f"repos/{owner}/{name}/releases?per_page=1")
        except gitrepo.GitError as e:
            rel = None
            pr.warnings.append(f"No se pudo consultar las releases ({e}); revisa el versionado")
    if rel:
        dot.track = "release"
        st["track"] = ("auto", f"publica releases (última: {rel[0].get('tag_name')})")
    elif gitrepo.latest_tag(refs):
        dot.track = "tag"
        st["track"] = ("auto", f"usa tags (último: {gitrepo.latest_tag(refs)[0]})")
    else:
        dot.track = "commit"
        st["track"] = ("auto", f"sin releases ni tags: se siguen los commits de {branch or default}")

    # ---- formato de Hyprland
    lua = [f for f in names if re.search(r"(^|/)hypr(land)?[^/]*/.*\.lua$|(^|/)hyprland\.lua$", f)]
    conf = [f for f in names if f.endswith("hyprland.conf")]
    if lua:
        dot.hypr_format = "lua"
    elif conf:
        dot.hypr_format = "conf"
    mine_lua = (config.config_home() / "hypr" / "hyprland.lua").exists()
    if dot.hypr_format == "conf" and mine_lua:
        pr.warnings.append("Este dot usa hyprland.conf (formato clásico) y tu Hyprland usa "
                           "hyprland.lua: probablemente haya que adaptarlo")
    if dot.hypr_format:
        dot.warnings = [w for w in pr.warnings if "hyprland.conf" in w]

    # ---- rutas de configuración del repo (método files)
    links = _find_links(names)
    pr.alt_links = links

    # ---- instalador propio
    inst = []
    for c in INSTALLER_NAMES:
        if c in files:
            inst.append(c)
    for f, mode in files.items():
        if "/" not in f and mode == "100755" and re.search(r"(?i)install", f) and f not in inst:
            inst.append(f)
    pr.installers = inst
    opaque = [f for f in inst if not re.search(r"\.(sh|py|bash|fish)$|^(install|setup)$", f.split("/")[-1])]

    script_text = ""
    for f in inst:
        if f in opaque:
            continue
        try:
            script_text += gitrepo.read_file(clone, f, 2_000_000).decode("utf-8", "replace") + "\n"
        except gitrepo.GitError:
            pass

    if inst and not (opaque and links):
        main = inst[0]
        dot.method = "script"
        dot.command = f"bash ./{main}" if main not in opaque else f"./{main}"
        dot.terminal = True
        st["method"] = ("review", f"tiene instalador propio ({', '.join(inst)}); revisa el comando")
        st["command"] = ("review", "comando propuesto; añade flags si el autor los recomienda")
        touches = _touches_from_script(script_text) | {l.dest for l in links}
        dot.touches = sorted(touches)
        st["touches"] = ("review" if touches else "missing",
                         "deducido leyendo el instalador y el repo: confírmalo, solo se respalda lo que figure aquí"
                         if touches else "indica qué rutas toca el instalador (se respaldan antes)")
        if opaque:
            pr.warnings.append(f"El instalador {', '.join(opaque)} es un binario: DotDeck no puede leer qué hace")
        if re.search(r"\b(curl|wget)\b[^\n|]*\|\s*(ba)?sh", script_text):
            pr.warnings.append("El instalador descarga y ejecuta otros scripts de Internet")
        if re.search(r"\bsudo\b", script_text):
            pr.warnings.append("El instalador usa sudo (se ejecuta en una terminal visible y te pedirá la contraseña)")
        un = [f for f in names if re.fullmatch(r"(?:[^/]+/)?uninstall\.sh", f)]
        if un:
            dot.uninstall_command = f"bash ./{un[0]}"
            st["uninstall_command"] = ("auto", un[0])
    elif links:
        dot.method = "files"
        dot.links = links
        st["method"] = ("auto", "sin instalador: se enlazan sus carpetas de configuración")
        st["links"] = ("auto" if all(not l.optional for l in links) else "review",
                       f"{len(links)} rutas detectadas en el repo")
        if opaque:
            pr.warnings.append(f"Hay un instalador binario ({', '.join(opaque)}); se propone copiar "
                               "la configuración directamente en su lugar")
    elif (pkg := find_package(name, progress)):
        # Programa empaquetado (p.ej. noctalia): se instala con pacman/AUR y se respalda su config.
        pkgname, source = pkg
        cfg = sorted({m.group(1) for m in re.finditer(
            r"~/\.config/([A-Za-z0-9._-]+)|\$XDG_CONFIG_HOME/([A-Za-z0-9._-]+)", readme_txt) if m.group(1)}
            | {m.group(2) for m in re.finditer(r"\$XDG_CONFIG_HOME/([A-Za-z0-9._-]+)", readme_txt)} - {None})
        cfg = [c for c in cfg if c] or [pkgname.removesuffix("-git").removesuffix("-bin").removesuffix("-shell")]
        dot.method = "script"
        if source == "repo":
            dot.command = f"sudo pacman -S --needed {pkgname}"
        else:
            dot.command = f"{deps.aur_helper() or 'yay'} -S --needed {pkgname}"
        dot.uninstall_command = f"sudo pacman -Rns {pkgname}"
        dot.touches = [f"~/.config/{c}" for c in cfg]
        dot.deps_note = f"Se instala como paquete ({pkgname}, {'repos oficiales' if source == 'repo' else 'AUR'})."
        st["method"] = ("review", f"no es un dotfile: es un programa empaquetado ({pkgname} en "
                                  f"{'tus repos' if source == 'repo' else 'el AUR'})")
        st["command"] = ("auto", "instalación con el gestor de paquetes")
        st["touches"] = ("review", "carpeta de configuración deducida: confírmala")
        pr.warnings.append(f"Este repo es un programa compilado: se instalará el paquete «{pkgname}» "
                           "y DotDeck respaldará su configuración antes.")
    else:
        dot.method = "files"
        st["method"] = ("missing", "no se encontró instalador, carpetas de configuración ni paquete")
        st["links"] = ("missing", "indica qué carpetas del repo van a qué ruta de tu $HOME, "
                                  "o cambia el método a «instalador del autor» y escribe el comando")

    # ---- dependencias
    pac, aur = _find_deps(readme_txt + "\n" + script_text, files, clone)
    dot.pacman, dot.aur = pac, aur
    if pac or aur:
        st["deps"] = ("review", "extraídas del README/instalador: revísalas")
    else:
        st["deps"] = ("review", "no se detectaron dependencias: añade las que indique el README")
        if dot.method == "script":
            dot.deps_note = "El instalador del autor gestiona sus propias dependencias."

    # ---- id único
    existing, _ = catalog.load_all()
    if dot.id in existing:
        dot.id = _slug(f"{dot.id}-{owner}") if owner else dot.id + "-2"
    st["id"] = ("auto", dot.id)
    dot.origin = "user"
    dot.detected_auto = sorted(k for k, (s, _) in st.items() if s == "auto")
    dot.detected_manual = sorted(k for k, (s, _) in st.items() if s != "auto")
    log.info("propuesta para %s: %s", url, {k: v[0] for k, v in st.items()})
    return pr


def _find_images(readme: str, names: list[str], readme_path: str) -> list[str]:
    refs = re.findall(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)", readme)
    refs += re.findall(r"<img[^>]+src=[\"']([^\"']+)", readme, flags=re.I)
    refs += re.findall(r"<source[^>]+srcset=[\"']([^\"']+)", readme, flags=re.I)
    out: list[str] = []
    base = str(Path(readme_path).parent) if readme_path else "."
    nameset = set(names)
    for r in refs:
        if re.search(r"shields\.io|badge|img\.shields|komarev|visitor|star-history|contrib\.rocks|"
                     r"github-readme-stats|readme-typing|capsule-render|\.svg(\?|$)", r, re.I):
            continue
        m = re.match(r"^https?://github\.com/[^/]+/[^/]+/(?:blob|raw)/[^/]+/(.+)$", r)
        if m and m.group(1) in nameset:
            r = m.group(1)
        if r.startswith(("http://", "https://")):
            out.append(r)
            continue
        rel = resolve_rel(base, r)
        if rel in nameset and rel.lower().endswith(IMG_EXT):
            out.append(rel)
    if len(out) < 3:
        for n in names:
            if n.lower().endswith(IMG_EXT) and re.search(r"(?i)(screenshot|screens|preview|showcase|demo)", n):
                out.append(n)
    seen, uniq = set(), []
    for o in out:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return uniq[:12]


def find_package(name: str, progress=lambda m: None) -> tuple[str, str] | None:
    """¿Existe un paquete con el nombre del repo? → (paquete, "repo"|"aur")."""
    import json
    import subprocess
    import urllib.request
    base = name.lower()
    cands = [base, f"{base}-shell", f"{base}-bin", f"{base}-git"]
    progress("Buscando si se distribuye como paquete…")
    for c in cands:
        if subprocess.run(["pacman", "-Si", c], capture_output=True).returncode == 0:
            return c, "repo"
    try:
        q = "&".join(f"arg[]={c}" for c in cands)
        with urllib.request.urlopen(f"https://aur.archlinux.org/rpc/v5/info?{q}", timeout=15) as r:
            found = {x["Name"] for x in json.load(r).get("results", [])}
        for c in cands:
            if c in found:
                return c, "aur"
    except (OSError, ValueError) as e:
        log.info("AUR no consultable: %s", e)
    return None


def resolve_rel(base: str, ref: str) -> str:
    """Ruta de una imagen referenciada desde un README situado en ``base``."""
    ref = re.sub(r"[?#].*$", "", ref)
    if ref.startswith("/"):
        return posixpath.normpath(ref.lstrip("/"))
    return posixpath.normpath(posixpath.join(base if base != "." else "", ref)).lstrip("/")


def _find_links(names: list[str]) -> list[Link]:
    found: dict[str, Link] = {}
    bases: set[str] = set()
    for n in names:
        m = re.match(r"^((?:[^/]+/){0,2}?)\.config/([^/]+)(/|$)", n)
        if m and not n.startswith((".github/", "nix/")):
            base, app = m.group(1), m.group(2)
            bases.add(base)
            if app in SKIP_CONFIG:
                continue
            src = f"{base}.config/{app}"
            found.setdefault(src, Link(src=src, dest=f"~/.config/{app}"))
    if not found:
        for n in names:
            m = re.match(r"^((?:dots|dotfiles|home|config)/)?([^/]+)/", n)
            if m and m.group(2) in KNOWN_APPS:
                src = f"{m.group(1) or ''}{m.group(2)}"
                found.setdefault(src, Link(src=src, dest=f"~/.config/{m.group(2)}"))
        top_cfg = {n.split("/")[1] for n in names if n.startswith("config/") and n.count("/") >= 2}
        if top_cfg and not found:
            for app in sorted(top_cfg):
                found.setdefault(f"config/{app}", Link(src=f"config/{app}", dest=f"~/.config/{app}"))
    for base in bases or {""}:
        for n in names:
            if n.startswith(base) and n[len(base):] in HOME_RC:
                rc = n[len(base):]
                found.setdefault(n, Link(src=n, dest=f"~/{rc}", optional=True,
                                         note="sustituye tu archivo de shell; desactivado por defecto"))
            m = re.match(rf"^{re.escape(base)}\.local/(bin|share/[^/]+)/", n)
            if m:
                src = f"{base}.local/{m.group(1)}"
                if m.group(1) == "bin":
                    continue  # ~/.local/bin entero es demasiado amplio
                found.setdefault(src, Link(src=src, dest=f"~/.local/{m.group(1)}"))
    return sorted(found.values(), key=lambda l: (l.optional, l.src))


def _touches_from_script(text: str) -> set[str]:
    out = set()
    for m in PATH_RE.finditer(text):
        sub, leaf = m.group(1), m.group(2)
        if not sub:
            prefix = m.group(0)
            if "XDG_CONFIG_HOME" in prefix:
                sub = "/.config"
            else:
                if leaf.startswith(".") and leaf in HOME_RC:
                    out.add(f"~/{leaf}")
                continue
        if leaf in (".", "..") or leaf.endswith((".log", ".tmp")) or "backup" in leaf.lower():
            continue
        out.add(f"~{sub}/{leaf}")
    return out


def _find_deps(text: str, files: dict, clone: Path) -> tuple[list[str], list[str]]:
    pac: set[str] = set()
    aur: set[str] = set()
    for m in re.finditer(r"(?m)(?:sudo\s+)?(pacman|yay|paru)\s+-S\w*\s+((?:[^\n\\]|\\\n)+)", text):
        tool, rest = m.group(1), m.group(2).replace("\\\n", " ")
        for tok in rest.split():
            if tok.startswith("-") or tok in ("&&", "||", "|", ";") or not re.fullmatch(r"[a-z0-9@._+-]+", tok):
                if tok in ("&&", "||", "|", ";"):
                    break
                continue
            (pac if tool == "pacman" else aur).add(tok)
    for cand in ("packages.txt", "pkgs.txt", "install/pkgs.txt", "install/packages.txt", "deps.txt"):
        if cand in files:
            try:
                for line in gitrepo.read_file(clone, cand).decode("utf-8", "replace").splitlines():
                    line = line.split("#")[0].strip()
                    if re.fullmatch(r"[a-z0-9@._+-]+", line):
                        pac.add(line)
            except gitrepo.GitError:
                pass
    aur -= pac
    return sorted(pac)[:80], sorted(aur)[:40]
