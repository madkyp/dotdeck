"""Aplicación GTK4 / libadwaita."""
from __future__ import annotations

import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .. import config  # noqa: E402

CSS = """
:root { --accent-bg-color: @ACCENT@; --accent-color: @ACCENT@; --accent-fg-color: #11111b; }
@define-color accent_bg_color @ACCENT@;
@define-color accent_color @ACCENT@;
@define-color accent_fg_color #11111b;

window.dotdeck.translucent { background-color: alpha(@window_bg_color, 0.90); }
window.dotdeck.translucent headerbar { background-color: transparent; }

.dot-card {
  border-radius: 20px; padding: 0;
  background-color: alpha(@card_bg_color, 0.92);
  border: 1px solid alpha(@borders, 0.5);
  transition: border-color 180ms ease, box-shadow 220ms ease, transform 180ms ease;
}
.dot-card:hover { border-color: alpha(@accent_bg_color, 0.75); box-shadow: 0 8px 28px alpha(black, 0.35); }
.dot-card .thumb { border-radius: 20px 20px 0 0; }
.thumb-placeholder {
  border-radius: 20px 20px 0 0;
  background-image: linear-gradient(135deg, alpha(@accent_bg_color, 0.45), alpha(@accent_bg_color, 0.06));
}
.thumb-letter { font-size: 44pt; font-weight: 800; color: alpha(white, 0.85); }
.card-title { font-weight: 700; font-size: 13pt; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 8.5pt; font-weight: 700; }
.pill.installed { background-color: alpha(@success_color, 0.18); color: @success_color; }
.pill.update { background-color: alpha(@warning_color, 0.22); color: @warning_color; }
.pill.example { background-color: alpha(@accent_bg_color, 0.20); color: @accent_color; }
.pill.user { background-color: alpha(@purple_3, 0.25); color: @purple_1; }
.pill.neutral { background-color: alpha(@view_fg_color, 0.08); color: alpha(@view_fg_color, 0.75); }
.pill.danger { background-color: alpha(@error_color, 0.18); color: @error_color; }
.hero-title { font-size: 26pt; font-weight: 800; }
.hero { padding: 8px 4px 4px 4px; }
.readme-card { background-color: alpha(@card_bg_color, 0.7); border-radius: 18px; padding: 24px 28px; }
.code-block { background-color: alpha(black, 0.30); border-radius: 12px; padding: 10px 14px; }
.md-quote { border-left: 3px solid @accent_bg_color; padding-left: 12px; }
.rounded-img, .md-image picture { border-radius: 12px; }
.gallery { border-radius: 18px; background-color: alpha(black, 0.25); }
.warn-box { background-color: alpha(@warning_color, 0.12); border-radius: 14px; padding: 12px 16px; }
.warn-box label { color: @warning_color; }
.details-box { background-color: alpha(black, 0.2); border-radius: 10px; padding: 8px; }
.status-auto { color: @success_color; }
.status-review { color: @warning_color; }
.status-missing { color: @error_color; }
.status-manual { color: @accent_color; }
.about-card { background-color: alpha(@card_bg_color, 0.7); border-radius: 18px; padding: 22px 26px; }
.section-title { font-size: 15pt; font-weight: 800; }
.lead { font-size: 12pt; line-height: 1.4; }
.feature-tile {
  background-color: alpha(@card_bg_color, 0.75); border-radius: 12px; padding: 9px 12px;
  border: 1px solid alpha(@borders, 0.35); transition: border-color 180ms ease;
}
.feature-tile:hover { border-color: alpha(@accent_bg_color, 0.6); }
.feature-title { font-weight: 700; font-size: 10pt; color: @accent_color; }
.star-badge {
  background-color: alpha(#11111b, 0.78); color: #f9e2af; font-weight: 800; font-size: 11pt;
  border-radius: 999px; padding: 4px 12px; border: 1px solid alpha(#f9e2af, 0.45);
  box-shadow: 0 2px 10px alpha(black, 0.4);
}
.star-badge.trend { color: #a6e3a1; border-color: alpha(#a6e3a1, 0.5); }
.rank-badge {
  background-color: alpha(@accent_bg_color, 0.92); color: @accent_fg_color; font-weight: 800;
  border-radius: 999px; padding: 3px 10px; box-shadow: 0 2px 10px alpha(black, 0.4);
}
.filter-bar { padding: 6px 0 4px 0; }
"""


class DotDeckApp(Adw.Application):
    def __init__(self, page: str | None = None):
        super().__init__(application_id=config.APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.page = page
        self.css = Gtk.CssProvider()

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Iconos propios: algunos temas (p.ej. Tela) no dibujan bien list-add-symbolic.
        from pathlib import Path
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            str(Path(__file__).parent / "icons"))
        self.apply_style()

    def apply_style(self):
        s = config.load_settings()
        accent = s.get("accent") or "#89b4fa"
        self.css.load_from_string(CSS.replace("@ACCENT@", accent))
        for w in self.get_windows():
            if s.get("translucent", True):
                w.add_css_class("translucent")
            else:
                w.remove_css_class("translucent")

    def do_activate(self):
        win = self.props.active_window
        if not win:
            from .window import MainWindow
            win = MainWindow(self)
            self.apply_style()
        if self.page:
            win.show_page(self.page)
        win.present()


def run_app(page: str | None = None, debug: bool = False) -> int:
    config.setup_logging(debug)
    return DotDeckApp(page).run([sys.argv[0]])
