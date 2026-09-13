import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker-entrypoint.sh"


class ComposeLauncherTests(unittest.TestCase):
    def test_launcher_builds_sandbox_and_starts_agent_with_immutable_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "bin"
            binary.mkdir()
            log = root / "calls"
            image_id = "sha256:" + "a" * 64
            (binary / "docker").write_text(
                "#!/bin/sh\nprintf 'docker:%s\\n' \"$*\" >>\"$CALL_LOG\"\nprintf '%s\\n' \"$IMAGE_ID\"\n",
                encoding="utf-8",
            )
            (binary / "coding-agent").write_text(
                "#!/bin/sh\nprintf 'agent:%s\\n' \"$*\" >>\"$CALL_LOG\"\n",
                encoding="utf-8",
            )
            for executable in binary.iterdir():
                executable.chmod(0o755)
            workspace = root / "workspace"
            (workspace / "sandbox-image").mkdir(parents=True)
            state = root / "state"
            env = os.environ | {
                "PATH": f"{binary}:{os.environ['PATH']}",
                "CALL_LOG": str(log),
                "IMAGE_ID": image_id,
                "WORKSPACE": str(workspace),
                "STATE_ROOT": str(state),
            }
            result = subprocess.run(
                [str(ENTRYPOINT), "resume", "--last"], env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), [
                f"docker:build --quiet --tag sang-coding-agent-sandbox:local {workspace}/sandbox-image",
                f"agent:--workspace {workspace} --state-root {state} --image {image_id} resume --last",
            ])

    def test_launcher_rejects_non_immutable_build_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "bin"
            binary.mkdir()
            (binary / "docker").write_text("#!/bin/sh\necho mutable-tag\n", encoding="utf-8")
            (binary / "docker").chmod(0o755)
            workspace = root / "workspace"
            (workspace / "sandbox-image").mkdir(parents=True)
            result = subprocess.run(
                [str(ENTRYPOINT)],
                env=os.environ | {"PATH": f"{binary}:{os.environ['PATH']}", "WORKSPACE": str(workspace)},
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("immutable image ID", result.stderr)


    def test_control_and_sandbox_dockerfiles_include_required_runtime_tools(self):
        control_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("apt-get install", control_dockerfile)
        self.assertIn("git", control_dockerfile)

        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("env_file:", compose)
        self.assertIn("- .env", compose)

        sandbox_dockerfile = (ROOT / "sandbox-image" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY --chmod", sandbox_dockerfile)
        self.assertIn("chmod 0555 /opt/coding-agent/bin/sandbox_fs.py", sandbox_dockerfile)


if __name__ == "__main__":
    unittest.main()
