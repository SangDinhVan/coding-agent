"""
agent/loop.py — vòng lặp chính Agent.run_turn(), tích hợp toàn bộ các phần đã build:

  tools/    -> registry (list tool, gọi tool.run())
  memory/   -> EventStore (raw history) + MemoryManager (PROJECT.md)
  context/  -> Compactor (nén messages dài trước khi gửi model)
  model/    -> llm.complete() (gọi litellm)
  agent/    -> TurnState, PlanState, ToolState (state không bị compact)

Đây là bản thay thế cho if/elif thủ công xử lý tool_calls trong agent.py cũ.
"""

import base64
import json
import mimetypes
import threading
from types import SimpleNamespace
from typing import Optional

from agent.state import PlanState, ToolState, TurnState
from context.compactor import Compactor
from memory.event_store import EventStore
from memory.manager import MemoryManager
from model import llm
from tools import registry
from tools.base import ToolResult

def encode_image(path: str) -> tuple[str, str]:
    """Đọc ảnh -> (mime_type, base64) để gửi cho model qua content parts."""
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type is None:
        mime_type = "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return mime_type, b64


def build_user_content(text: str, image_paths: list[str] | None = None):
    """
    Không có ảnh: giữ nguyên string như cũ (tương thích mọi model).
    Có ảnh: trả list OpenAI content parts [text, image_url, ...].
    """
    if not image_paths:
        return text

    content = [{"type": "text", "text": text}]
    for path in image_paths:
        mime_type, b64 = encode_image(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{b64}"},
        })
    return content


class StreamedToolCall:
    """Gom các mảnh của một tool call từ streaming response."""

    def __init__(self):
        self.id = ""
        self.type = "function"
        self.function = SimpleNamespace(
            name="",
            arguments="",
        )

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }

SYSTEM_PROMPT_TEMPLATE = """\
Bạn là 1 coding agent cá nhân, có quyền đọc/ghi/sửa file và chạy lệnh shell \
thông qua các tool được cung cấp.

--- PROJECT.md (facts về project) ---
{project_md}

--- Trạng thái turn hiện tại ---
{turn_state}

--- Plan ---
{plan_state}

--- Tool state ---
{tool_state}
"""


class Agent:
    def __init__(
        self,
        events_path: str,
        model: Optional[str] = None,
        workdir: str = ".",
    ):
        self.model = model
        self.event_store = EventStore(path=events_path)
        self.memory_manager = MemoryManager(model=model)
        self.compactor = Compactor(model=model)
        self.tool_state = ToolState(cwd=workdir)
        self.plan_state = PlanState()
        self.turn_state: Optional[TurnState] = None

    def _build_system_message(self) -> dict:
        content = SYSTEM_PROMPT_TEMPLATE.format(
            project_md=self.memory_manager.read(),
            turn_state=self.turn_state.render() if self.turn_state else "(no active turn)",
            plan_state=self.plan_state.render(),
            tool_state=self.tool_state.render(),
        )
        return {"role": "system", "content": content}

    def _build_context(self) -> list[dict]:
        """
        Ráp messages gửi model mỗi vòng lặp:
          1. system message MỚI mỗi lần gọi (chứa state hiện tại — luôn cập
             nhật, không nằm trong events.jsonl, không bị Compactor đụng vào)
          2. history từ EventStore (có thể bị Compactor nén nếu quá dài)
        """
        system_message = self._build_system_message()
        history = [m for m in self.event_store.to_messages() if m["role"] != "system"]
        messages = [system_message] + history

        if self.compactor.should_compact(messages):
            messages = self.compactor.compact(messages)

        return messages

    def _consume_stream(self, response):
        """
        Đọc streaming response, in text ngay và ghép các mảnh tool call.
        """
        full_text = ""
        tool_calls_by_index = {}
        started_printing = False
        try:
            for chunk in response:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # Nếu chunk chứa text, in ngay ra terminal.
                content = getattr(delta, "content", None)

                if content:
                    if not started_printing:
                        print(
                            "\nassistant> ",
                            end="",
                            flush=True,
                        )
                        started_printing = True

                    print(content, end="", flush=True)
                    full_text += content

                # Nếu chunk chứa một phần tool call, ghép nó lại.
                delta_tool_calls = (
                    getattr(delta, "tool_calls", None) or []
                )

                for tool_call_delta in delta_tool_calls:
                    index = getattr(
                        tool_call_delta,
                        "index",
                        None,
                    )

                    if index is None:
                        index = 0

                    if index not in tool_calls_by_index:
                        tool_calls_by_index[index] = (
                            StreamedToolCall()
                        )

                    accumulated = tool_calls_by_index[index]

                    tool_call_id = getattr(
                        tool_call_delta,
                        "id",
                        None,
                    )
                    if tool_call_id:
                        accumulated.id = tool_call_id

                    tool_call_type = getattr(
                        tool_call_delta,
                        "type",
                        None,
                    )
                    if tool_call_type:
                        accumulated.type = tool_call_type

                    function_delta = getattr(
                        tool_call_delta,
                        "function",
                        None,
                    )

                    if function_delta is not None:
                        function_name = getattr(
                            function_delta,
                            "name",
                            None,
                        )
                        if function_name:
                            accumulated.function.name += (
                                function_name
                            )

                        arguments = getattr(
                            function_delta,
                            "arguments",
                            None,
                        )
                        if arguments:
                            accumulated.function.arguments += (
                                arguments
                            )
        except KeyboardInterrupt:
            raise
        
        if started_printing:
            print()

        tool_calls = [
            tool_calls_by_index[index]
            for index in sorted(tool_calls_by_index)
        ]

        return full_text, tool_calls

    def run_turn(
        self,
        user_input: str,
        image_paths: list[str] | None = None,
        max_iterations: int = 20,
    ) -> str:
        """
        Chạy 1 turn: nhận user_input (+ ảnh nếu có), lặp tool-call cho tới khi
        model trả lời text thuần (không còn tool_call) hoặc chạm max_iterations.
        """
        self.turn_state = TurnState(goal=user_input)
        self.event_store.append(
            role="user",
            content=build_user_content(user_input, image_paths),
        )

        final_text = ""

        for _ in range(max_iterations):
            try:
                messages = self._build_context()
                response = llm.complete(
                    messages=messages,
                    tools=registry.get_schemas(),
                    model=self.model,
                    stream=True,
                )
                response_text, tool_calls = self._consume_stream(response)
            except KeyboardInterrupt:
                print("\n[cancelled by user]", flush=True)
                self.turn_state.status = "failed"
                self.turn_state.current_problem = "Turn cancelled by user"
                return final_text

            if tool_calls:
                self.event_store.append(
                    role="assistant",
                    content=response_text or None,
                    tool_calls=[
                        tool_call.model_dump()
                        for tool_call in tool_calls
                    ],
                )
                interrupted = False
                for tool_call in tool_calls:
                    if interrupted:
                        # các tool_call phía sau chưa kịp chạy -> vẫn phải đóng lại
                        self.event_store.append(
                            role="tool",
                            tool_call_id=tool_call.id,
                            content="Cancelled by user (not executed)",
                        )
                        continue
                    try: 
                        print(
                            f"\ntool> {tool_call.function.name}",
                            flush=True,
                        )
                        result = self._execute_tool_call(tool_call)

                    except KeyboardInterrupt:
                        interrupted = True
                        self.event_store.append(
                            role="tool",
                            tool_call_id=tool_call.id,
                            content="Cancelled by user",
                        )
                        continue
                    self.event_store.append(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content=result.compact,
                    )

                    if not result.success:
                        self.turn_state.current_problem = result.compact
                if interrupted:
                    self.turn_state.status = "failed"
                    self.turn_state.current_problem = "Turn cancelled by user"
                    return final_text
                
                continue  # loop lại để model đọc kết quả tool và quyết định bước tiếp

            # Không còn tool_call -> model trả lời text cuối cùng cho turn này
            final_text = response_text
            self.event_store.append(
                role="assistant", 
                content=final_text
            )
            self.turn_state.status = "done"
            break
        else:
            self.turn_state.status = "failed"
            self.turn_state.current_problem = f"Reached max_iterations ({max_iterations})"

        # Cập nhật memory bền vững khi turn hoàn thành — chạy trong background
        # thread (daemon) để KHÔNG block vòng lặp chính: update() gọi thêm 1
        # LLM call nữa (stream=False, vài giây), nếu chạy đồng bộ sẽ tạo
        # khoảng chờ im lặng cuối mỗi turn. Turn sau có thể bắt đầu ngay.
        if self.turn_state.status == "done":
            recent = self._render_recent_for_memory()
            print("[memory] updating PROJECT.md...", flush=True)
            threading.Thread(
                target=self._update_memory_bg,
                args=(recent,),
                daemon=True,
            ).start()

        return final_text

    def _update_memory_bg(self, recent: str):
        """Chạy trong thread nền: gọi LLM sinh diff rồi append vào file memory."""
        try:
            diff = self.memory_manager.update(recent)
            project_added = diff.get("project_md_append") or ""
            if project_added:
                print(
                    flush=True,
                )
            else:
                print(flush=True)
        except Exception as e:
            print(f"[memory] update failed: {e}", flush=True)

    def _execute_tool_call(self, tool_call) -> ToolResult:
        tool = registry.get_tool(tool_call.function.name)

        if tool is None:
            return ToolResult(
                raw=f"Unknown tool: {tool_call.function.name}",
                compact=f"Error: tool '{tool_call.function.name}' does not exist.",
                success=False,
            )

        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            return ToolResult(
                raw=f"Invalid JSON arguments: {tool_call.function.arguments}",
                compact="Error: tool call arguments are not valid JSON.",
                success=False,
            )

        result = tool.run(**args)

        # Track file đã sửa trong turn -> nuôi TurnState.files_touched,
        # cũng là input hữu ích cho MemoryManager khi update PROJECT.md.
        if tool.name in ("write", "edit") and "path" in args:
            self.turn_state.mark_file_touched(args["path"])

        return result

    def _render_recent_for_memory(self, last_n: int = 20) -> str:
        messages = self.event_store.to_messages()[-last_n:]
        lines = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                content = llm.content_to_text(content)
            lines.append(f"{m['role']}: {content}")
        return "\n".join(lines)