# shellcheck shell=bash
# Release signature and bundle age (1.6.0), sourced by install.sh.
#
# A release tarball carries, at its top level:
#   SHA256SUMS      the SHA-256 of every file in the release
#   SHA256SUMS.sig  a detached GPG signature of SHA256SUMS, made by the
#                   release key whose public half ships in the repository as
#                   packaging/dyode-release.pub (and, for gpgv, as the binary
#                   keyring packaging/dyode-release.gpg)
#
# install.sh checks the signature with gpgv, then every file against
# SHA256SUMS, before it changes anything.  Offline (the air-gapped case)
# a release without a signature is refused unless --allow-unsigned is
# given, for local test builds and git checkouts.  A signature that does
# not verify is always refused.
#
# The public key is in the tarball it checks, so it proves the files match
# what was signed with that key.  Check the key itself once, against the
# fingerprint published in the README, on a machine you trust.

RELEASE_KEYRING="${DYODE_RELEASE_KEYRING:-$ROOT/packaging/dyode-release.gpg}"   # override: tests
BUILD_INFO="$ROOT/packaging/BUILD-INFO"
BUNDLE_MAX_AGE_DAYS=90

# verify_release DIR REQUIRED(0|1)
verify_release() {
  local dir="$1" required="$2" out bad
  if [ ! -f "$dir/SHA256SUMS.sig" ]; then
    if [ "$required" -eq 1 ] && [ "${ALLOW_UNSIGNED:-0}" -eq 0 ]; then
      die "this copy has no release signature (SHA256SUMS.sig).
   Use a release tarball from GitHub Releases, or, for a local test build
   or a git checkout you trust, rerun with --allow-unsigned."
    fi
    warn "no release signature (SHA256SUMS.sig): the files are not verified$([ "${ALLOW_UNSIGNED:-0}" -eq 1 ] && echo " (--allow-unsigned)")"
    return 0
  fi
  command -v gpgv >/dev/null 2>&1 ||
    die "gpgv is needed to check the release signature (package gpgv; it is part of every supported system)"
  [ -f "$RELEASE_KEYRING" ] ||
    die "the release key ${RELEASE_KEYRING#"$ROOT"/} is missing, so the signature cannot be checked"
  [ -f "$dir/SHA256SUMS" ] || die "SHA256SUMS.sig without SHA256SUMS: the release is incomplete"
  if ! out="$(gpgv --keyring "$RELEASE_KEYRING" "$dir/SHA256SUMS.sig" "$dir/SHA256SUMS" 2>&1)"; then
    die "the release signature does NOT verify: do not install this copy.
$(printf '%s\n' "$out" | sed 's/^/   /' | tail -4)"
  fi
  say "   release signature: $(printf '%s\n' "$out" | sed -n 's/.*Good signature from \(.*\)/good, \1/p' | head -1)"
  say "   key: $(printf '%s\n' "$out" | sed -n 's/.*using [A-Z0-9]* key \([0-9A-F]*\).*/\1/p' | head -1)"
  if ! bad="$(cd "$dir" && sha256sum --quiet --strict -c SHA256SUMS 2>&1)"; then
    die "files differ from the signed SHA256SUMS: copy the release again.
$(printf '%s\n' "$bad" | sed 's/^/   /' | head -10)"
  fi
  say "   $(grep -c . "$dir/SHA256SUMS") files match the signed SHA256SUMS"
}

# How old the offline bundle is (packaging/BUILD-INFO, written when it was
# built).  An old bundle installs packages without months of security fixes.
bundle_age_report() {
  local built today age
  [ -f "$BUILD_INFO" ] || { warn "no packaging/BUILD-INFO: the bundle's build date is unknown"; return 0; }
  built="$(sed -n 's/^built=\([0-9-]*\).*/\1/p' "$BUILD_INFO" | head -1)"
  [ -n "$built" ] || { warn "packaging/BUILD-INFO has no build date"; return 0; }
  today="${DYODE_TODAY:-$(date -u +%Y-%m-%d)}"                       # override: tests
  age=$(( ( $(date -u -d "$today" +%s) - $(date -u -d "$built" +%s) ) / 86400 ))
  say "   bundle built $built ($age days ago)"
  if [ "$age" -gt "$BUNDLE_MAX_AGE_DAYS" ]; then
    warn "the offline bundle is $age days old: its packages may lack security fixes released since.
   Download the latest release, or rebuild the bundle (tools/build_debs.sh,
   tools/build_wheelhouse.sh, or the 'Offline install bundle' workflow)."
  fi
}
