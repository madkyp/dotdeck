"""Buscador de dots en GitHub (pestaña «Descubrir»)."""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import gitrepo
from .config import log

PRESETS = [  # (clave, etiqueta, filtro de temas)
    ("popular", "Todos", "topic:hyprland"),
    ("rices", "Rices completos", "topic:hyprland topic:dotfiles"),
    ("quickshell", "Quickshell", "topic:hyprland topic:quickshell"),
    ("waybar", "Waybar", "topic:hyprland topic:waybar"),
]
SORTS = [("stars", "Más estrellas"), ("updated", "Actualizados hace poco"), ("forks", "Más forks")]
STARS = [(0, "Cualquiera"), (100, "≥ 100 ★"), (500, "≥ 500 ★"), (1000, "≥ 1k ★"), (5000, "≥ 5k ★")]
ACTIVITY = [(0, "Cualquier fecha"), (30, "Último mes"), (180, "Últimos 6 meses"), (365, "Último año")]
_cache: dict[str, tuple[float, "Page"]] = {}
TTL = 600


@dataclass
class Repo:
    full_name: str
    url: str
    description: str
    stars: int
    pushed: str
    topics: list[str]
    license: str
    owner_avatar: str

    @property
    def preview_image(self) -> str:
        """Imagen social del repo (la que el autor puso, o la tarjeta generada por GitHub)."""
        return f"https://opengraph.githubassets.com/1/{self.full_name}"


@dataclass
class Page:
    total: int
    items: list[Repo] = field(default_factory=list)
    has_more: bool = False


def build_query(text: str, preset: str, min_stars: int = 0, active_days: int = 0) -> str:
    p = next((x for x in PRESETS if x[0] == preset), PRESETS[0])
    q = f"{text.strip()} {p[2]} archived:false fork:false".strip()
    if min_stars:
        q += f" stars:>={min_stars}"
    if active_days:
        q += f" pushed:>={(date.today() - timedelta(days=active_days)).isoformat()}"
    return q


def search(text: str = "", preset: str = "popular", page: int = 1, per_page: int = 30,
           sort: str = "stars", min_stars: int = 0, active_days: int = 0) -> Page:
    q = build_query(text, preset, min_stars, active_days)
    key = f"{q}|{sort}|{page}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    path = ("search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": sort, "order": "desc", "per_page": per_page, "page": page}))
    d = gitrepo.github_api(path, timeout=25)
    if d is None:
        raise gitrepo.GitError("la búsqueda de GitHub no respondió")
    items = [Repo(full_name=r["full_name"], url=r["html_url"], description=r.get("description") or "",
                  stars=r.get("stargazers_count", 0), pushed=(r.get("pushed_at") or "")[:10],
                  topics=r.get("topics") or [], license=((r.get("license") or {}).get("spdx_id") or ""),
                  owner_avatar=(r.get("owner") or {}).get("avatar_url", ""))
             for r in d.get("items", [])]
    res = Page(total=d.get("total_count", 0), items=items,
               has_more=page * per_page < min(d.get("total_count", 0), 1000))
    _cache[key] = (time.time(), res)
    log.info("búsqueda «%s» (%s): %d resultados", q, sort, res.total)
    return res


# ------------------------------------------------------------------ tendencias
# GitHub ya no expone cuándo se dio cada estrella; OSS Insight publica el historial diario
# de estrellas de cualquier repo público (sus totales difieren de los de GitHub, pero las
# diferencias entre fechas sí sirven para medir lo ganado en un periodo).
PERIODS = [("all", "Siempre (total)", 0), ("day", "Hoy", 1), ("week", "Esta semana", 7),
           ("month", "Este mes", 30), ("year", "Este año", 365)]
HIST_TTL = 3 * 3600


def _history(full_name: str) -> list[tuple[str, int]]:
    import json
    import urllib.request
    from . import config
    cf = config.cache_dir() / "stars" / (full_name.replace("/", "__") + ".json")
    if cf.exists() and time.time() - cf.stat().st_mtime < HIST_TTL:
        return [tuple(x) for x in json.loads(cf.read_text())]
    since = (date.today() - timedelta(days=400)).isoformat()
    url = f"https://api.ossinsight.io/v1/repos/{full_name}/stargazers/history/?per=day&from={since}"
    req = urllib.request.Request(url, headers={"User-Agent": "dotdeck"})
    with urllib.request.urlopen(req, timeout=25) as r:
        rows = json.load(r).get("data", {}).get("rows", [])
    hist = [(x["date"], int(float(x["stargazers"]))) for x in rows]
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(hist))
    return hist


def gained(hist: list[tuple[str, int]], days: int) -> int:
    if not hist:
        return 0
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    before = [v for d, v in hist if d < cutoff] if days > 1 else [v for d, v in hist if d < date.today().isoformat()]
    return max(0, hist[-1][1] - (before[-1] if before else 0))


def trending(text: str, preset: str, period: str, min_stars: int = 0) -> list[tuple[Repo, int]]:
    """Repos ordenados por estrellas ganadas en el periodo (día/semana/mes/año)."""
    from concurrent.futures import ThreadPoolExecutor
    days = next(d for k, _, d in PERIODS if k == period)
    cands: dict[str, Repo] = {}
    floor = max(min_stars, 20)
    for sort, active in (("stars", 0), ("updated", max(days, 7))):
        for r in search(text, preset, 1, per_page=40, sort=sort, min_stars=floor, active_days=active).items:
            cands.setdefault(r.full_name, r)
    out: list[tuple[Repo, int]] = []

    def one(r: Repo):
        try:
            return r, gained(_history(r.full_name), days)
        except Exception as e:  # noqa: BLE001
            log.info("historial de estrellas de %s no disponible: %s", r.full_name, e)
            return r, -1

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = [x for x in ex.map(one, list(cands.values())[:70]) if x[1] >= 0]
    out.sort(key=lambda x: (-x[1], -x[0].stars))
    log.info("tendencias %s: %d repos con historial", period, len(out))
    return out
