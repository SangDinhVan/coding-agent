import json
import tempfile
import time
import unittest
from pathlib import Path

from core.paths import ControlPaths
from sandbox.models import ResourceLimits, SandboxStatus, WorkspaceMode
from sandbox.session import SandboxSession, SessionError, cleanup_expired


class FakeBackend:
    def __init__(self):
        self.container_id = None
        self.running = False
        self.stops = 0
        self.destroyed = False

    def preflight(self, limits):
        return {"daemon_id": "daemon", "isolation_level": "VM_ISOLATED"}
    def create(self, session_id, identity, workspace, limits, *, mode=WorkspaceMode.SHADOW, masks=()):
        self.container_id = "cid-" + session_id
        self.identity = identity
        self.workspace = Path(workspace)
        self.mode = WorkspaceMode(mode)
        self.masks = tuple(masks)
        return self.container_id
    def start(self): self.running = True
    def verify_runtime(self, limits, workspace): return {}
    def stop(self, grace_seconds=5): self.running = False; self.stops += 1
    def destroy(self): self.running = False; self.destroyed = True
    def inspect(self):
        return {
            "container_id": self.container_id,
            "running": self.running,
            "image_digest": "test",
            "user": "65532:65532",
            "labels": {
                "io.sang-coding-agent.managed": "true",
                "io.sang-coding-agent.session": "s1",
                "io.sang-coding-agent.workspace": self.identity,
                "io.sang-coding-agent.image": "test",
                "io.sang-coding-agent.workspace-mode": self.mode.value,
            },
            "mount_source": str(self.workspace.resolve()),
            "mount_rw": True,
            "mounts": [
                {
                    "Destination": f"/workspace/{entry.path}",
                    "Source": str(source),
                    "RW": False,
                }
                for entry, source in self.masks
            ],
            "network_mode": "none",
            "read_only_rootfs": True,
            "pids": 256,
            "memory_bytes": 2 * 1024**3,
            "memory_swap_bytes": 2 * 1024**3,
            "cpus": 2.0,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }


class SandboxSessionTests(unittest.TestCase):
    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        self.addCleanup(self.state_tmp.cleanup)
        self.source = Path(self.source_tmp.name)
        (self.source / "main.py").write_text("print(1)")
        self.paths = ControlPaths.create(self.state_tmp.name, "s1", self.source)
        self.backend = FakeBackend()

    def session(self, mode=WorkspaceMode.SHADOW):
        return SandboxSession(
            "s1", self.source, self.paths, self.backend, ResourceLimits(),
            mode=mode, free_space_floor_bytes=0,
        )

    def test_create_start_stop_and_resume_persist_transitions(self):
        session = self.session()
        session.create()
        self.assertEqual(session.status, SandboxStatus.CREATED)
        session.start()
        self.assertEqual(session.status, SandboxStatus.RUNNING)
        session.stop("exit")
        self.assertEqual(session.status, SandboxStatus.STOPPED)
        session.resume()
        self.assertEqual(session.status, SandboxStatus.RUNNING)
        metadata = json.loads(self.paths.metadata.read_text())
        self.assertEqual(metadata["container_id"], "cid-s1")
        self.assertEqual(metadata["isolation_level"], "VM_ISOLATED")

    def test_invalid_transition_fails_closed(self):
        session = self.session()
        with self.assertRaises(SessionError):
            session.start()

    def test_resume_rejects_container_identity_mismatch(self):
        session = self.session()
        session.create(); session.start(); session.stop("exit")
        self.backend.container_id = "attacker-container"
        with self.assertRaisesRegex(SessionError, "identity"):
            session.resume()

    def test_resume_rejects_security_contract_mismatch(self):
        session = self.session()
        session.create(); session.start(); session.stop("exit")
        original = self.backend.inspect
        self.backend.inspect = lambda: original() | {"network_mode": "bridge"}
        with self.assertRaisesRegex(SessionError, "reconciliation"):
            session.resume()

    def test_start_runtime_probe_failure_stops_verifies_and_marks_error(self):
        session = self.session()
        session.create()
        inspected = self.backend.inspect
        self.backend.inspect_calls = 0
        def inspect():
            self.backend.inspect_calls += 1
            return inspected()
        self.backend.inspect = inspect
        self.backend.verify_runtime = lambda limits, workspace: (_ for _ in ()).throw(RuntimeError("probe mismatch"))
        with self.assertRaisesRegex(RuntimeError, "probe mismatch"):
            session.start()
        self.assertFalse(self.backend.running)
        self.assertEqual(self.backend.stops, 1)
        self.assertEqual(self.backend.inspect_calls, 1)
        self.assertEqual(session.status, SandboxStatus.ERROR)

    def test_prepare_changes_stops_before_scan_and_seals(self):
        session = self.session()
        session.create(); session.start()
        (self.paths.workspace / "main.py").write_text("print(2)")
        changes = session.prepare_changes()
        self.assertEqual(session.status, SandboxStatus.SEALED)
        self.assertFalse(self.backend.running)
        self.assertTrue(changes.approvable)

    def test_workspace_budget_stops_container(self):
        session = self.session()
        session.create(); session.start()
        (self.paths.workspace / "large").write_bytes(b"x" * 20)
        session.check_disk_budget(baseline_bytes=0, growth_limit_bytes=10, free_space_floor_bytes=0)
        self.assertEqual(session.status, SandboxStatus.ERROR)
        self.assertGreaterEqual(self.backend.stops, 1)

    def test_live_session_mounts_source_and_persists_mode_and_masks(self):
        (self.source / ".env").write_text("SECRET=hidden", encoding="utf-8")
        (self.source / ".git").mkdir()
        session = self.session(WorkspaceMode.LIVE)

        session.create()

        self.assertEqual(self.backend.workspace, self.source.resolve())
        self.assertEqual(self.backend.mode, WorkspaceMode.LIVE)
        self.assertEqual(
            {str(entry.path) for entry, _ in self.backend.masks},
            {".env", ".git"},
        )
        metadata = json.loads(self.paths.metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata["workspace_mode"], "live")
        self.assertEqual({item["path"] for item in metadata["masks"]}, {".env", ".git"})

    def test_live_tool_effect_is_immediately_visible_and_cannot_be_sealed(self):
        session = self.session(WorkspaceMode.LIVE)
        session.create(); session.start()
        (self.backend.workspace / "main.py").write_text("print(2)", encoding="utf-8")
        self.assertEqual((self.source / "main.py").read_text(encoding="utf-8"), "print(2)")
        with self.assertRaisesRegex(SessionError, "live"):
            session.prepare_changes()

    def test_legacy_metadata_without_mode_restores_as_shadow(self):
        session = self.session(WorkspaceMode.SHADOW)
        session.create(); session.stop("exit")
        metadata = json.loads(self.paths.metadata.read_text(encoding="utf-8"))
        metadata.pop("workspace_mode")
        metadata.pop("masks")
        self.paths.metadata.write_text(json.dumps(metadata), encoding="utf-8")
        current_inspect = self.backend.inspect

        def legacy_inspect():
            evidence = current_inspect()
            evidence["labels"].pop("io.sang-coding-agent.workspace-mode")
            return evidence

        self.backend.inspect = legacy_inspect
        restored = SandboxSession(
            "s1", self.source, self.paths, self.backend, ResourceLimits(),
            mode=WorkspaceMode.LIVE, free_space_floor_bytes=0,
        )

        self.assertEqual(restored.mode, WorkspaceMode.SHADOW)
        restored.resume()
        self.assertTrue(self.backend.running)

    def test_cleanup_expired_only_deletes_managed_expired_session(self):
        session = self.session(); session.create(); session.stop("exit")
        metadata = json.loads(self.paths.metadata.read_text())
        metadata["updated_at"] = 0
        self.paths.metadata.write_text(json.dumps(metadata))
        removed = cleanup_expired(Path(self.state_tmp.name), now=time.time(), ttl_seconds=1, backend_factory=lambda data: self.backend)
        self.assertEqual(removed, ["s1"])
        self.assertTrue(self.backend.destroyed)
        self.assertFalse(self.paths.session_dir.exists())

    def test_watchdog_refreshes_heartbeat_and_stops_cleanly(self):
        session = self.session()
        session.create()
        session.start_watchdog(interval_seconds=0.01)
        deadline = time.time() + 1
        while self.paths.heartbeat.stat().st_mtime <= self.paths.metadata.stat().st_mtime and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreater(self.paths.heartbeat.stat().st_mtime, self.paths.metadata.stat().st_mtime)
        session.stop_watchdog()
        self.assertFalse(session._watchdog_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
