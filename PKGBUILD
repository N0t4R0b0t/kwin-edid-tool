# Maintainer: N0t4R0b0t
pkgname=kwin-edid-tool-git
pkgver=r1.0000000
pkgrel=1
pkgdesc="Add custom resolutions to a real display's EDID on Linux/KWin - a workaround for NVIDIA's DRM-KMS driver rejecting runtime-synthesized display modes"
arch=('any')
url="https://github.com/N0t4R0b0t/kwin-edid-tool"
license=('MIT')
depends=('python' 'libkscreen' 'libxcvt')
optdepends=(
  'openbsd-netcat: for the Sunshine connect-hook command'
  'socat: alternative to openbsd-netcat for the Sunshine connect-hook command'
)
makedepends=('git')
provides=('kwin-edid-tool')
conflicts=('kwin-edid-tool')
install=kwin-edid-tool-git.install
source=("$pkgname::git+https://github.com/N0t4R0b0t/kwin-edid-tool.git")
sha256sums=('SKIP')

pkgver() {
  cd "$pkgname"
  printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

package() {
  cd "$pkgname"

  # Core tool.
  install -Dm644 edid_lib.py "$pkgdir/usr/lib/kwin-edid-tool/edid_lib.py"
  install -Dm755 edid-custom-resolutions.py "$pkgdir/usr/lib/kwin-edid-tool/edid-custom-resolutions.py"

  # A wrapper execs the real file in place rather than symlinking it into
  # /usr/bin - Python's sys.path[0] handling for symlinked entry points is
  # inconsistent across versions/platforms, and this way `import edid_lib`
  # in the script reliably resolves via its own real directory.
  install -Dm755 /dev/stdin "$pkgdir/usr/bin/kwin-edid-custom-resolutions" <<'WRAPPER'
#!/bin/sh
exec /usr/lib/kwin-edid-tool/edid-custom-resolutions.py "$@"
WRAPPER

  # Optional Sunshine connect-hook integration. edid_lib.py is duplicated
  # alongside daemon.py rather than imported across directories - Python's
  # automatic same-directory sys.path entry then just finds it.
  install -Dm755 integrations/sunshine/daemon.py "$pkgdir/usr/lib/kwin-edid-tool/sunshine/daemon.py"
  install -Dm644 edid_lib.py "$pkgdir/usr/lib/kwin-edid-tool/sunshine/edid_lib.py"
  sed 's#/opt/sunshine-edid-helper#/usr/lib/kwin-edid-tool/sunshine#' \
    integrations/sunshine/sunshine-edid-helper.service > "$srcdir/sunshine-edid-helper.service.patched"
  install -Dm644 "$srcdir/sunshine-edid-helper.service.patched" "$pkgdir/usr/lib/systemd/system/sunshine-edid-helper.service"

  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
  install -Dm644 integrations/sunshine/README.md "$pkgdir/usr/share/doc/$pkgname/sunshine-integration.md"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
