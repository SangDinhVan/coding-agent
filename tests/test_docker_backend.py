import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sandbox.docker import (
    DockerBackend,
    DockerIsolationLevel,
    DockerPreflightError,
    DockerTransportError,
    classify_isolation_level,
)
from sandbox.models import ExecRequest, ManifestEntry, ResourceLimits, SandboxPath, WorkspaceMode


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, *, input=None, timeout=None):
        self.calls.append((argv, input, timeout))
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def result(stdout="", returncode=0, stderr=""):
    return type("Result", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()


class DockerBackendTests(unittest.TestCase):
    image = "coding-agent-python@sha256:" + "a" * 64

    def preflight_responses(self, info):
        return [
            result(json.dumps(info | {"CgroupVersion": "2", "ID": "daemon"})),
            result(json.dumps([{"RepoDigests": [self.image], "Config": {"User": "65532:65532"}}])),
        ]

    def test_classifies_all_daemon_isolation_levels(self):
        cases = (
            ({"SecurityOptions": ["name=rootless"], "OperatingSystem": "CachyOS", "ServerVersion": "29.0"}, DockerIsolationLevel.ROOTLESS),
            ({"SecurityOptions": ["name=seccomp"], "OperatingSystem": "Docker Desktop", "ServerVersion": "29.7.2"}, DockerIsolationLevel.VM_ISOLATED),
            ({"SecurityOptions": ["name=seccomp"], "OperatingSystem": "Ubuntu", "ServerVersion": "Docker Desktop 4.50"}, DockerIsolationLevel.VM_ISOLATED),
            ({"SecurityOptions": ["name=seccomp"], "OperatingSystem": "Ubuntu", "ServerVersion": "29.0"}, DockerIsolationLevel.ROOTFUL_BARE),
        )
        for info, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_isolation_level(info), expected)

    def test_preflight_rejects_rootful_bare_daemon(self):
        runner = FakeRunner([result(json.dumps({
            "SecurityOptions": ["name=seccomp"], "OperatingSystem": "Ubuntu",
            "ServerVersion": "29.0", "CgroupVersion": "2",
        }))])
        backend = DockerBackend(self.image, runner=runner)
        with self.assertRaisesRegex(DockerPreflightError, "ROOTFUL_BARE"):
            backend.preflight(ResourceLimits())

    def test_preflight_accepts_rootless_and_vm_isolated_daemons(self):
        cases = (
            ({"SecurityOptions": ["name=rootless"], "OperatingSystem": "CachyOS", "ServerVersion": "29.0"}, DockerIsolationLevel.ROOTLESS),
            ({"SecurityOptions": ["name=seccomp"], "OperatingSystem": "Docker Desktop", "ServerVersion": "29.7.2"}, DockerIsolationLevel.VM_ISOLATED),
        )
        for info, expected in cases:
            with self.subTest(expected=expected):
                runner = FakeRunner(self.preflight_responses(info))
                backend = DockerBackend(self.image, runner=runner)
                if expected == DockerIsolationLevel.VM_ISOLATED:
                    with self.assertLogs("sandbox.docker", level="WARNING") as logs:
                        evidence = backend.preflight(ResourceLimits())
                    self.assertIn("not equivalent to rootless", " ".join(logs.output))
                else:
                    evidence = backend.preflight(ResourceLimits())
                self.assertEqual(evidence["isolation_level"], expected.value)
                self.assertEqual(backend.isolation_level, expected)


    def test_accepts_exact_local_image_id_and_rejects_malformed_id(self):
        image_id = "sha256:" + "b" * 64
        backend = DockerBackend(image_id)
        self.assertEqual(backend.image, image_id)
        with self.assertRaisesRegex(DockerPreflightError, "immutable"):
            DockerBackend("sha256:short")

    def test_preflight_verifies_exact_local_image_id(self):
        image_id = "sha256:" + "b" * 64
        runner = FakeRunner([
            result(json.dumps({"SecurityOptions": ["name=rootless"], "CgroupVersion": "2", "ID": "daemon"})),
            result(json.dumps([{"Id": image_id, "RepoDigests": [], "Config": {"User": "65532:65532"}}])),
        ])
        evidence = DockerBackend(image_id, runner=runner).preflight(ResourceLimits())
        self.assertEqual(evidence["image"], image_id)

    def test_create_uses_required_hardening_without_shell(self):
        runner = FakeRunner([
            result(json.dumps({"SecurityOptions": ["name=rootless", "name=seccomp"], "CgroupVersion": "2", "ID": "daemon"})),
            result(json.dumps([{"RepoDigests": [self.image], "Config": {"User": "65532:65532"}}])),
            result("container-id\n"),
        ])
        with tempfile.TemporaryDirectory() as workspace:
            backend = DockerBackend(self.image, runner=runner)
            backend.preflight(ResourceLimits())
            container_id = backend.create("session-1", "workspace-hash", Path(workspace), ResourceLimits())
        self.assertEqual(container_id, "container-id")
        argv = runner.calls[-1][0]
        joined = " ".join(argv)
        for required in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges:true", "--pids-limit 256", "--memory 2147483648", "--memory-swap 2147483648", "--cpus 2.0", "dst=/workspace"):
            self.assertIn(required, joined)
        mount = argv[argv.index("--mount") + 1]
        self.assertEqual(mount, f"type=bind,src={Path(workspace).resolve()},dst=/workspace")
        self.assertNotIn(",rw", mount)
        self.assertNotIn("--privileged", argv)
        self.assertIsInstance(argv, list)

    def test_create_argv_is_identical_for_every_isolation_level(self):
        calls = []
        with tempfile.TemporaryDirectory() as workspace:
            for level in DockerIsolationLevel:
                runner = FakeRunner([result("container-id\n")])
                backend = DockerBackend(self.image, runner=runner)
                backend.isolation_level = level
                backend.create("session-1", "workspace-hash", Path(workspace), ResourceLimits())
                calls.append(runner.calls[0][0])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[1], calls[2])

    def test_docker_outside_of_docker_uses_daemon_visible_mount_sources(self):
        limits = ResourceLimits()
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state:
            mask = Path(state) / "mask-file"
            mask.touch()
            control_id = "b" * 12
            control = [{
                "Id": "b" * 64,
                "Mounts": [
                    {"Source": "/daemon/workspace", "Destination": workspace},
                    {"Source": "/daemon/state", "Destination": state},
                ],
            }]
            runner = FakeRunner([
                result(json.dumps({
                    "SecurityOptions": ["name=rootless"], "CgroupVersion": "2", "ID": "daemon",
                })),
                result(json.dumps(control)),
                result(json.dumps([{"RepoDigests": [self.image], "Config": {"User": "65532:65532"}}])),
                result("container-id\n"),
            ])
            backend = DockerBackend(self.image, runner=runner, control_container_id=control_id)
            backend.preflight(limits)
            backend.create(
                "session-1", "workspace-hash", Path(workspace), limits,
                mode=WorkspaceMode.LIVE,
                masks=((ManifestEntry(SandboxPath(".env"), "file"), mask),),
            )
        argv = runner.calls[-1][0]
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertEqual(mounts[0], "type=bind,src=/daemon/workspace,dst=/workspace")
        self.assertEqual(mounts[1], "type=bind,src=/daemon/state/mask-file,dst=/workspace/.env,readonly")
        self.assertIn("--network", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop", argv)

    def test_live_create_mounts_host_workspace_as_rootless_root_with_read_only_masks(self):
        runner = FakeRunner([result("container-id\n")])
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state:
            root = Path(workspace)
            mask_file = Path(state) / "empty-file"
            mask_directory = Path(state) / "empty-directory"
            mask_file.touch()
            mask_directory.mkdir()
            backend = DockerBackend(self.image, runner=runner)
            backend.create(
                "session-1", "workspace-hash", root, ResourceLimits(),
                mode=WorkspaceMode.LIVE,
                masks=(
                    (ManifestEntry(SandboxPath(".env"), "file"), mask_file),
                    (ManifestEntry(SandboxPath(".git"), "directory"), mask_directory),
                ),
            )
        argv = runner.calls[-1][0]
        self.assertEqual(argv[argv.index("--user") + 1], "0:0")
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertEqual(mounts[0], f"type=bind,src={root.resolve()},dst=/workspace")
        self.assertIn(f"type=bind,src={mask_file.resolve()},dst=/workspace/.env,readonly", mounts)
        self.assertIn(f"type=bind,src={mask_directory.resolve()},dst=/workspace/.git,readonly", mounts)

    def test_shadow_create_keeps_unprivileged_image_user(self):
        runner = FakeRunner([result("container-id\n")])
        with tempfile.TemporaryDirectory() as workspace:
            backend = DockerBackend(self.image, runner=runner)
            backend.create(
                "session-1", "workspace-hash", Path(workspace), ResourceLimits(),
                mode=WorkspaceMode.SHADOW,
            )
        argv = runner.calls[-1][0]
        self.assertEqual(argv[argv.index("--user") + 1], "65532:65532")

    def test_create_rejects_comma_in_bind_mount_path(self):
        runner = FakeRunner([result("container-id\n")])
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state:
            mask_file = Path(state) / "empty-file"
            mask_file.touch()
            backend = DockerBackend(self.image, runner=runner)
            with self.assertRaisesRegex(DockerPreflightError, "comma"):
                backend.create(
                    "session-1", "workspace-hash", Path(workspace), ResourceLimits(),
                    mode=WorkspaceMode.LIVE,
                    masks=((ManifestEntry(SandboxPath("secret,copy.json"), "file"), mask_file),),
                )
        self.assertEqual(runner.calls, [])

    def test_exec_passes_json_over_stdin_not_shell_command_argv(self):
        runner = FakeRunner([result('{"success":true,"status":"success","stdout":"ok","stderr":"","exit_code":0,"truncated":false}')])
        backend = DockerBackend(self.image, runner=runner)
        backend.container_id = "cid"
        response = backend.exec(ExecRequest("a", "echo $SECRET", SandboxPath("src"), 5))
        argv, payload, _ = runner.calls[0]
        self.assertNotIn("echo $SECRET", argv)
        self.assertEqual(json.loads(payload)["command"], "echo $SECRET")
        self.assertEqual(response.stdout, "ok")

    def test_inspect_normalizes_runtime_security_evidence(self):
        inspected = [{
            "Id": "cid", "Image": "image-id", "State": {"Running": False},
            "Config": {"Image": self.image, "User": "65532:65532", "Labels": {"io.sang-coding-agent.session": "s1"}},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "PidsLimit": 256,
                           "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000,
                           "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
            "Mounts": [{"Type": "bind", "Source": "/state/workspace", "Destination": "/workspace", "RW": True}],
        }]
        backend = DockerBackend(self.image, runner=FakeRunner([result(json.dumps(inspected))]))
        backend.container_id = "cid"
        evidence = backend.inspect()
        self.assertEqual(evidence["container_id"], "cid")
        self.assertEqual(evidence["image_digest"], self.image)
        self.assertEqual(evidence["mount_source"], "/state/workspace")
        self.assertFalse(evidence["running"])
        self.assertTrue(evidence["read_only_rootfs"])

    def test_verify_runtime_rejects_inspect_contract_mismatch(self):
        inspected = [{
            "Id": "cid", "State": {"Running": True},
            "Config": {"Image": self.image, "User": "65532:65532", "Labels": {}},
            "HostConfig": {"NetworkMode": "bridge", "ReadonlyRootfs": True},
            "Mounts": [],
        }]
        backend = DockerBackend(self.image, runner=FakeRunner([result(json.dumps(inspected))]))
        backend.container_id = "cid"
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaisesRegex(DockerPreflightError, "runtime"):
                backend.verify_runtime(ResourceLimits(), Path(workspace))

    def test_verify_runtime_malformed_probe_fails_as_preflight_error(self):
        limits = ResourceLimits()
        with tempfile.TemporaryDirectory() as workspace:
            source = str(Path(workspace).resolve())
            inspected = [{
                "Id": "cid", "State": {"Running": True},
                "Config": {"Image": self.image, "User": "65532:65532", "Labels": {}},
                "HostConfig": {
                    "NetworkMode": "none", "ReadonlyRootfs": True,
                    "PidsLimit": limits.pids, "Memory": limits.memory_bytes,
                    "MemorySwap": limits.memory_bytes,
                    "NanoCpus": int(limits.cpus * 1_000_000_000),
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                },
                "Mounts": [{"Source": source, "Destination": "/workspace", "RW": True}],
            }]
            probe = {
                "nonewprivs": "1", "capeff": "0",
                "memory": str(limits.memory_bytes), "swap": "0",
                "pids": str(limits.pids), "cpu": "max 100000",
                "route_lines": 1,
                "mounts": [
                    {"target": "/", "options": ["ro"]},
                    {"target": "/workspace", "options": ["rw"]},
                    {"target": "/tmp", "options": ["rw", "nosuid", "nodev", "noexec"]},
                ],
            }
            backend = DockerBackend(
                self.image,
                runner=FakeRunner([result(json.dumps(inspected)), result(json.dumps(probe))]),
            )
            backend.container_id = "cid"
            with self.assertRaisesRegex(DockerPreflightError, "runtime probe"):
                backend.verify_runtime(limits, Path(workspace))

    def test_malformed_helper_output_stops_and_verifies_container(self):
        runner = FakeRunner([
            result("not-json"),
            result("cid\n"),
            result(json.dumps([{"Id": "cid", "State": {"Running": False}, "Config": {}, "HostConfig": {}, "Mounts": []}])),
        ])
        backend = DockerBackend(self.image, runner=runner)
        backend.container_id = "cid"
        with self.assertRaisesRegex(DockerTransportError, "malformed"):
            backend.fs_call({"operation": "read", "path": "x"})
        self.assertEqual(runner.calls[1][0][:3], ["docker", "stop", "--time"])
        self.assertEqual(runner.calls[2][0], ["docker", "inspect", "cid"])

    def test_host_timeout_also_stops_container(self):
        runner = FakeRunner([
            subprocess.TimeoutExpired(["docker", "exec"], 2),
            result("cid\n"),
            result(json.dumps([{"Id": "cid", "State": {"Running": False}, "Config": {}, "HostConfig": {}, "Mounts": []}])),
        ])
        backend = DockerBackend(self.image, runner=runner)
        backend.container_id = "cid"
        with self.assertRaisesRegex(DockerTransportError, "ambiguous"):
            backend.fs_call({"operation": "read", "path": "x"})
        self.assertEqual(runner.calls[1][0][1], "stop")

    def test_helper_timeout_result_stops_whole_container(self):
        runner = FakeRunner([
            result('{"status":"timed_out","stdout":"","stderr":"","exit_code":-15,"truncated":false}'),
            result("cid\n"),
            result(json.dumps([{"Id": "cid", "State": {"Running": False}, "Config": {}, "HostConfig": {}, "Mounts": []}])),
        ])
        backend = DockerBackend(self.image, runner=runner)
        backend.container_id = "cid"
        response = backend.exec(ExecRequest("a", "sleep 10", SandboxPath("."), 1))
        self.assertEqual(response.status.value, "timed_out")
        self.assertEqual(runner.calls[1][0][1], "stop")
        self.assertEqual(runner.calls[2][0], ["docker", "inspect", "cid"])


if __name__ == "__main__":
    unittest.main()
