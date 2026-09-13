"""Opt-in security integration tests requiring a real rootless Docker daemon."""
import errno
import json
import os
import socket
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from core.paths import ControlPaths
from sandbox.changes import ChangeSetError, apply_changeset
from sandbox.docker import DockerBackend, DockerPreflightError
from sandbox.models import ExecRequest, ExecutionStatus, ResourceLimits, SandboxPath, WorkspaceMode
from sandbox.session import SandboxSession, cleanup_expired
from sandbox.workspace import SnapshotError, prepare_workspace

RUN = os.environ.get("RUN_SANDBOX_INTEGRATION") == "1"
IMAGE = os.environ.get("SANDBOX_IMAGE", "")


@unittest.skipUnless(RUN, "set RUN_SANDBOX_INTEGRATION=1")
class RootlessSandboxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not IMAGE:
            raise unittest.SkipTest("SANDBOX_IMAGE is required")

    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        self.addCleanup(self.state_tmp.cleanup)
        self.source = Path(self.source_tmp.name)
        self.state = Path(self.state_tmp.name)
        (self.source / "main.py").write_text("print(1)\n", encoding="utf-8")
        (self.source / ".env").write_text("TOP_SECRET=host-canary\n", encoding="utf-8")
        self.session_id = "it-" + uuid.uuid4().hex
        self.paths = ControlPaths.create(self.state, self.session_id, self.source)
        self.backend = DockerBackend(IMAGE)
        self.limits = ResourceLimits(memory_bytes=512 * 1024**2, pids=64, tmpfs_bytes=64 * 1024**2, tool_timeout_seconds=5, output_bytes=1024 * 1024)
        self.session = SandboxSession(
            self.session_id, self.source, self.paths, self.backend, self.limits,
            mode=WorkspaceMode.SHADOW, free_space_floor_bytes=0,
        )
        self.addCleanup(self._cleanup)
        self.session.create()
        self.session.start()

    def _cleanup(self):
        try:
            self.backend.destroy()
        except Exception:
            pass

    def exec(self, command, timeout=5):
        return self.session.exec(ExecRequest(uuid.uuid4().hex, command, SandboxPath("."), timeout))

    def test_runtime_contract_and_cgroups(self):
        evidence = self.backend.inspect()
        self.assertTrue(evidence["running"])
        self.assertEqual(evidence["image_digest"], IMAGE)
        self.assertEqual(evidence["user"], "65532:65532")
        self.assertEqual(evidence["network_mode"], "none")
        self.assertTrue(evidence["read_only_rootfs"])
        self.assertEqual(evidence["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", evidence["security_opt"])
        self.assertEqual(evidence["pids"], 64)
        self.assertEqual(evidence["memory_bytes"], 512 * 1024**2)
        self.assertEqual(evidence["mount_source"], str(self.paths.workspace.resolve()))
        probe = self.exec("python - <<'PY'\nimport json\nfrom pathlib import Path\np=Path('/proc/self/status').read_text()\nr={k:v.strip() for k,v in (line.split(':',1) for line in p.splitlines() if ':' in line)}\nprint(json.dumps({'nonewprivs':r['NoNewPrivs'],'capeff':r['CapEff'],'memory':Path('/sys/fs/cgroup/memory.max').read_text().strip(),'swap':Path('/sys/fs/cgroup/memory.swap.max').read_text().strip(),'pids':Path('/sys/fs/cgroup/pids.max').read_text().strip(),'cpu':Path('/sys/fs/cgroup/cpu.max').read_text().strip(),'route':Path('/proc/net/route').read_text()}))\nPY")
        data = json.loads(probe.stdout)
        self.assertEqual(data["nonewprivs"], "1")
        self.assertEqual(int(data["capeff"], 16), 0)
        self.assertEqual(data["memory"], str(512 * 1024**2))
        self.assertEqual(data["swap"], "0")
        self.assertEqual(data["pids"], "64")
        self.assertEqual(data["cpu"], "200000 100000")
        self.assertEqual(len(data["route"].splitlines()), 1)

    def test_filesystem_escape_secrets_and_rootfs_are_blocked(self):
        for path in ("/etc/passwd", "../escape", "link/out"):
            if path == "link/out":
                (self.paths.workspace / "link").symlink_to("/tmp")
            result = self.session.fs_call({"operation": "write", "path": path, "content": "bad"})
            self.assertFalse(result["success"], path)
            self.assertEqual(result["status"], "blocked_by_sandbox")
        shell = self.exec("set +e; test ! -e .env; test ! -e .git; test ! -S /var/run/docker.sock; test ! -e /home/sang; printf bad >/root/bad 2>/dev/null; test $? -ne 0")
        self.assertTrue(shell.success, shell.stderr)
        self.assertEqual((self.source / ".env").read_text(), "TOP_SECRET=host-canary\n")

    def test_network_is_unreachable(self):
        result = self.exec("python - <<'PY'\nimport socket\nfor host,port in [('127.0.0.1',1),('169.254.169.254',80),('10.0.0.1',80),('1.1.1.1',53)]:\n s=socket.socket(); s.settimeout(.2)\n try: s.connect((host,port))\n except OSError: pass\n else: raise SystemExit(f'reachable: {host}')\n finally: s.close()\nPY")
        self.assertTrue(result.success, result.stderr)

    def test_pid_exhaustion_is_capped_by_cgroup(self):
        result = self.exec("""python - <<'PY'
import json, os, signal, time
children = []
try:
    while True:
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        children.append(pid)
except OSError as error:
    print(json.dumps({'errno': error.errno, 'count': len(children)}))
finally:
    for pid in children:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    for pid in children:
        try: os.waitpid(pid, 0)
        except ChildProcessError: pass
PY""", timeout=10)
        self.assertTrue(result.success, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["errno"], errno.EAGAIN)
        self.assertLess(observed["count"], self.limits.pids)

    def test_output_is_bounded_and_timeout_stops_container(self):
        output = self.exec("python -c \"print('x'*12000000)\"")
        self.assertTrue(output.truncated)
        self.assertLessEqual(len(output.stdout.encode()), 10 * 1024**2)
        timed = self.exec("(setsid sh -c 'sleep 30' &) ; sleep 30", timeout=1)
        self.assertEqual(timed.status, ExecutionStatus.TIMED_OUT)
        self.assertFalse(self.backend.inspect()["running"])

    def test_host_unchanged_until_exact_whole_set_apply_and_conflict_blocks_all(self):
        result = self.exec("printf 'print(2)\\n' > main.py; printf created > created.txt")
        self.assertTrue(result.success)
        self.assertEqual((self.source / "main.py").read_text(), "print(1)\n")
        self.assertFalse((self.source / "created.txt").exists())
        changes = self.session.prepare_changes()
        self.assertTrue(changes.approvable)
        (self.source / "main.py").write_text("user edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ChangeSetError, "conflict"):
            apply_changeset(self.paths, self.source, changes, changes.change_set_hash, "reviewed")
        self.assertFalse((self.source / "created.txt").exists())
        self.assertEqual((self.source / "main.py").read_text(), "user edit\n")

    def test_exact_whole_set_apply_succeeds(self):
        result = self.exec("printf 'print(2)\\n' > main.py; printf created > created.txt")
        self.assertTrue(result.success, result.stderr)
        changes = self.session.prepare_changes()
        applied = apply_changeset(self.paths, self.source, changes, changes.change_set_hash, "integration approval")
        self.assertTrue(applied.success)
        self.assertEqual((self.source / "main.py").read_text(), "print(2)\n")
        self.assertEqual((self.source / "created.txt").read_text(), "created")

    def test_injected_mid_apply_failure_rolls_back_real_workspace(self):
        result = self.exec("printf 'print(2)\\n' > main.py; printf created > created.txt")
        self.assertTrue(result.success, result.stderr)
        changes = self.session.prepare_changes()
        with self.assertRaisesRegex(OSError, "injected"):
            apply_changeset(
                self.paths, self.source, changes, changes.change_set_hash,
                "integration rollback", fail_after=2,
            )
        self.assertEqual((self.source / "main.py").read_text(), "print(1)\n")
        self.assertFalse((self.source / "created.txt").exists())
        self.assertFalse((self.paths.rollback / "apply-journal.json").exists())

    def test_expired_session_cleanup_removes_exact_container_and_state(self):
        self.session.stop("ttl")
        metadata = json.loads(self.paths.metadata.read_text(encoding="utf-8"))
        metadata["updated_at"] = 0
        self.paths.metadata.write_text(json.dumps(metadata), encoding="utf-8")

        def backend_factory(data):
            backend = DockerBackend(IMAGE)
            backend.container_id = data["container_id"]
            return backend

        removed = cleanup_expired(
            self.state, now=time.time(), ttl_seconds=1,
            backend_factory=backend_factory,
        )
        self.assertEqual(removed, [self.session_id])
        self.assertFalse(self.paths.session_dir.exists())

    def test_resume_reconciles_exact_container(self):
        self.session.stop("crash")
        resumed_backend = DockerBackend(IMAGE)
        resumed_backend.container_id = self.backend.container_id
        resumed_backend.session_id = self.session_id
        resumed = SandboxSession(
            self.session_id, self.source, self.paths, resumed_backend, self.limits,
            mode=WorkspaceMode.SHADOW, free_space_floor_bytes=0,
        )
        resumed.resume()
        self.assertTrue(resumed_backend.inspect()["running"])
        resumed.stop("test")


@unittest.skipUnless(RUN, "set RUN_SANDBOX_INTEGRATION=1")
class RootlessLiveWorkspaceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not IMAGE:
            raise unittest.SkipTest("SANDBOX_IMAGE is required")

    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        self.addCleanup(self.state_tmp.cleanup)
        self.source = Path(self.source_tmp.name)
        self.state = Path(self.state_tmp.name)
        (self.source / "main.py").write_text("print(1)\n", encoding="utf-8")
        (self.source / ".env").write_text("TOP_SECRET=host-canary\n", encoding="utf-8")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("private", encoding="utf-8")
        (self.source / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.source / "ignored").mkdir()
        (self.source / "ignored" / "secret.txt").write_text("ignored-secret", encoding="utf-8")
        self.session_id = "it-live-" + uuid.uuid4().hex
        self.paths = ControlPaths.create(self.state, self.session_id, self.source)
        self.backend = DockerBackend(IMAGE)
        self.limits = ResourceLimits(
            memory_bytes=512 * 1024**2, pids=64, tmpfs_bytes=64 * 1024**2,
            tool_timeout_seconds=5, output_bytes=1024 * 1024,
        )
        self.session = SandboxSession(
            self.session_id, self.source, self.paths, self.backend, self.limits,
            mode=WorkspaceMode.LIVE, free_space_floor_bytes=0,
        )
        self.addCleanup(self._cleanup)
        self.session.create()
        self.session.start()

    def _cleanup(self):
        try:
            self.backend.destroy()
        except Exception:
            pass

    def exec(self, command, timeout=5):
        return self.session.exec(ExecRequest(uuid.uuid4().hex, command, SandboxPath("."), timeout))

    def test_live_writes_are_immediate_security_masked_and_owned_by_host_user(self):
        result = self.exec(
            "set -e; printf 'print(2)\\n' > main.py; printf created > created.txt; "
            "test -f .env; test ! -s .env; "
            "test ! -e .git/config; test -f ignored/secret.txt; "
            "printf generated > ignored/output.txt; "
            "if printf exposed > .env 2>/dev/null; then exit 1; fi; "
            "test ! -S /var/run/docker.sock"
        )
        self.assertTrue(result.success, result.stderr)
        self.assertEqual((self.source / "main.py").read_text(encoding="utf-8"), "print(2)\n")
        created = self.source / "created.txt"
        self.assertEqual(created.read_text(encoding="utf-8"), "created")
        self.assertEqual(created.stat().st_uid, os.getuid())
        self.assertEqual((self.source / "ignored" / "output.txt").read_text(encoding="utf-8"), "generated")
        self.assertEqual((self.source / ".env").read_text(encoding="utf-8"), "TOP_SECRET=host-canary\n")
        self.assertEqual((self.source / ".git" / "config").read_text(encoding="utf-8"), "private")

    def test_live_runtime_keeps_rootless_security_contract(self):
        evidence = self.backend.inspect()
        self.assertEqual(evidence["mount_source"], str(self.source.resolve()))
        self.assertEqual(evidence["user"], "0:0")
        self.assertEqual(evidence["network_mode"], "none")
        self.assertTrue(evidence["read_only_rootfs"])
        self.assertEqual(evidence["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", evidence["security_opt"])
        network = self.exec("python -c \"import socket; socket.create_connection(('1.1.1.1', 53), .2)\"")
        self.assertFalse(network.success)

    def test_live_resume_reconciles_exact_source_and_masks(self):
        self.session.stop("resume-test")
        resumed_backend = DockerBackend(IMAGE)
        resumed_backend.container_id = self.backend.container_id
        resumed_backend.session_id = self.session_id
        resumed = SandboxSession(
            self.session_id, self.source, self.paths, resumed_backend, self.limits,
            mode=WorkspaceMode.LIVE, free_space_floor_bytes=0,
        )
        resumed.resume()
        self.assertTrue(resumed_backend.inspect()["running"])
        resumed.stop("test")


@unittest.skipUnless(RUN, "set RUN_SANDBOX_INTEGRATION=1")
class RootlessPreflightIntegrationTests(unittest.TestCase):
    def test_mutable_or_wrong_image_fails_closed(self):
        with self.assertRaises(DockerPreflightError):
            DockerBackend("python:latest")
        wrong = "missing@sha256:" + "0" * 64
        with self.assertRaises((DockerPreflightError, RuntimeError)):
            DockerBackend(wrong).preflight(ResourceLimits())

    def test_malicious_snapshot_rejects_escape_and_fifo_without_reading_canary(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as state_dir:
            source, state = Path(source_dir), Path(state_dir)
            (source / "main.py").write_text("print(1)\n", encoding="utf-8")
            (source / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
            paths = ControlPaths.create(state, "snapshot-secret", source)
            manifest = prepare_workspace(source, paths, free_space_floor_bytes=0)
            self.assertFalse((paths.workspace / ".env").exists())
            self.assertIn(".env", {str(entry.path) for entry in manifest.excluded})

        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as state_dir:
                source, state = Path(source_dir), Path(state_dir)
                canary = state / "outside-canary"
                canary.write_text("host-canary", encoding="utf-8")
                if kind == "symlink":
                    (source / "escape").symlink_to(canary)
                else:
                    os.mkfifo(source / "pipe")
                paths = ControlPaths.create(state, f"snapshot-{kind}", source)
                with self.assertRaises(SnapshotError):
                    prepare_workspace(source, paths, free_space_floor_bytes=0)
                self.assertEqual(canary.read_text(encoding="utf-8"), "host-canary")



if __name__ == "__main__":
    unittest.main()
