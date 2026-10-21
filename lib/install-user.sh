# shellcheck shell=bash
# The unprivileged service user (1.6.0), sourced by install.sh.
#
# DYODE's service runs as 'dyode' inside a systemd sandbox, keeping only the
# capabilities it needs (dyode_setup_core.service_capabilities).  Under
# ProtectSystem=strict it can write only to its own folders, so this step
# creates them, owned by dyode:
#
#   in/       (input box)   dyode:dyode-share 2770  share users drop files here
#   out/      (output box)  dyode:dyode-share 2750  share users read files here
#   logs out  (output box)  dyode:dyode-share 2750
#   staging   (output box)  dyode:dyode       0750  never shared
#   tail_state folder       dyode:dyode       0750
#
# The 'dyode-share' group is how SMB and NFS users reach those folders: SMB
# accounts are added to it, NFS exports map clients to it.  Then this side's
# systemd unit is regenerated (dyode_setup.py --write-unit) and, if it is
# installed in /etc/systemd/system, replaced (with a backup).
#
# --run-as-root skips all of this and keeps the pre-1.6.0 unit (User=root).

SVC_USER=dyode
SVC_SHARE_GROUP=dyode-share
SVC_UNIT_DIR="${DYODE_ETC:-/etc}/systemd/system"   # override: tests

service_user_wanted() { [ "${RUN_AS_ROOT:-0}" -eq 0 ]; }

describe_service_user() {
  if service_user_wanted; then
    say "  service:      runs as the '$SVC_USER' user in a systemd sandbox (--run-as-root: as before)"
  else
    say "  service:      runs as root (--run-as-root)"
  fi
}

# The gid of dyode-share, or nothing if the group does not exist (yet).
share_gid() {
  [ -n "${DYODE_SHARE_GID:-}" ] && { echo "$DYODE_SHARE_GID"; return; }  # tests
  getent group "$SVC_SHARE_GROUP" 2>/dev/null | cut -d: -f3
}
service_uid() {
  [ -n "${DYODE_SERVICE_UID:-}" ] && { echo "$DYODE_SERVICE_UID"; return; }  # tests
  id -u "$SVC_USER" 2>/dev/null
}

ensure_service_accounts() {
  if ! getent group "$SVC_SHARE_GROUP" >/dev/null 2>&1; then
    run groupadd --system "$SVC_SHARE_GROUP"
  fi
  if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    run useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --comment "DYODE data diode service" "$SVC_USER"
  fi
}

# own_folder ROLE PATH: create PATH and give it to the service.
own_folder() {
  local role="$1" path="$2" group mode
  case "$role" in
    in)          group="$SVC_SHARE_GROUP" mode=2770 ;;
    out|logs)    group="$SVC_SHARE_GROUP" mode=2750 ;;
    *)           group="$SVC_USER"        mode=0750 ;;
  esac
  # Never take over a system folder or someone's home: chown -R would.
  case "$path" in
    /|/etc|/etc/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|\
    /boot|/boot/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/run|/root|/home|/var|/var/log|\
    /var/lib|/opt|/srv|/tmp|/mnt|/media)
      warn "not taking over $path for the $role folder: choose a dedicated folder"
      return 0 ;;
  esac
  if getent passwd 2>/dev/null | cut -d: -f6 | grep -Fqx -- "$path"; then
    warn "not taking over $path for the $role folder: it is a home directory"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    say "   would create $path ($SVC_USER:$group, $mode)"
    return 0
  fi
  mkdir -p "$path"
  # Existing contents too: files received before 1.6.0 belong to root, mode
  # 0600, which neither the service nor share users could read.
  chown -R "$SVC_USER:$group" "$path"
  chmod "$mode" "$path"
  case "$role" in
    out|logs) chmod -R u+rwX,g+rX,g-w,o-rwx "$path"
              find "$path" -type d -exec chmod g+s {} + ;;
    in)       chmod -R u+rwX,g+rwX,o-rwx "$path"
              find "$path" -type d -exec chmod g+s {} + ;;
    *)        chmod -R u+rwX,go-rwx "$path" ;;
  esac
  say "   $path ($role): $SVC_USER:$group $mode"
}

# setup_service_user CONFIG PYTHON VARIANT SIDE
setup_service_user() {
  local cfg="$1" py="$2" variant="$3" side="$4" listing role path unit sides s new result
  service_user_wanted || return 0
  step "service user"
  if [ -z "$cfg" ] || [ ! -f "$cfg" ]; then
    say "   no config.yaml yet: after the setup wizard, run: sudo ./install.sh --service-only"
    return 0
  fi
  if [ "$(id -u)" -ne 0 ] && [ -z "${DYODE_ETC:-}" ] && [ "$DRY_RUN" -eq 0 ]; then
    warn "not root: skipped the service user. Rerun: sudo ./install.sh --service-only"
    return 0
  fi
  [ "$DRY_RUN" -eq 1 ] && say "   would create the '$SVC_USER' user and '$SVC_SHARE_GROUP' group" ||
    ensure_service_accounts
  listing="$("$py" "$ROOT/lib/dyode_paths.py" "$cfg" --side "${side:-auto}" 2>&1)" ||
    { warn "could not read the folders from ${cfg#"$ROOT"/}: $listing"; return 0; }
  while IFS=$'\t' read -r role path; do
    if [ "$role" = side ]; then side="$path"; continue; fi
    own_folder "$role" "$path"
  done <<<"$listing"

  # This side's unit, regenerated for the new user (both on a v1 box whose
  # side could not be told from its addresses).
  case "$side" in in|out) sides="$side" ;; *) sides="in out" ;; esac
  for s in $sides; do
    unit="dyode-$s.service"
    if [ "$DRY_RUN" -eq 1 ]; then
      say "   would regenerate $unit (dyode_setup.py --write-unit --side $s)"
      continue
    fi
    "$py" "$ROOT/dyode_setup.py" --repo "$ROOT" --variant "$variant" --side "$s" \
        --write-unit >/dev/null || { warn "could not regenerate $unit"; continue; }
    new="$(target_folder "$variant" "$s")/$unit"
    if [ -f "$SVC_UNIT_DIR/$unit" ]; then
      result="$(install_file "$SVC_UNIT_DIR/$unit" "$new")"
      case "$result" in
        unchanged) say "   $SVC_UNIT_DIR/$unit already up to date" ;;
        *) say "   updated $SVC_UNIT_DIR/$unit${result:+ (previous copy: $result)}"
           command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload >/dev/null 2>&1
           say "   restart it to run as $SVC_USER: sudo systemctl restart $unit" ;;
      esac
    else
      say "   generated ${new#"$ROOT"/}; install it with:"
      say "     sudo cp ${new} $SVC_UNIT_DIR/ && sudo systemctl daemon-reload && sudo systemctl enable --now $unit"
    fi
  done
}

target_folder() {               # target_folder VARIANT SIDE
  if [ "$1" = v1 ]; then echo "$ROOT/DYODE_v1_full"; else echo "$ROOT/DYODE_v2_light/$2"; fi
}
