# Maintainer: madkyp
pkgname=dotdeck
pkgver=0.1.0
pkgrel=16
pkgdesc="Gestor de dots de Hyprland: previsualiza, instala con backup, actualiza, revierte y limpia huérfanos"
arch=('any')
url="https://github.com/madkyp/dotdeck"
license=('MIT')
depends=('python>=3.11' 'python-gobject' 'gtk4' 'libadwaita' 'python-markdown' 'git' 'libnotify')
optdepends=('kitty: terminal para instaladores y sudo (o ghostty/alacritty/foot)'
            'yay: dependencias del AUR'
            'paru: dependencias del AUR'
            'glycin: miniaturas webp/avif')
source=()
sha256sums=()

package() {
  cd "$startdir"
  install -d "$pkgdir/usr/lib/dotdeck"
  cp -r dotdeck dots.d "$pkgdir/usr/lib/dotdeck/"
  find "$pkgdir/usr/lib/dotdeck" -name '__pycache__' -prune -exec rm -rf {} +
  # Legible por todos: un archivo 600 en el repo (p.ej. una portada) sería ilegible para el usuario
  chmod -R u=rwX,go=rX "$pkgdir/usr/lib/dotdeck"
  install -Dm755 data/dotdeck.sh "$pkgdir/usr/bin/dotdeck"
  install -Dm644 data/dotdeck.desktop "$pkgdir/usr/share/applications/io.github.madkyp.DotDeck.desktop"
  install -Dm644 data/io.github.madkyp.DotDeck.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.madkyp.DotDeck.svg"
  install -Dm644 data/systemd/dotdeck-update-check.service "$pkgdir/usr/lib/systemd/user/dotdeck-update-check.service"
  install -Dm644 data/systemd/dotdeck-update-check.timer "$pkgdir/usr/lib/systemd/user/dotdeck-update-check.timer"
  install -Dm644 docs/dot-format.toml "$pkgdir/usr/share/doc/dotdeck/dot-format.toml"
  install -Dm644 README.md "$pkgdir/usr/share/doc/dotdeck/README.md"
}
