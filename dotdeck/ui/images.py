"""Carga asíncrona de imágenes (del repo o de URL) con caché en disco."""
from __future__ import annotations

import hashlib
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import config, gitrepo  # noqa: E402
from ..config import log  # noqa: E402

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="img")
_lock = threading.Lock()
_mem: dict[str, Gdk.Texture] = {}
MAX_BYTES = 150 * 1024 * 1024  # los vídeos de preview pueden pesar decenas de MB


def _cache_file(key: str) -> Path:
    return config.cache_dir() / "img" / hashlib.sha1(key.encode()).hexdigest()


def is_badge(src: str) -> bool:
    return bool(re.search(r"shields\.io|/badge|badgen|komarev|visitor|star-history|contrib\.rocks|"
                          r"github-readme-stats|readme-typing|capsule-render|hits\.|\.svg(\?|$)", src, re.I))


def github_raw(repo: str, rel: str) -> str | None:
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)$", gitrepo.normalize_url(repo))
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/{rel}" if m else None


# Las capturas que se arrastran al README se guardan como github.com/user-attachments/assets/<uuid>.
# Muchas dan 404 sin sesión: GitHub solo las sirve con un enlace firmado y temporal
# (private-user-images…?jwt=…) que aparece en el README renderizado por su API.
UA_RE = re.compile(r"https://github\.com/user-attachments/assets/([0-9a-f-]{20,})")
SIGNED: dict[str, str] = {}
ASSET_REPO: dict[str, str] = {}


def register_assets(full_name: str, text: str) -> None:
    """Recuerda de qué repo es cada asset del README (para poder pedir su enlace firmado)."""
    for m in UA_RE.finditer(text):
        ASSET_REPO.setdefault(m.group(1), full_name)


def _refresh_signed(full_name: str) -> None:
    tok = gitrepo._token()
    headers = {"Accept": "application/vnd.github.html+json", "User-Agent": "dotdeck"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(f"https://api.github.com/repos/{full_name}/readme", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    for m in re.finditer(r"https://private-user-images\.githubusercontent\.com/[^\"'\s<>)]+", html):
        url = m.group(0).replace("&amp;", "&")
        u = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url)
        if u:
            SIGNED[u.group(1)] = url
    log.debug("enlaces firmados de %s: %d", full_name, len(SIGNED))


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dotdeck"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(MAX_BYTES)


def _fetch_attachment(src: str, uuid: str) -> bytes:
    try:
        return _download(src)
    except urllib.error.HTTPError as e:
        if e.code not in (403, 404) or uuid not in ASSET_REPO:
            raise
    for attempt in range(2):  # el enlace firmado caduca a los ~5 min: se renueva una vez
        if attempt or uuid not in SIGNED:
            _refresh_signed(ASSET_REPO[uuid])
        if uuid not in SIGNED:
            break
        try:
            return _download(SIGNED[uuid])
        except urllib.error.HTTPError:
            SIGNED.pop(uuid, None)
    raise FileNotFoundError(f"asset {uuid} no disponible")


def fetch_bytes(src: str, clone: Path | None) -> bytes:
    """``src``: URL absoluta o ruta relativa al repo (se lee del clon parcial)."""
    if src.startswith("/"):
        return Path(src).read_bytes()[:MAX_BYTES]  # portada local junto a la definición
    key = src if src.startswith(("http://", "https://")) else f"{clone}:{src}"
    cf = _cache_file(key)
    if cf.exists():
        return cf.read_bytes()
    if (ua := UA_RE.match(src)):
        data = _fetch_attachment(src, ua.group(1))
    elif src.startswith(("http://", "https://")):
        data = _download(src)
    else:
        if clone is None:
            raise FileNotFoundError(src)
        data = gitrepo.read_file(clone, src, MAX_BYTES)
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_bytes(data)
    return data


VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".m4v")


def is_video_src(src: str) -> bool:
    return src.split("?")[0].lower().endswith(VIDEO_EXT)


def is_video_bytes(b: bytes) -> bool:
    return b[4:8] == b"ftyp" or b[:4] == b"\x1aE\xdf\xa3" or (b[:4] == b"RIFF" and b[8:12] == b"AVI ")


def cached_path(src: str, clone: Path | None) -> Path:
    if src.startswith("/"):
        return Path(src)
    return _cache_file(src if src.startswith(("http://", "https://")) else f"{clone}:{src}")


def texture_from_bytes(data: bytes) -> Gdk.Texture:
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))


def load_async(src: str, clone: Path | None, callback) -> None:
    """callback(texture | None, error | None) en el hilo principal."""
    key = src if src.startswith("http") else f"{clone}:{src}"
    with _lock:
        tex = _mem.get(key)
    if tex is not None:
        GLib.idle_add(callback, tex, None)
        return

    def work():
        try:
            data = fetch_bytes(src, clone)
            t = texture_from_bytes(data)
            with _lock:
                _mem[key] = t
            GLib.idle_add(callback, t, None)
        except Exception as e:  # noqa: BLE001
            log.debug("imagen %s no cargada: %s", src, e)
            GLib.idle_add(callback, None, e)

    _pool.submit(work)


def load_media_async(src: str, clone: Path | None, callback) -> None:
    """callback(kind, obj): ("image", Gdk.Texture) | ("video", Path) | (None, error)."""
    def work():
        try:
            data = fetch_bytes(src, clone)
            if is_video_bytes(data[:16]):
                GLib.idle_add(callback, "video", cached_path(src, clone))
            else:
                GLib.idle_add(callback, "image", texture_from_bytes(data))
        except Exception as e:  # noqa: BLE001
            log.debug("medio %s no cargado: %s", src, e)
            GLib.idle_add(callback, None, e)

    _pool.submit(work)


class AsyncPicture(Gtk.Box):
    """Imagen que se carga en segundo plano, con alto máximo y relación de aspecto."""

    def __init__(self, src: str, clone: Path | None, max_w: int = 760, max_h: int = 460,
                 css: str | None = "md-image", on_fail=None, allow_video: bool = False,
                 autoplay: bool = False):
        super().__init__(halign=Gtk.Align.START)
        self.max_w, self.max_h, self.on_fail = max_w, max_h, on_fail
        self.autoplay = autoplay
        self.spinner = Gtk.Spinner(spinning=True, margin_top=12, margin_bottom=12)
        self.append(self.spinner)
        if css:
            self.add_css_class(css)
        if allow_video:
            load_media_async(src, clone, self._media)
        else:
            load_async(src, clone, self._loaded)

    def _media(self, kind, obj):
        if kind == "video":
            self.remove(self.spinner)
            self._video(obj)
            return False
        return self._loaded(obj if kind == "image" else None, None)

    def _video(self, path: Path):
        """Vídeo de preview: en la galería se reproduce solo, en bucle y sin sonido."""
        media = Gtk.MediaFile.new_for_filename(str(path))
        media.set_loop(True)
        media.set_muted(self.autoplay)
        video = Gtk.Video(media_stream=media, autoplay=self.autoplay, hexpand=True)
        video.set_size_request(-1, self.max_h)
        video.add_css_class("rounded-img")
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.append(video)
        if self.autoplay:
            media.play()

    def _loaded(self, tex, err):
        self.remove(self.spinner)
        if tex is None:
            if self.on_fail:
                self.on_fail(self)
            else:
                self.set_visible(False)
            return False
        w, h = tex.get_width(), tex.get_height()
        scale = min(1.0, self.max_w / max(w, 1), self.max_h / max(h, 1))
        pic = Gtk.Picture.new_for_paintable(tex)
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_can_shrink(True)
        # Solo se fija el alto: el ancho puede encoger con la ventana (CONTAIN mantiene el aspecto).
        pic.set_size_request(-1, max(1, int(h * scale)))
        if w * scale < self.max_w:
            pic.set_halign(Gtk.Align.START)
        pic.add_css_class("rounded-img")
        self.append(pic)
        return False
