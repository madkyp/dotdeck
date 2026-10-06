#!/usr/bin/env python3
"""Verificación en seco del catálogo (todos los dots), sin instalar nada.

Comprueba: definición válida, repo accesible, versión remota según track,
README legible, capturas existentes, y que las rutas/instalador de la
definición existen de verdad en el repo.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

base = Path(tempfile.mkdtemp(prefix="dotdeck-verify-", dir=os.environ.get("DOTDECK_TEST_TMP")))
for k, sub in (("HOME", ""), ("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
               ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
    os.environ[k] = str(base / sub)

from dotdeck import catalog, gitrepo  # noqa: E402
from dotdeck.ui import readme  # noqa: E402

dots, errors = catalog.load_all()
bad = 0
for e in errors:
    print("✘ definición inválida:", e)
    bad += 1
for d in sorted(dots.values(), key=lambda d: d.id):
    probs = []
    try:
        rv = gitrepo.remote_version(d)
        ver = rv["label"]
    except gitrepo.GitError as e:
        probs.append(f"repo/versión: {e}")
        ver = "?"
    try:
        clone = gitrepo.preview_clone(d)
        files = set(gitrepo.ls_files(clone))
        dirs = {str(Path(f).parent) for f in files} | {p for f in files for p in
                                                       ["/".join(f.split("/")[:i]) for i in range(1, f.count("/") + 1)]}
        txt = gitrepo.read_file(clone, d.readme).decode("utf-8", "replace") if d.readme else ""
        blocks = readme.parse(txt) if txt else []
        for l in d.links:
            if l.src not in files and l.src not in dirs:
                probs.append(f"link.src no existe en el repo: {l.src}")
        if d.method == "script":
            cmd, cwd = d.command, ""
            m = __import__("re").match(r"^\s*cd\s+(\S+)\s*&&\s*(.*)$", cmd)
            if m:  # «cd Scripts && ./install.sh»
                cwd, cmd = m.group(1).strip("/") + "/", m.group(2)
            first = [t for t in cmd.split() if not t.startswith("-")]
            script = next((cwd + t.removeprefix("./") for t in first if "/" in t or t.endswith(".sh")), None)
            if script and script not in files:
                probs.append(f"instalador no encontrado: {script}")
        if d.cover and not d.cover.startswith("http") and not Path(d.cover_source()).is_file() \
                and d.cover not in files:
            probs.append(f"portada no encontrada: {d.cover}")
        for img in d.images:
            if not img.startswith("http") and img not in files:
                probs.append(f"captura no encontrada: {img}")
        extra = f"README {len(blocks)} bloques, {len(readme.image_list(blocks))} imágenes"
    except gitrepo.GitError as e:
        probs.append(f"preview: {e}")
        extra = ""
    mark = "✘" if probs else "✔"
    bad += bool(probs)
    print(f"{mark} {d.id:24} {d.method:6} track={d.track:7} última={ver:24} {extra}")
    for p in probs:
        print("     -", p)
print(f"\n{len(dots)} dots verificados, {bad} con problemas")
sys.exit(1 if bad else 0)
