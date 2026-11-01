# Maintainer: Asif Arbaj <asifarbaj@gmail.com>
pkgname=reel
pkgver=r0
pkgrel=1
pkgdesc="Video library for mpv's start screen: ~/Videos with TMDB details, finished torrents sorted in"
arch=('any')
url="https://github.com/anisthdev/reel"
license=('unknown')
# `uv run --script` scripts: uv brings guessit itself
depends=('uv' 'ffmpeg' 'ffmpegthumbnailer' 'libnotify')
optdepends=(
    'transmission-cli: reel-sort, run by transmission-daemon when a torrent finishes'
    'mpv: Play from the notification'
    'yazi: Open folder from the notification'
)
makedepends=('git')
source=("git+$url.git")
sha256sums=('SKIP')

pkgver() {
    cd "$pkgname"
    printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}

package() {
    cd "$pkgname"
    # Together: sort.py imports scan from beside itself
    install -Dm755 -t "$pkgdir/usr/lib/reel" scan.py sort.py
    install -d "$pkgdir/usr/bin"
    ln -s ../lib/reel/scan.py "$pkgdir/usr/bin/reel-scan"
    ln -s ../lib/reel/sort.py "$pkgdir/usr/bin/reel-sort"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
