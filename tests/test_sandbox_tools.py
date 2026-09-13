import unittest

import tools.filesystem as filesystem_module
import tools.terminal as terminal_module
from sandbox.models import ExecutionStatus, SandboxResult, ViolationStatus
from tools.filesystem import ReadTool, WriteTool, EditTool
from tools.registry import ToolRegistry
from tools.terminal import BashTool


class FakeSandbox:
    session_id = "s1"
    def __init__(self): self.calls = []
    def fs_call(self, request):
        self.calls.append(("fs", request))
        if request["operation"] == "read":
            return {"success": True, "status": "success", "content": "hello"}
        return {"success": True, "status": "success", "sha256": "a" * 64}
    def fingerprint(self, path): self.calls.append(("fingerprint", str(path))); return "b" * 64
    def exec(self, request):
        self.calls.append(("exec", request))
        return SandboxResult(ExecutionStatus.SUCCESS, "ok", "", 0, ViolationStatus.UNKNOWN, "s1", "cid", "image@sha256:" + "a" * 64)


class SandboxToolTests(unittest.TestCase):
    def test_unbound_side_effect_tools_fail_closed(self):
        for tool, kwargs in (
            (ReadTool(), {"path": "x"}),
            (WriteTool(), {"path": "x", "content": "y"}),
            (EditTool(), {"path": "x", "old_string": "a", "new_string": "b"}),
            (BashTool(), {"command": "echo unsafe"}),
        ):
            with self.subTest(tool=tool.name):
                result = tool.run(**kwargs)
                self.assertFalse(result.success)
                self.assertIn("sandbox", result.compact.lower())

    def test_filesystem_tools_only_delegate_to_sandbox(self):
        sandbox = FakeSandbox()
        self.assertFalse(hasattr(filesystem_module, "Path"))
        self.assertTrue(ReadTool(sandbox).run(path="x.txt").success)
        self.assertTrue(WriteTool(sandbox).run(path="x.txt", content="new").success)
        self.assertTrue(EditTool(sandbox).run(path="x.txt", old_string="a", new_string="b").success)
        self.assertEqual([call[1]["operation"] for call in sandbox.calls], ["read", "write", "edit"])

    def test_bash_only_delegates_to_sandbox(self):
        sandbox = FakeSandbox()
        self.assertFalse(hasattr(terminal_module, "subprocess"))
        result = BashTool(sandbox).run(command="echo ok")
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(sandbox.calls[0][0], "exec")
        self.assertEqual(str(sandbox.calls[0][1].cwd), ".")

    def test_registry_binds_every_os_tool_to_same_session(self):
        sandbox = FakeSandbox()
        registry = ToolRegistry(sandbox)
        self.assertEqual({x["function"]["name"] for x in registry.schemas()}, {"read", "write", "edit", "bash"})
        for name in ("read", "write", "edit", "bash"):
            self.assertIs(registry.get(name).sandbox, sandbox)

    def test_recovery_fingerprint_uses_sandbox(self):
        sandbox = FakeSandbox()
        tool = WriteTool(sandbox)
        self.assertEqual(tool.recovery_fingerprint({"path": "x.txt"}), "b" * 64)
        self.assertEqual(sandbox.calls, [("fingerprint", "x.txt")])


if __name__ == "__main__":
    unittest.main()
