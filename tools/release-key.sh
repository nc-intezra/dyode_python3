#!/usr/bin/env bash
# Create DYODE's release signing key (once), on a machine you trust.
#
#   tools/release-key.sh "DYODE releases <you@example.org>"
#
# Writes, in packaging/:
#   dyode-release.pub   the public key, ASCII armored: commit it
#   dyode-release.gpg   the same key as a binary keyring for gpgv: commit it
# and, in a new private folder (mode 700) it names at the end:
#   dyode-release.secret.asc   the PRIVATE key: store it offline (two copies)
#                              and paste it into the GitHub secret
#                              DYODE_RELEASE_KEY
#   passphrase.txt             its passphrase: the GitHub secret
#                              DYODE_RELEASE_KEY_PASSPHRASE
#
# The key is Ed25519, signing only, valid for 5 years.  It is made in a
# throwaway GnuPG home, so your own keyring is not touched.
#
# Options: --out-dir DIR (default packaging/), --secret-dir DIR,
#          --passphrase-file FILE (default: a random one).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/packaging" SECRET_DIR="" PASSFILE="" UID_=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) OUT="$2"; shift ;;
    --secret-dir) SECRET_DIR="$2"; shift ;;
    --passphrase-file) PASSFILE="$2"; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) UID_="$1" ;;
  esac
  shift
done
[ -n "$UID_" ] || { echo "usage: tools/release-key.sh \"Name <email>\"" >&2; exit 2; }
command -v gpg >/dev/null || { echo "gpg is needed (package gnupg)" >&2; exit 1; }
for f in "$OUT/dyode-release.pub" "$OUT/dyode-release.gpg"; do
  [ ! -e "$f" ] || { echo "$f already exists. Not replacing a release key:" \
                          "delete both files first if you really mean to." >&2; exit 1; }
done

umask 077
GNUPGHOME="$(mktemp -d)"; export GNUPGHOME
trap 'gpgconf --kill all >/dev/null 2>&1 || true; rm -rf "$GNUPGHOME"' EXIT
SECRET_DIR="${SECRET_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/dyode-release-key.XXXXXX")}"
mkdir -p "$SECRET_DIR"; chmod 700 "$SECRET_DIR"
if [ -n "$PASSFILE" ]; then
  cp "$PASSFILE" "$SECRET_DIR/passphrase.txt"
else
  head -c 32 /dev/urandom | base64 | tr -d '\n=' > "$SECRET_DIR/passphrase.txt"
fi

gpg --batch --pinentry-mode loopback --passphrase-file "$SECRET_DIR/passphrase.txt" \
    --quick-generate-key "$UID_" ed25519 sign 5y 2>/dev/null
fpr="$(gpg --batch --with-colons --list-keys | awk -F: '/^fpr:/ {print $10; exit}')"
mkdir -p "$OUT"
gpg --batch --armor --export "$fpr" > "$OUT/dyode-release.pub"
gpg --batch --export "$fpr" > "$OUT/dyode-release.gpg"
gpg --batch --pinentry-mode loopback --passphrase-file "$SECRET_DIR/passphrase.txt" \
    --armor --export-secret-keys "$fpr" > "$SECRET_DIR/dyode-release.secret.asc"
umask 022
chmod 644 "$OUT/dyode-release.pub" "$OUT/dyode-release.gpg"

cat <<EOF
Release key created.
  fingerprint: $fpr

Commit (public):
  ${OUT#"$ROOT"/}/dyode-release.pub
  ${OUT#"$ROOT"/}/dyode-release.gpg

Private, in $SECRET_DIR (mode 700):
  dyode-release.secret.asc   private key
  passphrase.txt             its passphrase

Next:
  1. GitHub > Settings > Secrets and variables > Actions > New repository secret:
       DYODE_RELEASE_KEY             = contents of dyode-release.secret.asc
       DYODE_RELEASE_KEY_PASSPHRASE  = contents of passphrase.txt
  2. Copy both files to offline storage (two copies), then delete $SECRET_DIR.
  3. Publish the fingerprint above where users can check it (the README).
EOF
