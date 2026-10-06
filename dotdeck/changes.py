"""Qué trae una actualización: commits agrupados, configuración afectada y notas del autor.

Usa la API de comparación de GitHub (versión instalada → última disponible).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from . import gitrepo
from .catalog import Dot
from .config import log

CC = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<title>.+)$")
GROUPS = [  # (clave, título, tipos convencionales)
    ("breaking", "⚠ Cambios importantes", ()),
    ("feat", "✨ Novedades", ("feat", "feature", "add")),
    ("fix", "🐛 Correcciones", ("fix", "bugfix", "hotfix")),
    ("perf", "⚡ Rendimiento", ("perf",)),
    ("refactor", "♻ Reestructuración", ("refactor",)),
    ("style", "🎨 Aspecto", ("style", "theme", "ui")),
    ("docs", "📝 Documentación", ("docs", "doc")),
    ("chore", "🔧 Mantenimiento", ("chore", "ci", "build", "test", "tests", "deps", "revert", "release")),
    ("other", "• Otros cambios", ()),
]
CONFIG_RE = re.compile(r"(?:^|/)(\.config/[^/]+|\.local/(?:share|lib|bin|state)/[^/]+)")


class ChangesError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    title: str
    scope: str
    author: str
    date: str
    group: str
    url: str


@dataclass
class Changes:
    installed: str
    latest: str
    commits: list[Commit] = field(default_factory=list)
    total: int = 0
    files: int = 0
    additions: int = 0
    deletions: int = 0
    areas: list[tuple[str, int]] = field(default_factory=list)   # ~/.config/hypr → nº de archivos
    notes: str = ""            # notas del autor (CHANGELOG nuevo o releases), en Markdown
    notes_source: str = ""
    url: str = ""
    first_date: str = ""
    last_date: str = ""

    def grouped(self) -> list[tuple[str, str, list[Commit]]]:
        out = []
        for key, title, _ in GROUPS:
            items = [c for c in self.commits if c.group == key]
            if items:
                out.append((key, title, items))
        return out


def _group(ctype: str, bang: bool) -> str:
    if bang:
        return "breaking"
    ctype = ctype.lower()
    for key, _, types in GROUPS:
        if ctype in types:
            return key
    return "other"


def _area(path: str, dot: Dot) -> str | None:
    """Ruta del repo → zona de tu $HOME que cambia (o None si no es configuración)."""
    for l in dot.links:
        src = l.src.strip("/")
        if path == src or path.startswith(src + "/"):
            return l.dest
    m = CONFIG_RE.search(path)
    return "~/" + m.group(1) if m else None


def fetch(dot: Dot, st: dict, latest: dict) -> Changes:
    orr = dot.owner_repo
    if not orr:
        raise ChangesError("Solo se pueden ver los cambios de repositorios de GitHub")
    base = st["version"].get("commit") or st["version"]["ref"]
    head = latest.get("commit") or latest["ref"]
    try:
        d = gitrepo.github_api(f"repos/{orr[0]}/{orr[1]}/compare/{base}...{head}", timeout=40)
    except gitrepo.GitError as e:
        raise ChangesError(f"No se pudo consultar GitHub: {e}") from e
    if not d:
        raise ChangesError("GitHub no encuentra la versión instalada (¿historial reescrito?)")
    ch = Changes(installed=st["version"]["label"], latest=latest["label"], url=d.get("html_url", ""),
                 total=d.get("total_commits", 0))
    for c in d.get("commits", []):
        msg = (c["commit"]["message"] or "").split("\n")[0].strip()
        if msg.lower().startswith("merge "):
            continue
        m = CC.match(msg)
        body = c["commit"]["message"] or ""
        bang = bool(m and m.group("bang")) or "BREAKING CHANGE" in body
        ch.commits.append(Commit(
            sha=c["sha"], title=(m.group("title") if m else msg), scope=(m.group("scope") or "") if m else "",
            author=(c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
            date=c["commit"]["committer"]["date"][:10],
            group=_group(m.group("type"), bang) if m else ("breaking" if bang else "other"),
            url=c.get("html_url", "")))
    ch.commits.reverse()  # lo más reciente primero
    if ch.commits:
        ch.last_date, ch.first_date = ch.commits[0].date, ch.commits[-1].date
    files = d.get("files", [])
    ch.files = len(files)
    ch.additions = sum(f.get("additions", 0) for f in files)
    ch.deletions = sum(f.get("deletions", 0) for f in files)
    areas = Counter(a for f in files if (a := _area(f["filename"], dot)))
    ch.areas = areas.most_common()

    # Notas del autor: lo añadido al CHANGELOG, o las releases publicadas entre medias.
    for f in files:
        if re.fullmatch(r"(?i)(CHANGELOG|CHANGES|NEWS)(\.md)?", f["filename"]) and f.get("patch"):
            added = [l[1:] for l in f["patch"].splitlines() if l.startswith("+") and not l.startswith("+++")]
            if added:
                ch.notes, ch.notes_source = "\n".join(added).strip(), f["filename"]
                break
    if not ch.notes and dot.track in ("release", "tag"):
        try:
            rels = gitrepo.github_api(f"repos/{orr[0]}/{orr[1]}/releases?per_page=20") or []
        except gitrepo.GitError:
            rels = []
        parts = []
        for r in rels:
            if r.get("tag_name") == st["version"]["ref"]:
                break
            if r.get("body"):
                parts.append(f"## {r.get('name') or r['tag_name']}\n\n{r['body']}")
        if parts:
            ch.notes, ch.notes_source = "\n\n".join(parts), "releases de GitHub"
    log.info("cambios %s: %d commits, %d archivos", dot.id, ch.total, ch.files)
    return ch
