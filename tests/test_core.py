"""Tests unitarios rápidos y sin red (los ejecuta GitHub Actions).

Las pruebas de extremo a extremo con repos reales están en tests/e2e.py (manuales).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Sandbox(unittest.TestCase):
    """Cada test corre con HOME y XDG_* en un directorio temporal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dotdeck-test-"))
        self.h = self.tmp / "home"
        self.h.mkdir()
        self._env = {k: os.environ.get(k) for k in
                     ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}
        os.environ.update(HOME=str(self.h), XDG_CONFIG_HOME=str(self.h / ".config"),
                          XDG_DATA_HOME=str(self.h / ".local/share"), XDG_STATE_HOME=str(self.h / ".local/state"),
                          XDG_CACHE_HOME=str(self.h / ".cache"))

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.h / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class CatalogTests(Sandbox):
    def test_builtin_catalog_is_valid(self):
        from dotdeck import catalog
        dots, errors = catalog.load_all()
        self.assertEqual(errors, [])
        self.assertIn("hyde", dots)
        self.assertGreaterEqual(len(dots), 10)

    def test_documented_format_is_valid(self):
        from dotdeck import catalog
        d = catalog.load_file(ROOT / "docs/dot-format.toml", "user")
        self.assertEqual(d.id, "mi-dot")

    def test_toml_roundtrip(self):
        from dotdeck import catalog, tomlw
        dots, _ = catalog.load_all()
        for d in dots.values():
            again = catalog.from_dict(tomllib.loads(tomlw.dumps(d.to_dict())))
            self.assertEqual(again.to_dict(), d.to_dict(), d.id)

    def test_rejects_dangerous_paths(self):
        from dotdeck import catalog
        base = {"id": "x", "repo": "https://github.com/a/b",
                "install": {"method": "files", "links": [{"src": "a", "dest": "~/.config"}]}}
        with self.assertRaises(catalog.DotError):
            catalog.from_dict(base)
        base["install"]["links"] = [{"src": "../etc", "dest": "~/.config/x"}]
        with self.assertRaises(catalog.DotError):
            catalog.from_dict(base)
        with self.assertRaises(catalog.DotError):
            catalog.from_dict({"id": "x", "repo": "https://github.com/a/b",
                               "install": {"method": "script", "command": "x", "touches": ["~"]}})

    def test_user_dot_overrides_initial(self):
        from dotdeck import catalog
        dots, _ = catalog.load_all()
        d = dots["athena-eww"]
        d.name = "Mi Athena"
        catalog.save_user_dot(d)
        again, _ = catalog.load_all()
        self.assertEqual(again["athena-eww"].name, "Mi Athena")
        self.assertEqual(again["athena-eww"].origin, "user")


class BackupTests(Sandbox):
    def test_backup_restore_is_exact(self):
        from dotdeck import backup, fsutil
        cfg = self.write(".config/kitty/kitty.conf", "font_size 11\n")
        os.symlink("kitty.conf", cfg.parent / "link.conf")
        missing = self.h / ".config/eww"
        before = fsutil.digest_tree(cfg.parent)
        b = backup.create("t", "install", [cfg.parent, missing])
        self.assertTrue(b.complete)
        shutil.rmtree(cfg.parent)
        self.write(".config/kitty/otro.conf", "x")
        self.write(".config/eww/eww.yuck", "(defwindow)")
        backup.restore(b.id)
        self.assertEqual(fsutil.digest_tree(cfg.parent), before)
        self.assertFalse(missing.exists(), "lo que no existía debe desaparecer al restaurar")

    def test_insufficient_space_touches_nothing(self):
        from dotdeck import backup
        p = self.write(".config/a/b", "x")
        old = backup.MARGIN
        backup.MARGIN = 1 << 62
        try:
            with self.assertRaises(backup.BackupError):
                backup.create("t", "install", [p.parent])
        finally:
            backup.MARGIN = old
        self.assertTrue(p.exists())


class SnapshotTests(Sandbox):
    def test_diff_detects_created_and_modified(self):
        from dotdeck import engine
        rc = self.write(".bashrc", "a\n")
        (self.h / ".config").mkdir(exist_ok=True)
        before = engine.snapshot([self.h / ".config/hypr"])
        self.write(".config/newshell/x.conf", "1")
        time.sleep(0.01)
        rc.write_text("a\nb\n")
        after = engine.snapshot([self.h / ".config/hypr"])
        created, modified, _ = engine.diff_snapshots(before, after)
        self.assertIn(str(self.h / ".config/newshell"), created)
        self.assertIn(str(rc), modified)


class ReadmeTests(unittest.TestCase):
    MD = """# Demo
<div align="center">

**A tiny shell for Hyprland, built for testing.**

</div>

![shot](assets/shot.png) ![badge](https://img.shields.io/badge/x-y-z)

https://github.com/user-attachments/assets/0840f496-575c-4ca6-83a8-87bb01a85c5f

## Features
- **Bar** — a bar with workspaces and a clock.
- **Launcher** — fuzzy app launcher with icons.
- **Lock** — a lock screen that checks your password.

## Install
| Flag | What |
|---|---|
| `-y` | yes |
"""

    def test_parse_and_summary(self):
        from dotdeck.ui import readme
        blocks = readme.parse(self.MD)
        self.assertEqual(readme.image_list(blocks), ["assets/shot.png"])  # sin badges
        self.assertEqual(len(readme.media_list(blocks)), 1)              # el vídeo incrustado
        sm = readme.summarize(blocks)
        self.assertIn("tiny shell", readme.plain(sm["intro"][0]))
        self.assertEqual([t for t, _ in sm["features"]], ["Bar", "Launcher", "Lock"])
        self.assertEqual([t for t, _, _ in sm["sections"]], ["Features", "Install"])

    def test_resolve_rel(self):
        from dotdeck.onboard import resolve_rel
        self.assertEqual(resolve_rel(".github", "../assets/a.png"), "assets/a.png")
        self.assertEqual(resolve_rel(".", "./shot.png?raw=true"), "shot.png")


class OnboardTests(unittest.TestCase):
    def test_find_links(self):
        from dotdeck.onboard import _find_links
        links = _find_links(["dots/.config/hypr/hyprland.lua", "dots/.config/kitty/kitty.conf",
                             "dots/.zshrc", ".github/workflows/x.yml"])
        got = {(l.src, l.dest, l.optional) for l in links}
        self.assertIn(("dots/.config/hypr", "~/.config/hypr", False), got)
        self.assertIn(("dots/.zshrc", "~/.zshrc", True), got)

    def test_touches_from_script(self):
        from dotdeck.onboard import _touches_from_script
        t = _touches_from_script('cp -r x "$HOME/.config/waybar"\nmkdir -p ~/.local/share/fonts\n'
                                 'echo >> "$HOME/.bashrc"\ncp a ${XDG_CONFIG_HOME:-$HOME/.config}/rofi')
        self.assertTrue({"~/.config/waybar", "~/.local/share/fonts", "~/.bashrc", "~/.config/rofi"} <= t)

    def test_normalize_url(self):
        from dotdeck.gitrepo import normalize_url
        self.assertEqual(normalize_url("https://github.com/snes19xx/surface-dots/tree/main#Installation"),
                         "https://github.com/snes19xx/surface-dots")
        self.assertEqual(normalize_url("https://github.com/a/b?tab=readme-ov-file"), "https://github.com/a/b")


class ChangesTests(unittest.TestCase):
    def test_conventional_commit_groups(self):
        from dotdeck.changes import CC, _group
        m = CC.match("feat(waybar)!: new layout")
        self.assertEqual((m.group("type"), m.group("scope")), ("feat", "waybar"))
        self.assertEqual(_group("feat", True), "breaking")
        self.assertEqual(_group("fix", False), "fix")
        self.assertEqual(_group("ci", False), "chore")
        self.assertEqual(_group("whatever", False), "other")


@unittest.skipUnless(sys.platform.startswith("linux"), "usa /proc")
class WatchdogTests(unittest.TestCase):
    def test_detects_daemon_holding_pipe(self):
        from dotdeck import watchdog
        script = 'x=$(sleep 30 & echo hi); echo "$x"'
        p = subprocess.Popen(["bash", "-c", script], stdout=subprocess.DEVNULL)
        try:
            stall = None
            for _ in range(30):
                time.sleep(0.2)
                stall = watchdog.check(p.pid)
                if stall:
                    break
            self.assertIsNotNone(stall)
            self.assertTrue(any("sleep" in c for _, c in stall.blockers))
            watchdog.unblock(stall)
            self.assertEqual(p.wait(timeout=10), 0)
        finally:
            p.kill()

    def test_no_false_positive_on_slow_child(self):
        from dotdeck import watchdog
        p = subprocess.Popen(["bash", "-c", "x=$(sleep 2; echo hi)"])
        try:
            time.sleep(1)
            self.assertIsNone(watchdog.check(p.pid))
        finally:
            p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
