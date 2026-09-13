"""Read-only sandbox projection for the model prompt."""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class WorkspaceState:
    sandbox: object | None = None

    def snapshot(self) -> dict:
        if self.sandbox is None:
            return {"cwd": "/workspace", "sandbox_status": "unavailable", "image": "unknown", "network": "none", "included": 0, "excluded": 0}
        try:
            manifest = json.loads(self.sandbox.paths.baseline_manifest.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            manifest = {"included": [], "excluded": []}
        status = getattr(getattr(self.sandbox, "status", None), "value", "unavailable")
        image = getattr(getattr(self.sandbox, "backend", None), "image", "unknown")
        return {
            "cwd": "/workspace",
            "sandbox_status": status,
            "image": image.rsplit("sha256:", 1)[-1][:12] if "sha256:" in image else image,
            "network": "none",
            "included": len(manifest.get("included", [])),
            "excluded": len(manifest.get("excluded", [])),
        }

    def render(self) -> str:
        snap = self.snapshot()
        return (
            f"cwd: {snap['cwd']}\n"
            f"sandbox: {snap['sandbox_status']}\n"
            f"image: {snap['image']}\n"
            f"network: {snap['network']}\n"
            f"included files: {snap['included']}\n"
            f"excluded entries: {snap['excluded']}"
        )


ToolState = WorkspaceState
