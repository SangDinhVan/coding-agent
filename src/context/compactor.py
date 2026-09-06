from typing import Optional

from model import llm

# Giữ nguyên N message gần nhất khi compact — KHÔNG tóm tắt phần này, vì đây
# là ngữ cảnh trực tiếp liên quan hành động agent sắp làm tiếp theo.
KEEP_RECENT_MESSAGES = 10

# Ngưỡng compact: kích hoạt khi tổng token > tỉ lệ này so với context window
# của model đang dùng.
COMPACT_THRESHOLD_RATIO = 0.70

SUMMARY_PROMPT_TEMPLATE = """\
Dưới đây là 1 đoạn hội thoại giữa user và 1 coding agent (bao gồm cả tool \
call và tool result). Hãy tóm tắt lại NGẮN GỌN nhưng đầy đủ các thông tin \
quan trọng để agent có thể tiếp tục làm việc mà không mất ngữ cảnh:

- Mục tiêu/task user đang yêu cầu
- Những gì đã làm được (file đã đọc/sửa, lệnh đã chạy, kết quả)
- Quyết định/ràng buộc quan trọng đã thống nhất trong đoạn hội thoại này
- Vấn đề/lỗi đang gặp phải (nếu có), đã thử cách nào rồi mà chưa được

KHÔNG cần tóm tắt lại từng bước chi tiết, chỉ giữ lại thông tin cần thiết để \
agent tiếp tục đúng hướng.

--- Đoạn hội thoại cần tóm tắt ---
{conversation}
--- Hết đoạn hội thoại ---

Tóm tắt:"""


class Compactor:
    def __init__(self, model: Optional[str] = None):
        self.model = model

    def should_compact(self, messages: list[dict]) -> bool:
        total_tokens = llm.count_messages_tokens(messages, self.model)
        context_window = llm.get_context_window(self.model)
        return total_tokens > context_window * COMPACT_THRESHOLD_RATIO

    def compact(self, messages: list[dict]) -> list[dict]:
        """
        Nhận list messages đầy đủ, trả về list messages đã rút gọn:
        [system messages giữ nguyên] + [1 summary message] + [N message gần nhất]

        Nếu chưa vượt ngưỡng, hoặc không đủ message để tách phần giữa ra tóm
        tắt -> trả về nguyên messages, KHÔNG gọi LLM tốn tiền vô ích.
        """
        if not self.should_compact(messages):
            return messages

        system_messages = [m for m in messages if m["role"] == "system"]
        non_system = [m for m in messages if m["role"] != "system"]

        if len(non_system) <= KEEP_RECENT_MESSAGES:
            return messages

        groups = self._interaction_groups(non_system)
        recent_groups = []
        recent_count = 0
        while groups and recent_count < KEEP_RECENT_MESSAGES:
            group = groups.pop()
            recent_groups.append(group)
            recent_count += len(group)
        recent_groups.reverse()
        to_summarize = [message for group in groups for message in group]
        recent = [message for group in recent_groups for message in group]

        if not to_summarize:
            return messages

        conversation_text = self._render_for_summary(to_summarize)
        summary_text = llm.complete_text(
            SUMMARY_PROMPT_TEMPLATE.format(conversation=conversation_text),
            model=self.model,
        )

        summary_message = {
            "role": "system",
            "content": f"[Tóm tắt phần hội thoại trước đó]\n{summary_text}",
        }

        return system_messages + [summary_message] + recent

    def _interaction_groups(self, messages: list[dict]) -> list[list[dict]]:
        """Keep an assistant tool-call and all of its tool results atomic."""
        groups = []
        index = 0
        while index < len(messages):
            message = messages[index]
            tool_calls = message.get("tool_calls") or []
            if message.get("role") != "assistant" or not tool_calls:
                groups.append([message])
                index += 1
                continue
            expected_ids = {call.get("id") for call in tool_calls}
            group = [message]
            index += 1
            while index < len(messages):
                candidate = messages[index]
                if candidate.get("role") != "tool" or candidate.get("tool_call_id") not in expected_ids:
                    break
                group.append(candidate)
                expected_ids.discard(candidate.get("tool_call_id"))
                index += 1
                if not expected_ids:
                    break
            groups.append(group)
        return groups

    def _render_for_summary(self, messages: list[dict]) -> str:
        """Render messages thành text đơn giản để đưa vào prompt tóm tắt."""
        lines = []
        for m in messages:
            role = m["role"]
            content = m.get("content") or ""
            if isinstance(content, list):
                content = llm.content_to_text(content)
            if m.get("tool_calls"):
                tool_names = [tc["function"]["name"] for tc in m["tool_calls"]]
                content += f" [tool_calls: {', '.join(tool_names)}]"
            lines.append(f"{role}: {content}")
        return "\n".join(lines)