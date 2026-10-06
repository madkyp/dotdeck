"""Catálogo de dots: carga, validación y guardado de definiciones TOML.

Dos orígenes:
  * inicial  → <app>/dots.d/*.toml           (lista que se distribuye con la app)
  * usuario  → ~/.config/dotdeck/dots.d/*.toml (añadidos desde la app o a mano)
Un dot de usuario con el mismo id que uno inicial lo sobrescribe.
Ver docs/dot-format.toml para el formato documentado.
"""
from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config, tomlw
from .config import log

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
METHODS = ("files", "script")
TRACK_MODES = ("release", "tag", "commit")


class DotError(Exception):
    pass


@dataclass
class Link:
    src: str            # ruta dentro del repo
    dest: str           # destino, admite ~
    optional: bool = False
    note: str = ""


@dataclass
class Dot:
    id: str
    name: str
    repo: str
    author: str = ""
    description: str = ""
    license: str = ""
    branch: str = ""
    example: bool = False
    track: str = "commit"
    method: str = "files"
    links: list[Link] = field(default_factory=list)
    command: str = ""
    update_command: str = ""
    uninstall_command: str = ""
    terminal: bool = True
    existing: str = ""             # clon ya instalado fuera de DotDeck (p.ej. ~/HyDE) → se adopta
    touches: list[str] = field(default_factory=list)
    pacman: list[str] = field(default_factory=list)
    aur: list[str] = field(default_factory=list)
    deps_note: str = ""
    readme: str = "README.md"
    images: list[str] = field(default_factory=list)
    about: str = ""                # presentación propia si el README no tiene una buena
    cover: str = ""                # portada: ruta del repo, URL o archivo junto a la definición
    reload: list[str] = field(default_factory=list)
    reload_note: str = ""
    warnings: list[str] = field(default_factory=list)
    hypr_format: str = ""          # "lua" | "conf" | "" (desconocido / no toca hypr)
    detected_auto: list[str] = field(default_factory=list)
    detected_manual: list[str] = field(default_factory=list)
    # no serializados
    origin: str = "initial"        # initial | user
    path: Path | None = None

    # ---------- helpers ----------
    @property
    def owner_repo(self) -> tuple[str, str] | None:
        m = re.match(r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$", self.repo)
        return (m.group(1), m.group(2)) if m else None

    def backup_paths(self, selected_links: list[Link] | None = None) -> list[Path]:
        """Todas las rutas que hay que respaldar antes de aplicar el dot."""
        links = self.links if selected_links is None else selected_links
        out: list[Path] = []
        for p in [l.dest for l in links] + list(self.touches):
            ep = config.expand(p)
            if ep not in out:
                out.append(ep)
        return out

    def cover_source(self) -> str:
        """Portada lista para cargar: archivo local (relativo a la definición) o ruta/URL tal cual."""
        if not self.cover:
            return ""
        if self.cover.startswith(("http://", "https://")):
            return self.cover
        if self.path is not None:
            local = (self.path.parent / self.cover).resolve()
            if local.is_file():
                return str(local)
        return self.cover

    def touches_hypr(self) -> bool:
        hy = config.config_home() / "hypr"
        for p in self.backup_paths():
            if p == hy or hy in p.parents or p in hy.parents:
                return True
        return False

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "repo": self.repo,
            "description": self.description,
            "license": self.license,
        }
        if self.branch:
            d["branch"] = self.branch
        if self.example:
            d["example"] = True
        if self.hypr_format:
            d["hypr_format"] = self.hypr_format
        if self.warnings:
            d["warnings"] = self.warnings
        d["track"] = {"mode": self.track}
        inst: dict = {"method": self.method}
        if self.method == "script":
            inst["command"] = self.command
            if self.update_command:
                inst["update_command"] = self.update_command
            if self.uninstall_command:
                inst["uninstall_command"] = self.uninstall_command
            inst["terminal"] = self.terminal
            if self.existing:
                inst["existing"] = self.existing
        inst["touches"] = self.touches
        if self.links:
            inst["links"] = [
                {k: v for k, v in (("src", l.src), ("dest", l.dest),
                                   ("optional", l.optional), ("note", l.note)) if v}
                for l in self.links
            ]
        d["install"] = inst
        d["deps"] = {"pacman": self.pacman, "aur": self.aur}
        if self.deps_note:
            d["deps"]["note"] = self.deps_note
        d["preview"] = {"readme": self.readme, "images": self.images}
        if self.cover:
            d["preview"]["cover"] = self.cover
        if self.about:
            d["preview"]["about"] = self.about
        d["reload"] = {"commands": self.reload}
        if self.reload_note:
            d["reload"]["note"] = self.reload_note
        if self.detected_auto or self.detected_manual:
            d["detected"] = {"auto": self.detected_auto, "manual": self.detected_manual}
        return d


def from_dict(d: dict, origin: str = "initial", path: Path | None = None) -> Dot:
    try:
        inst = d.get("install", {})
        deps = d.get("deps", {})
        prev = d.get("preview", {})
        rel = d.get("reload", {})
        det = d.get("detected", {})
        dot = Dot(
            id=str(d["id"]),
            name=str(d.get("name") or d["id"]),
            repo=str(d["repo"]).strip(),
            author=str(d.get("author", "")),
            description=str(d.get("description", "")),
            license=str(d.get("license", "")),
            branch=str(d.get("branch", "")),
            example=bool(d.get("example", False)),
            track=str(d.get("track", {}).get("mode", "commit")),
            method=str(inst.get("method", "files")),
            links=[Link(src=str(l["src"]), dest=str(l["dest"]),
                        optional=bool(l.get("optional", False)), note=str(l.get("note", "")))
                   for l in inst.get("links", [])],
            command=str(inst.get("command", "")),
            update_command=str(inst.get("update_command", "")),
            uninstall_command=str(inst.get("uninstall_command", "")),
            terminal=bool(inst.get("terminal", True)),
            existing=str(inst.get("existing", "")),
            touches=[str(t) for t in inst.get("touches", [])],
            pacman=[str(p) for p in deps.get("pacman", [])],
            aur=[str(p) for p in deps.get("aur", [])],
            deps_note=str(deps.get("note", "")),
            readme=str(prev.get("readme", "README.md")),
            images=[str(i) for i in prev.get("images", [])],
            cover=str(prev.get("cover", "")),
            about=str(prev.get("about", "")),
            reload=[str(c) for c in rel.get("commands", [])],
            reload_note=str(rel.get("note", "")),
            warnings=[str(w) for w in d.get("warnings", [])],
            hypr_format=str(d.get("hypr_format", "")),
            detected_auto=[str(x) for x in det.get("auto", [])],
            detected_manual=[str(x) for x in det.get("manual", [])],
            origin=origin,
            path=path,
        )
    except KeyError as e:
        raise DotError(f"falta el campo obligatorio {e}") from None
    validate(dot)
    return dot


def validate(dot: Dot) -> None:
    errs = []
    if not ID_RE.match(dot.id):
        errs.append(f"id inválido '{dot.id}' (minúsculas, números, . _ -)")
    if not re.match(r"^(https?://|git@|file://|/)", dot.repo):
        errs.append(f"repo inválido '{dot.repo}'")
    if dot.method not in METHODS:
        errs.append(f"install.method debe ser uno de {METHODS}")
    if dot.track not in TRACK_MODES:
        errs.append(f"track.mode debe ser uno de {TRACK_MODES}")
    if dot.method == "files" and not dot.links:
        errs.append("method='files' necesita al menos un [[install.links]]")
    if dot.method == "script" and not dot.command.strip():
        errs.append("method='script' necesita install.command")
    if dot.method == "script" and not dot.touches:
        errs.append("method='script' necesita install.touches (rutas a respaldar)")
    h = config.home()
    for l in dot.links:
        if l.src.startswith("/") or ".." in Path(l.src).parts:
            errs.append(f"link.src debe ser relativo al repo: {l.src}")
        dp = config.expand(l.dest)
        if not str(dp).startswith(str(h) + "/"):
            errs.append(f"link.dest debe estar dentro de $HOME: {l.dest}")
        elif dp in (h, config.config_home(), h / ".local", h / ".local/share"):
            errs.append(f"link.dest demasiado amplio: {l.dest}")
    for t in dot.touches:
        tp = config.expand(t)
        if tp in (h, config.config_home()):
            errs.append(f"touches demasiado amplio ({t}); indica subcarpetas concretas")
    if errs:
        raise DotError("; ".join(errs))


def load_file(path: Path, origin: str) -> Dot:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return from_dict(data, origin=origin, path=path)


def load_all() -> tuple[dict[str, Dot], list[str]]:
    """Devuelve (dots por id, errores de carga legibles)."""
    dots: dict[str, Dot] = {}
    errors: list[str] = []
    for origin, d in (("initial", config.builtin_catalog_dir()), ("user", config.user_catalog_dir())):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.toml")):
            try:
                dot = load_file(p, origin)
                if dot.id in dots and origin == "user":
                    log.info("dot de usuario %s sobrescribe al inicial", dot.id)
                dots[dot.id] = dot
            except (DotError, tomllib.TOMLDecodeError, OSError) as e:
                errors.append(f"{p.name}: {e}")
                log.error("definición inválida %s: %s", p, e)
    return dots, errors


HEADER_COMMENTS = {
    "id": "Identificador único (minúsculas). Formato documentado en docs/dot-format.toml",
    "detected": "Qué campos detectó DotDeck solo y cuáles rellenaste/revisaste tú",
}


def save_user_dot(dot: Dot) -> Path:
    validate(dot)
    d = config.user_catalog_dir()
    d.mkdir(parents=True, exist_ok=True)
    if dot.cover.startswith("/"):
        # Portada elegida desde un archivo local: se copia junto a la definición.
        src = Path(dot.cover)
        if not src.is_file():
            raise DotError(f"la portada no existe: {src}")
        dst = d / "covers" / f"{dot.id}{src.suffix.lower() or '.png'}"
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o644)
        dot.cover = f"covers/{dst.name}"
    p = d / f"{dot.id}.toml"
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text(tomlw.dumps(dot.to_dict(), HEADER_COMMENTS))
    # Re-leer para garantizar que lo escrito es válido antes de reemplazar.
    load_file(tmp, "user")
    tmp.replace(p)
    dot.origin, dot.path = "user", p
    log.info("definición guardada: %s", p)
    return p


def delete_user_dot(dot: Dot) -> None:
    if dot.origin != "user" or not dot.path:
        raise DotError("solo se pueden eliminar dots añadidos por ti")
    dot.path.unlink()
    log.info("definición eliminada: %s", dot.path)
