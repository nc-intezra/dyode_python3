"""1.6.0 least privilege (D-06/D-07): the sandboxed systemd unit, the folders
it may write to, and install.sh creating the 'dyode' user and handing it
those folders.

install.sh runs against a copy of the repository with stand-ins for
useradd, groupadd, getent, id, chown, chmod and find, so the machine running
the tests is never changed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _setup
import dyode_common as common
import dyode

sys.path.insert(0, _setup.REPO)
import dyode_setup_core as core  # noqa: E402

sys.path.insert(0, os.path.join(_setup.REPO, "lib"))
import dyode_paths  # noqa: E402

CFG_TEXT = """\
config_name: test
dyode_in: {ip: 10.0.1.1, mac: "b8:27:eb:89:1e:f3", interface: eth0}
dyode_out: {ip: 10.0.1.2, mac: "b8:27:eb:b1:ff:ab", interface: eth1}
modules:
  files: {type: folder, port: 9600, in: %(root)s/in, out: %(root)s/out}
  files2: {type: folder, port: 9610, in: %(root)s/in2, out: %(root)s/out2, staging: %(root)s/stage2}
  plc: {type: modbus, port: 9400, ip: 192.168.1.150, registers: [0-100]}
  logs: {type: logs, port: 9700, listen_udp: 514, tail: [/var/log/plc/*.log],
         tail_state: %(root)s/state/tail.json, out: %(root)s/ot-logs}
"""


def load(root):
    path = os.path.join(root, "config.yaml")
    with open(path, "w") as fh:
        fh.write(CFG_TEXT % {"root": root})
    return path, common.load_config(path)


def directives(unit):
    found = {}
    for line in unit.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            found.setdefault(key, []).append(value)
    return found


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dyode_svc_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _, self.cfg = load(self.root)

    def test_output_box_folders(self):
        r = self.root
        self.assertEqual(common.runtime_paths(self.cfg, "out"), [
            ("out", r + "/out"), ("staging", r + "/out.incoming"),
            ("out", r + "/out2"), ("staging", r + "/stage2"),
            ("logs", r + "/ot-logs")])

    def test_input_box_folders(self):
        r = self.root
        self.assertEqual(common.runtime_paths(self.cfg, "in"), [
            ("in", r + "/in"), ("in", r + "/in2"), ("state", r + "/state")])

    def test_staging_matches_the_runtime(self):
        for props in common.modules_of_type(self.cfg, "folder").values():
            self.assertIn(("staging", dyode.staging_dir(props)),
                          common.runtime_paths(self.cfg, "out"))

    def test_side_detection(self):
        self.assertEqual(dyode_paths.detect_side(common, self.cfg, {"eth1": "10.0.1.2"}), "out")
        self.assertEqual(dyode_paths.detect_side(common, self.cfg, {"eth0": "10.0.1.1"}), "in")
        self.assertEqual(dyode_paths.detect_side(common, self.cfg, {"eth0": "192.168.0.4"}), "both")


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dyode_svc_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _, self.cfg = load(self.root)

    def unit(self, side="out", variant="v1", **kw):
        return core.systemd_unit_text(variant, side, "/opt/dyode/DYODE_v1_full",
                                      interface="eth1", cfg=self.cfg, **kw)

    def test_runs_unprivileged_in_a_sandbox(self):
        d = directives(self.unit())
        self.assertEqual(d["User"], ["dyode"])
        self.assertEqual(d["NoNewPrivileges"], ["yes"])
        self.assertEqual(d["ProtectSystem"], ["strict"])
        self.assertEqual(d["PrivateTmp"], ["yes"])
        self.assertEqual(d["PrivateDevices"], ["yes"])
        self.assertEqual(d["SystemCallFilter"], ["@system-service"])
        self.assertEqual(d["RestrictAddressFamilies"], ["AF_INET AF_INET6 AF_UNIX AF_NETLINK"])
        self.assertEqual(d["UMask"], ["0027"])

    def test_only_the_capabilities_each_side_uses(self):
        out, inn = directives(self.unit("out")), directives(self.unit("in"))
        self.assertEqual(out["AmbientCapabilities"], ["CAP_NET_BIND_SERVICE CAP_NET_RAW"])
        self.assertEqual(out["CapabilityBoundingSet"], out["AmbientCapabilities"])
        self.assertEqual(inn["AmbientCapabilities"], ["CAP_NET_ADMIN CAP_NET_BIND_SERVICE"])
        self.assertNotIn("CAP_SYS_ADMIN", self.unit() + self.unit("in"))

    def test_writes_only_to_its_own_folders(self):
        d = directives(self.unit("out"))
        self.assertEqual(d["ReadWritePaths"],
                         [p for _, p in common.runtime_paths(self.cfg, "out")])
        self.assertEqual(directives(self.unit("in"))["ReadWritePaths"],
                         [p for _, p in common.runtime_paths(self.cfg, "in")])

    def test_home_is_read_only_only_when_needed(self):
        self.assertEqual(directives(self.unit())["ProtectHome"], ["yes"])
        home = core.systemd_unit_text("v1", "out", "/home/pi/dyode/DYODE_v1_full",
                                      cfg=self.cfg)
        self.assertEqual(directives(home)["ProtectHome"], ["read-only"])

    def test_groups(self):
        self.assertEqual(directives(self.unit("out"))["SupplementaryGroups"], ["dyode-share"])
        # following /var/log files needs adm on the input box
        self.assertEqual(directives(self.unit("in"))["SupplementaryGroups"], ["dyode-share adm"])
        v2 = core.systemd_unit_text("v2", "out", "/opt/dyode/DYODE_v2_light/out")
        self.assertIn("dialout", directives(v2)["SupplementaryGroups"][0])
        self.assertEqual(directives(v2)["PrivateDevices"], ["no"])    # serial port

    def test_output_unit_starts_after_the_firewall(self):
        self.assertIn("After=dyode-firewall.service", self.unit("out"))
        self.assertNotIn("dyode-firewall", self.unit("in"))

    def test_paths_with_spaces_and_percent_are_escaped(self):
        self.assertEqual(core._unit_path("/srv/my files"), '"/srv/my files"')
        self.assertEqual(core._unit_path("/srv/100%"), "/srv/100%%")

    def test_run_as_root_keeps_the_old_unit(self):
        unit = self.unit(run_as_root=True)
        self.assertIn("User=root", unit)
        self.assertNotIn("ProtectSystem", unit)
        self.assertNotIn("AmbientCapabilities", unit)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "needs systemd-analyze")
    def test_systemd_accepts_the_unit(self):
        work = os.path.join(self.root, "work")
        os.makedirs(os.path.join(work, "venv", "bin"))
        os.symlink(sys.executable, os.path.join(work, "venv", "bin", "python"))
        units = os.path.join(self.root, "units")
        os.mkdir(units)
        for side in ("in", "out"):
            path = os.path.join(units, "dyode-%s.service" % side)
            with open(path, "w") as fh:
                fh.write(core.systemd_unit_text("v1", side, work, cfg=self.cfg))
            res = subprocess.run(["systemd-analyze", "verify", "--man=no", path],
                                 capture_output=True, text=True)
            errors = [l for l in res.stderr.splitlines()
                      if "dyode-%s.service" % side in l]
            self.assertEqual(errors, [], res.stderr)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class InstallServiceUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dyode_svcinst_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "DYODE_v1_full"))
        for f in ("install.sh", "dyode_setup.py", "dyode_setup_core.py", "VERSION"):
            shutil.copy(os.path.join(_setup.REPO, f), self.repo)
        shutil.copytree(os.path.join(_setup.REPO, "lib"), os.path.join(self.repo, "lib"))
        for f in os.listdir(_setup.V1):
            if f.endswith(".py"):
                shutil.copy(os.path.join(_setup.V1, f), os.path.join(self.repo, "DYODE_v1_full"))
        self.data = os.path.join(self.tmp, "data")
        os.mkdir(self.data)
        self.config, _ = load(self.data)
        shutil.copy(self.config, os.path.join(self.repo, "DYODE_v1_full", "config.yaml"))
        self.config = os.path.join(self.repo, "DYODE_v1_full", "config.yaml")
        self.etc = os.path.join(self.tmp, "etc")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        rec = 'echo "%s $*" >> "$LOG"'
        for name in ("useradd", "groupadd", "chown", "chmod", "find", "usermod",
                     "systemctl"):
            self.fake(name, rec % name + "; exit 0")
        # The accounts do not exist yet: getent/id fail for them.
        self.fake("getent", rec % "getent" + "; exit 2")
        self.fake("id", rec % "id" + '; [ "$#" -le 1 ] && { echo 0; exit 0; }; exit 1')

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def install(self, *args):
        env = dict(os.environ, DYODE_ETC=self.etc, LOG=self.log, PYTHON=sys.executable,
                   PATH=self.bin + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin")
        return subprocess.run(["bash", os.path.join(self.repo, "install.sh"), *args],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              env=env, timeout=60, cwd=self.tmp)

    def calls(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_creates_accounts_folders_and_unit(self):
        res = self.install("--service-only", "--side", "out")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        calls = self.calls()
        self.assertIn("groupadd --system dyode-share", calls)
        self.assertIn("useradd --system --user-group", calls)
        self.assertIn("/usr/sbin/nologin", calls)
        d = self.data
        self.assertIn("chown -R dyode:dyode-share %s/out" % d, calls)
        self.assertIn("chmod 2750 %s/out" % d, calls)
        self.assertIn("chown -R dyode:dyode %s/out.incoming" % d, calls)
        self.assertIn("chmod 0750 %s/out.incoming" % d, calls)
        self.assertIn("chmod 2750 %s/ot-logs" % d, calls)
        self.assertNotIn("%s/in " % d, calls)             # input box folders
        for p in ("out", "out.incoming", "out2", "stage2", "ot-logs"):
            self.assertTrue(os.path.isdir(os.path.join(d, p)), p)
        unit = open(os.path.join(self.repo, "DYODE_v1_full", "dyode-out.service")).read()
        self.assertIn("User=dyode", unit)
        self.assertIn("ReadWritePaths=%s/out" % d, unit)
        self.assertIn("sudo cp", res.stdout)              # not installed yet

    def test_input_side_folder_is_group_writable(self):
        res = self.install("--service-only", "--side", "in")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("chmod 2770 %s/in" % self.data, self.calls())

    def test_installed_unit_is_replaced_with_a_backup(self):
        units = os.path.join(self.etc, "systemd", "system")
        os.makedirs(units)
        with open(os.path.join(units, "dyode-out.service"), "w") as fh:
            fh.write("[Service]\nUser=root\n")
        res = self.install("--service-only", "--side", "out")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("User=dyode", open(os.path.join(units, "dyode-out.service")).read())
        self.assertTrue([f for f in os.listdir(units) if "dyode-bak" in f])
        self.assertIn("systemctl restart dyode-out.service", res.stdout)
        again = self.install("--service-only", "--side", "out")
        self.assertIn("already up to date", again.stdout)

    def test_system_folders_and_homes_are_never_taken_over(self):
        with open(self.config, "a") as fh:
            fh.write("  bad: {type: folder, port: 9620, in: /etc, out: /var}\n")
        res = self.install("--service-only", "--side", "out")
        self.assertIn("not taking over /var", res.stderr)
        self.assertNotIn("chown -R dyode:dyode-share /var\n", self.calls())

    def test_dry_run_changes_nothing(self):
        res = self.install("--service-only", "--side", "out", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("would create %s/out" % self.data, res.stdout)
        self.assertNotIn("useradd", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.data, "out")))

    def test_run_as_root_opt_out(self):
        res = self.install("--service-only", "--run-as-root", "--side", "out")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--run-as-root", self.install("--help").stdout)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class ShareGroupTests(unittest.TestCase):
    """With the service user, share clients reach the files through the
    dyode-share group (files are dyode:dyode-share 0640 in 2750 folders)."""

    def blocks(self, gid):
        lib = os.path.join(_setup.REPO, "lib")
        script = """
            say(){ :; }; warn(){ :; }; die(){ echo "$*" >&2; exit 1; }
            . %s/install-shares.sh; . %s/install-user.sh
            NFS_SHARES=("/srv/dyode/out|10.0.2.0/24|ro" "/srv/dyode/in|10.0.2.0/24|rw")
            SMB_SHARES=("/srv/dyode/out|alice|ro|out" "/srv/dyode/in|bob|rw|in")
            nfs_block; smb_block
        """ % (lib, lib)
        env = dict(os.environ)
        if gid:
            env.update(DYODE_SHARE_GID=gid, DYODE_SERVICE_UID="991")
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=env).stdout

    def test_with_the_service_user(self):
        out = self.blocks("990")
        self.assertIn("(ro,sync,subtree_check,all_squash,anonuid=991,anongid=990)", out)
        self.assertNotIn("root_squash", out)
        self.assertIn("force group = dyode-share", out)
        self.assertIn("create mask = 0660", out)

    def test_without_it_nothing_changes(self):
        out = self.blocks("")
        self.assertIn("root_squash", out)
        self.assertNotIn("force group", out)


class ReceivedFileModeTests(unittest.TestCase):
    def test_verified_files_are_group_readable(self):
        tmp = tempfile.mkdtemp(prefix="dyode_mode_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "out")
        os.mkdir(out)
        src = os.path.join(tmp, "src")
        with open(src, "wb") as fh:
            fh.write(b"data")
        fd = os.open(src, os.O_RDONLY)
        manifest = dyode.build_manifest([("a.txt", src, fd)])
        os.close(fd)
        import json
        mpath = os.path.join(tmp, "m")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        receiver = dyode.FolderReceiver(out)
        receiver.handle(mpath)
        blob = tempfile.mkstemp(dir=tmp)[1]            # 0600, like udp-receiver's
        with open(blob, "wb") as fh:
            fh.write(b"data")
        self.assertEqual(receiver.handle(blob), "stored")
        self.assertEqual(os.stat(os.path.join(out, "a.txt")).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
