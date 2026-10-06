"""Operaciones git: clones de previsualización, checkouts de despliegue y versiones."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import config, fsutil
from .catalog import Dot
from .config import log

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"}


class GitError(Exception):
    pass


def git(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    cmd = ["git", *args]
    log.debug("git %s (cwd=%s)", " ".join(args), cwd)
    try:
        r = subprocess.run(cmd, cwd=cwd, env=GIT_ENV, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} ha excedido {timeout}s") from None
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        raise GitError(f"git {args[0]} falló: {err[-1] if err else r.returncode}")
    return r.stdout


def normalize_url(url: str) -> str:
    """Acepta URLs de navegador (…/tree/main#Installation, ?tab=readme…) y las limpia."""
    url = url.strip()
    url = re.sub(r"[?#].*$", "", url)
    m = re.match(r"^(https?://(?:www\.)?github\.com/[^/]+/[^/]+?)(?:\.git)?(?:/(?:tree|blob)/.*)?/?$", url)
    if m:
        return m.group(1).replace("www.github.com", "github.com")
    return url.rstrip("/")


def branch_from_url(url: str) -> str:
    m = re.search(r"github\.com/[^/]+/[^/]+/tree/([^/?#]+)", url)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- remoto
def ls_remote(repo: str) -> dict[str, str]:
    out = git("ls-remote", "--symref", repo, timeout=60)
    refs: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("ref: "):
            target, name = line[5:].split("\t")
            refs["SYMREF:" + name] = target
            continue
        sha, name = line.split("\t")
        refs[name] = sha
    return refs


def default_branch(refs: dict[str, str]) -> str:
    t = refs.get("SYMREF:HEAD", "refs/heads/main")
    return t.removeprefix("refs/heads/")


def _vkey(tag: str):
    nums = re.findall(r"\d+", tag)
    return [int(n) for n in nums] or [0]


def latest_tag(refs: dict[str, str]) -> tuple[str, str] | None:
    tags = {}
    for name, sha in refs.items():
        if name.startswith("refs/tags/"):
            t = name.removeprefix("refs/tags/")
            if t.endswith("^{}"):
                tags[t[:-3]] = sha  # tag anotado: el commit real
            else:
                tags.setdefault(t, sha)
    vt = [t for t in tags if re.search(r"\d", t)]
    if not vt:
        return None
    best = max(vt, key=_vkey)
    return best, tags[best]


_TOKEN: list = []


def _token() -> str | None:
    """Token de GitHub: $GITHUB_TOKEN/$GH_TOKEN o la sesión de `gh` (sube el límite de 60 a 5000/h)."""
    if not _TOKEN:
        tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not tok and shutil.which("gh"):
            try:
                r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
                tok = r.stdout.strip() if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                tok = None
        _TOKEN.append(tok)
    return _TOKEN[0]


def github_api(path: str, timeout: int = 20):
    url = "https://api.github.com/" + path.lstrip("/")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dotdeck"}
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (403, 429):
            raise GitError("límite de consultas a GitHub agotado (inicia sesión con `gh auth login` "
                           "o define GITHUB_TOKEN)") from None
        raise GitError(f"GitHub API {e.code} en {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GitError(f"sin acceso a GitHub ({e})") from None


def latest_release(dot: Dot) -> str | None:
    orr = dot.owner_repo
    if not orr:
        return None
    try:
        d = github_api(f"repos/{orr[0]}/{orr[1]}/releases/latest")
    except GitError as e:
        log.info("releases de %s no disponibles (%s); uso tags", dot.id, e)
        return None
    return d.get("tag_name") if d else None


def remote_version(dot: Dot) -> dict:
    """Última versión disponible según track.mode → {ref, commit, label}."""
    refs = ls_remote(dot.repo)
    branch = dot.branch or default_branch(refs)
    head = refs.get(f"refs/heads/{branch}")
    if dot.track in ("release", "tag"):
        tag = latest_release(dot) if dot.track == "release" else None
        tags = latest_tag(refs)
        if tag is None and tags:
            tag = tags[0]
        if tag:
            sha = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
            return {"ref": tag, "commit": sha or "", "label": tag, "branch": branch}
        log.info("%s no tiene tags/releases; sigo la rama %s", dot.id, branch)
    if not head:
        raise GitError(f"la rama '{branch}' no existe en {dot.repo}")
    return {"ref": branch, "commit": head, "label": f"{branch}@{head[:7]}", "branch": branch}


# ---------------------------------------------------------------- preview
def preview_clone(dot_or_url, refresh: bool = False) -> Path:
    """Clon superficial y sin blobs: árbol completo, ficheros bajo demanda."""
    repo = dot_or_url.repo if isinstance(dot_or_url, Dot) else dot_or_url
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", normalize_url(repo).split("://")[-1])
    dest = config.preview_dir() / key
    if (dest / ".git").exists() or (dest / "HEAD").exists():
        if refresh:
            try:
                git("fetch", "--depth", "1", "--filter=blob:none", "origin", "HEAD", cwd=dest, timeout=120)
                git("update-ref", "HEAD", "FETCH_HEAD", cwd=dest)
            except GitError as e:
                log.warning("no se pudo refrescar la previsualización de %s: %s", repo, e)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    fsutil.remove_any(tmp)
    git("clone", "--bare", "--depth", "1", "--filter=blob:none", repo, str(tmp), timeout=300)
    tmp.rename(dest)
    return dest


def ls_files(clone: Path) -> list[str]:
    return [l for l in git("ls-tree", "-r", "--name-only", "HEAD", cwd=clone).splitlines() if l]


def ls_files_with_mode(clone: Path) -> dict[str, str]:
    out = {}
    for line in git("ls-tree", "-r", "HEAD", cwd=clone).splitlines():
        meta, name = line.split("\t", 1)
        out[name] = meta.split()[0]
    return out


def read_file(clone: Path, rel: str, max_bytes: int = 30 * 1024 * 1024) -> bytes:
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=clone, env=GIT_ENV,
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise GitError(f"no se pudo leer {rel}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout[:max_bytes]


def head_commit(clone: Path) -> str:
    return git("rev-parse", "HEAD", cwd=clone).strip()


# ---------------------------------------------------------------- deploy
def deploy_path(dot: Dot) -> Path:
    return config.deploy_dir() / dot.id


def sparse_patterns(dot: Dot, links) -> list[str] | None:
    """En modo files solo hacen falta las rutas enlazadas (repos de cientos de MB)."""
    if dot.method != "files":
        return None
    pats = []
    for l in links:
        src = l.src.strip("/")
        pats.append(f"/{src}")
        pats.append(f"/{src}/")
    return pats


def checkout(dot: Dot, ref: str, dest: Path, links=None) -> str:
    """Crea un checkout superficial de ``ref`` en ``dest`` (que no debe existir)."""
    if dest.exists():
        raise GitError(f"{dest} ya existe")
    dest.parent.mkdir(parents=True, exist_ok=True)
    pats = sparse_patterns(dot, links if links is not None else dot.links)
    args = ["clone", "--depth", "1", "--branch", ref, "--no-checkout"]
    if pats:
        args.insert(1, "--filter=blob:none")
    git(*args, dot.repo, str(dest), timeout=1800)
    if pats:
        git("sparse-checkout", "set", "--no-cone", *pats, cwd=dest)
    git("checkout", "--quiet", ref, cwd=dest, timeout=1800)
    return head_commit(dest)


def local_changes(dest: Path) -> list[str]:
    """Cambios manuales del usuario sobre los ficheros del dot (vía symlink)."""
    out = git("status", "--porcelain", "--untracked-files=all", cwd=dest)
    return [l[3:] for l in out.splitlines() if l.strip()]


def update_checkout(dest: Path, ref: str, keep_local: bool) -> tuple[str, list[str]]:
    """Avanza el checkout a ``ref``. Si keep_local, reaplica los cambios del usuario.

    Devuelve (commit nuevo, ficheros con conflicto).
    """
    git("fetch", "--depth", "1", "origin", ref, cwd=dest, timeout=1800)
    stashed = False
    if keep_local and local_changes(dest):
        git("stash", "push", "--include-untracked", "-m", "dotdeck-update", cwd=dest)
        stashed = True
    elif local_changes(dest):
        git("reset", "--hard", "--quiet", cwd=dest)
        git("clean", "-fdq", cwd=dest)
    git("checkout", "--quiet", "--force", "FETCH_HEAD", cwd=dest, timeout=1800)
    conflicts: list[str] = []
    if stashed:
        try:
            git("stash", "pop", cwd=dest)
        except GitError:
            out = git("diff", "--name-only", "--diff-filter=U", cwd=dest)
            conflicts = [l for l in out.splitlines() if l]
            log.warning("conflictos al reaplicar cambios locales: %s", conflicts)
    return head_commit(dest), conflicts


def update_external(path: Path, ref: str) -> str:
    """Actualiza un clon del usuario como indica HyDE y similares:
    fetch superficial + reset --hard (sin «git clean»: no se borran archivos sin seguimiento)."""
    git("fetch", "--update-shallow", "--depth", "1", "origin", ref, cwd=path, timeout=1800)
    git("reset", "--hard", "--quiet", "FETCH_HEAD", cwd=path, timeout=600)
    return head_commit(path)
