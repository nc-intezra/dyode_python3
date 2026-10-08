#!/usr/bin/env bash
# Build packaging/debs/: the Ubuntu packages DYODE needs that do not come
# from pip, for offline installs.  Run on a machine WITH internet access and
# Docker (or Podman).  install.sh --offline installs from these files.
#
#   tools/build_debs.sh
#   RELEASES="24.04" ARCHES="amd64" tools/build_debs.sh
#
# Packages, per release:
#   22.04  udpcast python3.11 python3.11-venv
#          (22.04's python3 is 3.10, older than DYODE supports; 3.11 comes
#           from Ubuntu's own universe repository)
#   24.04  udpcast python3-venv
#   26.04  udpcast python3-venv
#
# Each release and CPU type is fetched inside a clean ubuntu:<release>
# container with apt's --download-only, so dependencies are resolved by apt
# itself rather than guessed.  That container is smaller than a real server
# install, so the set is a superset of what a given host needs; install.sh
# lets apt pick only the missing ones and never downgrades anything.
#
# arm64 (Raspberry Pi) on an x86 machine needs QEMU emulation:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
# or run the script on an arm64 machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_BASE="$ROOT/packaging/debs"
RELEASES="${RELEASES:-22.04 24.04 26.04}"
ARCHES="${ARCHES:-amd64 arm64}"

case "${1:-}" in
  -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

ENGINE="${ENGINE:-$(command -v docker || command -v podman || true)}"
[ -n "$ENGINE" ] || { echo "needs docker or podman" >&2; exit 1; }

packages_for() {
  case "$1" in
    22.04) echo "udpcast python3.11 python3.11-venv" ;;
    *)     echo "udpcast python3-venv" ;;
  esac
}

fail=0
for rel in $RELEASES; do
  for arch in $ARCHES; do
    out="$OUT_BASE/ubuntu-$rel/$arch"
    pkgs="$(packages_for "$rel")"
    echo "== Ubuntu $rel $arch: $pkgs"
    rm -rf "$out"; mkdir -p "$out"
    if ! "$ENGINE" run --rm --platform "linux/$arch" \
          -v "$out:/out" -e PKGS="$pkgs" \
          -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
          "ubuntu:$rel" bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        mkdir -p /out/partial          # apt refuses to download without it
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends --download-only \
          -o Dir::Cache::archives=/out $PKGS
        rm -rf /out/partial /out/lock
        cd /out
        sha256sum -- *.deb > SHA256SUMS
        chown -R "$HOST_UID:$HOST_GID" /out
      '; then
      echo "   FAILED (for arm64 on x86, see the QEMU note in --help)" >&2
      fail=1
      continue
    fi
    {
      echo "Ubuntu $rel $arch"
      echo "built:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "requested: $pkgs"
      echo
      (cd "$out" && ls -1 -- *.deb)
    } > "$out/MANIFEST.txt"
    echo "   $(ls "$out"/*.deb | wc -l) packages, $(du -sh "$out" | cut -f1)"
  done
done
[ "$fail" -eq 0 ] || { echo "some targets failed" >&2; exit 1; }
echo "Done. Commit packaging/debs/ to ship them."
