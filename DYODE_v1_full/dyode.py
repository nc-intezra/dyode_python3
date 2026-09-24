# -*- coding: utf-8 -*-
"""File transfer through the diode using udpcast (Python 3 port).

Protocol, per batch:
  1. the input side sends a JSON manifest listing each file's relative
     path, size and SHA-256, in sending order;
  2. it then sends each file, in that order, and deletes it once sent.
The output side receives the manifest, then each file, verifies size and
hash, and moves good files into place (keeping subfolders).

If a batch is interrupted, the output side notices the next manifest when
it arrives in place of an expected file, and resynchronizes on it instead
of discarding everything that follows.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import dyode_common as common

log = logging.getLogger("dyode.folder")

MANIFEST_MAGIC = "dyode-manifest-v1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
STAGING_DIR = ".dyode_incoming"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def hash_file(path, blocksize=65536):
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(blocksize), b""):
            hasher.update(block)
    return hasher.hexdigest()


class ChangeWatcher:
    """Wake up when something changes under `root` (recursively).

    Uses inotify through the small inotify_simple package. If it is not
    installed, falls back to plain polling, which is slower to react but
    otherwise equivalent (the sender rescans the folder either way).
    """

    def __init__(self, root, recursive=True):
        self.root = root
        self.recursive = recursive
        self.inotify = None
        try:
            from inotify_simple import INotify, flags
        except ImportError:
            log.warning("inotify_simple not installed: polling %s instead", root)
            return
        self.flags = flags
        self.inotify = INotify()
        self.mask = (flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE
                     | flags.DELETE_SELF | flags.MOVE_SELF)
        self._add_all()

    def _add_all(self):
        dirs = [self.root]
        if self.recursive:
            for dirpath, dirnames, _ in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".dyode")]
                dirs += [os.path.join(dirpath, d) for d in dirnames]
        for d in dirs:
            try:
                self.inotify.add_watch(d, self.mask)
            except OSError as err:
                log.debug("cannot watch %s: %s", d, err)

    def wait(self, timeout):
        """Block up to `timeout` seconds. Returns True if events arrived."""
        if self.inotify is None:
            time.sleep(timeout)
            return False
        events = self.inotify.read(timeout=int(timeout * 1000))
        if any(e.mask & self.flags.ISDIR for e in events):
            self._add_all()     # watch newly created subfolders
        return bool(events)


# --------------------------------------------------------------------------
# udpcast transport
# --------------------------------------------------------------------------

class UdpCast:
    """Thin wrapper around the udp-sender / udp-receiver programs.

    Arguments are passed as a list, never through a shell, so file names
    cannot inject commands (the original passed a shell string).
    """

    def __init__(self, props, cfg):
        net = cfg["network"]
        self.port = props["port"]
        self.bitrate = int(props.get("bitrate", 8))
        # Normalized by common._normalize_module: a ratio string, or None
        # when forward error correction is switched off for this module.
        self.fec = props.get("fec", common.DEFAULT_FEC)
        self.in_ip, self.out_ip = net["in_ip"], net["out_ip"]
        self.in_if, self.out_if = net["in_interface"], net["out_interface"]

    def sender_cmd(self, path):
        cmd = ["udp-sender", "--async"]
        if self.fec:
            cmd += ["--fec", self.fec]
        cmd += ["--max-bitrate", "%dm" % self.bitrate,
                "--mcast-rdv-addr", self.out_ip, "--mcast-data-addr", self.out_ip,
                "--portbase", str(self.port), "--autostart", "1",
                "--interface", self.in_if, "-f", path]
        return cmd

    def receiver_cmd(self, path):
        return ["udp-receiver", "--nosync", "--mcast-rdv-addr", self.in_ip,
                "--interface", self.out_if, "--portbase", str(self.port), "-f", path]

    @staticmethod
    def _run(cmd):
        log.debug("running: %s", cmd)
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            common.die("%s not found: install udpcast" % cmd[0])
        if res.returncode != 0:
            log.error("%s failed (exit %d): %s", cmd[0], res.returncode,
                      res.stderr.decode(errors="replace").strip()[-500:])
            return False
        return True

    def send(self, path):
        return self._run(self.sender_cmd(path))

    def receive(self, path):
        return self._run(self.receiver_cmd(path))


# --------------------------------------------------------------------------
# Input side
# --------------------------------------------------------------------------

def scan_ready_files(root, settle):
    """List regular files under root whose last change is at least `settle`
    seconds old (so half-written files are left for the next pass).

    Returns (ready, waiting): ready is a sorted list of (relpath, abspath);
    waiting counts files skipped because they are still changing.
    Symlinks are skipped on purpose: a link dropped in the input folder
    must not be able to exfiltrate an arbitrary file from the input box.
    """
    ready, waiting = [], 0
    now = time.time()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".dyode")
                             and not os.path.islink(os.path.join(dirpath, d)))
        for fname in sorted(filenames):
            path = os.path.join(dirpath, fname)
            try:
                st = os.lstat(path)
            except OSError:
                continue                      # vanished meanwhile
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            if now - st.st_mtime < settle:
                waiting += 1
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            ready.append((rel, path))
    return ready, waiting


def build_manifest(files):
    """files: list of (relpath, abspath). Hashes each file."""
    entries = []
    for rel, path in files:
        entries.append({"path": rel, "size": os.path.getsize(path),
                        "sha256": hash_file(path)})
    return {"magic": MANIFEST_MAGIC, "batch": uuid.uuid4().hex, "files": entries}


def send_batch(files, transport, workdir):
    """Send one batch. Returns the number of files sent (and deleted)."""
    manifest = build_manifest(files)
    manifest_path = os.path.join(workdir, "manifest_%s.json" % manifest["batch"])
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    try:
        log.info("sending manifest for %d file(s), batch %s",
                 len(files), manifest["batch"][:8])
        if not transport.send(manifest_path):
            return 0
    finally:
        os.remove(manifest_path)

    sent = 0
    for (rel, path), entry in zip(files, manifest["files"]):
        log.info("sending %s (%d bytes)", rel, entry["size"])
        if not transport.send(path):
            log.error("aborting batch; %d file(s) left for the next batch",
                      len(files) - sent)
            break
        try:
            os.remove(path)
        except OSError as err:
            log.error("sent %s but could not delete it: %s", rel, err)
        sent += 1
    return sent


def run_folder_input(name, props, cfg):
    """Input agent: watch props['in'] and send files as they settle."""
    common.setup_logging(cfg.get("_log_level", "INFO"))
    root = props["in"]
    os.makedirs(root, exist_ok=True)
    settle = float(props.get("settle", 2.0))
    rescan = float(props.get("rescan", 30.0))
    transport = UdpCast(props, cfg)
    watcher = ChangeWatcher(root)
    workdir = tempfile.mkdtemp(prefix="dyode_%s_" % props["port"])
    log.info("module %r: watching %s (port %d, %d Mbit/s, FEC %s)",
             name, root, props["port"], transport.bitrate,
             transport.fec or "off")
    try:
        while True:
            # Existing files are picked up at start-up too (the original
            # only noticed them once a new file arrived).
            ready, waiting = scan_ready_files(root, settle)
            if ready:
                send_batch(ready, transport, workdir)
                continue
            watcher.wait(settle if waiting else rescan)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Output side
# --------------------------------------------------------------------------

def safe_join(root, rel):
    """Join a manifest path under root, or return None if it tries to escape
    (absolute path, '..', empty or odd components)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".dyode") for p in parts):
        return None
    path = os.path.join(root, *parts)
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, os.path.abspath(path)]) != root_abs:
        return None
    return path


def read_manifest(path):
    """Return the manifest dict if the file at path is a valid manifest,
    otherwise None."""
    try:
        if os.path.getsize(path) > MAX_MANIFEST_BYTES:
            return None
        with open(path, "rb") as fh:
            if fh.read(1) != b"{":
                return None
            fh.seek(0)
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("magic") != MANIFEST_MAGIC:
        return None
    files = data.get("files")
    if not isinstance(files, list):
        return None
    for e in files:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                and isinstance(e.get("size"), int)
                and isinstance(e.get("sha256"), str) and len(e["sha256"]) == 64):
            return None
    return data


class FolderReceiver:
    """Output-side state machine. Feed it each received file with handle()."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.pending = []
        self.batch = None

    def handle(self, blob):
        """Process one received file. Always consumes (moves or deletes) it.
        Returns one of: 'manifest', 'stored', 'rejected', 'orphan'."""
        manifest = read_manifest(blob)
        if manifest is not None:
            if self.pending:
                log.warning("batch %s interrupted: %d file(s) never arrived",
                            (self.batch or "?")[:8], len(self.pending))
            self.pending = list(manifest["files"])
            self.batch = manifest.get("batch")
            log.info("manifest received: %d file(s), batch %s",
                     len(self.pending), (self.batch or "?")[:8])
            os.remove(blob)
            return "manifest"

        if not self.pending:
            log.warning("received a file with no manifest pending; discarded")
            os.remove(blob)
            return "orphan"

        entry = self.pending.pop(0)
        dest = safe_join(self.out_dir, entry["path"])
        if dest is None:
            log.error("unsafe path %r in manifest; file discarded", entry["path"])
            os.remove(blob)
            return "rejected"
        size = os.path.getsize(blob)
        if size != entry["size"] or hash_file(blob) != entry["sha256"]:
            log.error("checksum mismatch for %s (got %d bytes, expected %d); discarded",
                      entry["path"], size, entry["size"])
            os.remove(blob)
            return "rejected"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(blob, dest)            # atomic: same filesystem
        log.info("file %s available at %s", entry["path"], dest)
        return "stored"


def run_folder_output(name, props, cfg):
    """Output agent: receive files forever into props['out']."""
    common.setup_logging(cfg.get("_log_level", "INFO"))
    out_dir = props["out"]
    staging = os.path.join(out_dir, STAGING_DIR)
    os.makedirs(staging, exist_ok=True)
    # Clear leftovers from a previous crash.
    for leftover in os.listdir(staging):
        os.remove(os.path.join(staging, leftover))
    transport = UdpCast(props, cfg)
    receiver = FolderReceiver(out_dir)
    log.info("module %r: receiving into %s (port %d)", name, out_dir, props["port"])
    while True:
        fd, blob = tempfile.mkstemp(dir=staging)
        os.close(fd)
        if not transport.receive(blob):
            os.remove(blob)
            time.sleep(1.0)
            continue
        try:
            receiver.handle(blob)
        except OSError as err:
            log.error("could not store received file: %s", err)
            if os.path.exists(blob):
                os.remove(blob)
