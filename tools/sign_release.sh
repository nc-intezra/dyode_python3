#!/usr/bin/env bash
# Build and sign the release tarball (run by .github/workflows/release.yml).
#
#   DYODE_RELEASE_KEY=<armored private key> \
#   DYODE_RELEASE_KEY_PASSPHRASE=<passphrase> tools/sign_release.sh
#
# In the current directory (a git checkout), for version V from VERSION:
#   SHA256SUMS            SHA-256 of every tracked file
#   SHA256SUMS.sig        its detached signature
#   dyode-V.tar.gz        git archive of HEAD plus those two files
#   dyode-V.tar.gz.sig    detached signature of the tarball
#   dyode-V.tar.gz.sha256
#
# Refuses to sign with any key other than the one committed in
# packaging/dyode-release.gpg, so a release always verifies with the key
# users already have.  Create that key with tools/release-key.sh.
set -euo pipefail

err() { echo "::error::$*" >&2; exit 1; }

[ -n "${DYODE_RELEASE_KEY:-}" ] ||
  err "the DYODE_RELEASE_KEY secret is not set, so this release cannot be signed. Create the key with tools/release-key.sh, commit packaging/dyode-release.pub and .gpg, and add the DYODE_RELEASE_KEY and DYODE_RELEASE_KEY_PASSPHRASE repository secrets."
KEYRING="$PWD/packaging/dyode-release.gpg"
[ -f "$KEYRING" ] || err "packaging/dyode-release.gpg is not committed: users could not check the signature"
git diff --quiet HEAD -- || err "the working tree differs from HEAD; refusing to sign"

version="$(cat VERSION)"
name="dyode-$version"

GNUPGHOME="$(mktemp -d)"; export GNUPGHOME
trap 'gpgconf --kill all >/dev/null 2>&1 || true; rm -rf "$GNUPGHOME"' EXIT
printf '%s\n' "$DYODE_RELEASE_KEY" | gpg --batch --quiet --import 2>/dev/null ||
  err "DYODE_RELEASE_KEY is not a usable private key"
pass="$GNUPGHOME/passphrase"
printf '%s' "${DYODE_RELEASE_KEY_PASSPHRASE:-}" > "$pass"
sign() {   # sign OUT FILE
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$pass" \
      --detach-sign -o "$1" "$2" || err "signing $2 failed (wrong passphrase?)"
}

rm -f SHA256SUMS SHA256SUMS.sig
git ls-files -z | grep -zvx -e SHA256SUMS -e SHA256SUMS.sig | xargs -0 sha256sum -- > SHA256SUMS
sign SHA256SUMS.sig SHA256SUMS
gpgv --keyring "$KEYRING" SHA256SUMS.sig SHA256SUMS >/dev/null 2>&1 ||
  err "the release key in the secrets is not the one in packaging/dyode-release.gpg"

git archive --format=tar.gz --prefix="$name/" \
    --add-file=SHA256SUMS --add-file=SHA256SUMS.sig -o "$name.tar.gz" HEAD
sign "$name.tar.gz.sig" "$name.tar.gz"
sha256sum "$name.tar.gz" > "$name.tar.gz.sha256"
echo "signed $(grep -c . SHA256SUMS) files; $name.tar.gz, .sig, .sha256 ready"
