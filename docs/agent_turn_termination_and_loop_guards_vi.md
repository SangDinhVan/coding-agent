# Agent Turn: cách kết thúc và chặn vòng lặp vô hạn

Tài liệu này mô tả vòng đời của một **turn** trong coding agent: khi nào agent kết thúc, khi nào tạm dừng, và các guardrail đang dùng để ngăn agent lặp vô hạn.

## 1. Turn là gì?

Một **turn** là một lần agent xử lý yêu cầu của người dùng:

```text
User gửi yêu cầu
  → agent gọi LLM để quyết định
  → có thể gọi tool (đọc file, sửa file, chạy test...)
  → LLM nhận kết quả tool và quyết định tiếp
  → kết thúc hoặc dừng do lỗi/ngắt
```

Vòng lặp chính nằm trong [`Agent._drive_turn()`](../src/agent/loop.py), được khởi động bởi [`Agent.run_turn()`](../src/agent/loop.py).

## 2. Các trạng thái kết thúc của turn

Reducer định nghĩa ba trạng thái terminal:

```python
_TERMINAL_TURNS = {
    TurnStatus.COMPLETED,
    TurnStatus.INTERRUPTED,
    TurnStatus.FAILED,
}
```

| Trạng thái | Ý nghĩa | Khi nào xảy ra? |
| --- | --- | --- |
| `COMPLETED` | Hoàn thành bình thường | LLM trả lời cuối và không còn điều kiện chặn completion. |
| `INTERRUPTED` | Bị người dùng ngắt | Người dùng nhấn `Ctrl+C` trong lúc model/tool đang chạy. |
| `FAILED` | Không thể tiếp tục an toàn | Model lỗi, hết iteration budget, hoặc liên tiếp cố kết thúc khi chưa xong plan. |

Khi reducer nhận một trong các event `TurnCompleted`, `TurnInterrupted`, hoặc `TurnFailed`, nó đều đặt:

```python
state.active_turn_id = None
```

Điều này giải phóng agent để turn mới có thể bắt đầu.

## 3. Một iteration hoạt động như thế nào?

Một iteration **không đồng nghĩa với đúng một tool call**. Nó là một lượt LLM được hỏi: “Bước tiếp theo là gì?”. Model có thể trả lời text hoặc gọi một/nhiều tool trong lượt đó.

```text
Đầu iteration
  → ghi event TurnIterationAdvanced
  → gọi LLM với context hiện tại
  ├─ LLM yêu cầu tool
  │   → chạy tất cả tool calls
  │   → lưu tool result
  │   → continue: quay lại iteration tiếp theo
  └─ LLM không gọi tool
      → coi text là câu trả lời cuối dự kiến
      → kiểm tra completion blockers
```

Ví dụ một task sửa bug có thể đi theo chuỗi:

```text
Iteration 1: đọc file implementation
Iteration 2: đọc test liên quan
Iteration 3: sửa code
Iteration 4: chạy test
Iteration 5: đọc lỗi test
Iteration 6: sửa lại
Iteration 7: chạy test lại
Iteration 8: trả lời tổng kết
```

## 4. Kết thúc bình thường: `COMPLETED`

Khi LLM không gọi tool, agent lấy response text làm ứng viên trả lời cuối:

```python
final_text = response_text
self._event("CompletionRequested", ...)
blockers = completion_blockers(self.runtime_state, turn_id)
```

Nếu `blockers` rỗng, agent mới được phép:

1. Lưu final assistant message.
2. Ghi event `TurnCompleted`.
3. Để reducer đổi trạng thái turn thành `COMPLETED`.

```text
RUNNING → COMPLETED
```

### Completion blockers kiểm tra gì?

[`completion_blockers()`](../src/runtime/reducer.py) chặn việc kết thúc nếu:

| Blocker | Ý nghĩa |
| --- | --- |
| `missing_required_plan` | Turn yêu cầu plan nhưng model chưa tạo plan. |
| `unfinished_required_steps` | Plan còn bước bắt buộc chưa `COMPLETED` hoặc `SKIPPED`. |
| `unresolved_tool_executions` | Có tool đang chờ approval, đang chạy, hoặc cần recovery. |

Mục tiêu là ngăn model chỉ nói “đã xong” khi trạng thái hệ thống chứng minh rằng công việc chưa an toàn để kết thúc.

## 5. Tạm dừng: chờ approval không phải là end

Nếu một tool yêu cầu người dùng phê duyệt, execution có trạng thái:

```text
WAITING_APPROVAL
```

Agent return khỏi `_drive_turn()` nhưng **không** phát event kết thúc turn. Turn vẫn ở trạng thái `RUNNING`, đồng thời `active_turn_id` vẫn tồn tại.

```text
RUNNING
  → tool yêu cầu approval
  → WAITING_APPROVAL
  → _drive_turn() return tạm thời
  → người dùng approve/reject
  → resolve_approval(...)
  → resume_active_turn(...)
```

Khi đang có active turn, [`Agent.run_turn()`](../src/agent/loop.py) từ chối mở turn mới. Điều này tránh hai turn cùng thao tác trên một workspace.

## 6. Chặn loop dài bằng `max_iterations = 20`

`run_turn()` dùng giá trị mặc định:

```python
max_iterations: int = 20
```

và `_drive_turn()` giới hạn mỗi lần chạy bằng:

```python
for _ in range(max_iterations):
```

Nếu LLM liên tục gọi tool hoặc tiếp tục suy nghĩ mà không đi đến completion, sau iteration thứ 20, agent phát `TurnFailed` với lỗi `max_iterations`.

```text
RUNNING → FAILED (max_iterations)
```

### Tại sao cần giới hạn này?

Không có giới hạn, LLM có thể lặp hành vi không tạo tiến triển:

```text
đọc file A → đọc file A lần nữa → chạy cùng test → chạy lại cùng test → ...
```

Hệ quả:

- Tốn token và chi phí API.
- Tăng thời gian chờ.
- Có thể tạo nhiều sửa đổi không cần thiết.
- Khó phát hiện agent đã bị kẹt.

### Vì sao chọn 20?

`20` là **heuristic/budget thực dụng**, không phải một con số bắt buộc về mặt kỹ thuật. Nó đủ cho một tác vụ coding thông thường gồm đọc code, sửa, test, và sửa lại; nhưng không quá cao để agent kẹt lâu mới bị dừng.

| Ngưỡng thấp như 3–5 | Ngưỡng cao như 100 |
| --- | --- |
| Nhiều task thật bị fail khi chưa kịp khám phá, sửa và test. | Tốn nhiều thời gian/chi phí trước khi phát hiện loop. |

> `20 iteration` là 20 lần gọi LLM để ra quyết định tiếp theo, không phải chính xác 20 tool calls. Một LLM response có thể yêu cầu nhiều tool calls.

## 7. Chặn “kết thúc sai” bằng 3 completion blockers liên tiếp

Khi model muốn trả lời cuối nhưng blockers vẫn tồn tại, agent ghi event `CompletionBlocked` và quay lại loop.

```python
if blockers:
    self._event("CompletionBlocked", ...)
    if self.turn_state.completion_block_count >= 3:
        self._event("TurnFailed", ...)
        return ""
    continue
```

Nếu điều này xảy ra 3 lần mà agent không tiến triển, turn bị fail với:

```text
repeated_incomplete_plan
```

### Vì sao không fail ngay blocker đầu tiên?

Lần đầu model có thể chỉ quên một bước. Nó nên được cơ hội đọc feedback, gọi tool, cập nhật plan, rồi tiếp tục làm việc:

```text
LLM: “Xong rồi”
System: còn step_3
LLM: nhận ra thiếu bước, chạy test và hoàn thành step_3
LLM: trả lời cuối
```

### Vì sao chỉ 3 thay vì 20?

Lặp tool có thể là công việc hữu ích; lặp “tôi đã xong” khi state báo chưa xong thường là dấu hiệu model không hội tụ.

```text
Lần 1: nhận feedback và sửa hướng
Lần 2: thêm một cơ hội tự sửa
Lần 3: vẫn không tiến triển → fail an toàn
```

Đây cũng là **3 blocker liên tiếp không có tiến triển**. Counter được reset về `0` khi plan tiến triển hoặc một tool đi đến kết quả terminal, ví dụ tool `COMPLETED`, `FAILED`, `REJECTED`, hoặc `CANCELLED`.

## 8. Hai lớp bảo vệ phối hợp với nhau

```text
Agent có tiến triển hữu ích?
  │
  ├─ Có: đọc/sửa/test/cập nhật plan
  │    → cho tối đa 20 lượt quyết định trong một lần _drive_turn()
  │
  └─ Không: liên tiếp cố kết thúc khi state báo chưa xong
       → cho tối đa 3 completion blocks liên tiếp
       → fail sớm
```

| Guardrail | Chặn loại loop nào? | Lý do |
| --- | --- | --- |
| `max_iterations = 20` | Loop làm việc dài: gọi tool/suy nghĩ mãi không xong. | Bảo vệ thời gian, token và chi phí. |
| `completion_block_count >= 3` | Loop kết thúc sai: liên tục nói “xong” dù plan/tool chưa được giải quyết. | Dừng sớm vì hành vi này hiếm khi tự tạo tiến triển. |

## 9. Lưu ý thiết kế hiện tại: resume có budget mới

`max_iterations` hiện giới hạn **mỗi lần gọi** `_drive_turn()`, không phải toàn bộ vòng đời turn.

Nếu tool cần approval, sau khi người dùng approve, code gọi:

```python
resume_active_turn(max_iterations=20)
```

Lần resume đó lại có tối đa 20 iteration mới. Vì vậy một turn có thể vượt 20 iteration tổng nếu nó bị pause/resume nhiều lần.

Trong khi đó `completion_block_count` thuộc `TurnState`, nên được giữ xuyên suốt turn (trừ khi có tiến triển làm reset counter).

> Nếu cần giới hạn cứng cho toàn bộ turn, có thể bổ sung kiểm tra dựa trên `turn.iteration` trước khi tạo iteration mới, thay vì chỉ dùng `for range` cục bộ. Hiện tại code chưa làm điều đó.

## 10. Tóm tắt để ghi nhớ

```text
COMPLETED   : câu trả lời cuối hợp lệ, không còn blocker.
INTERRUPTED : user chủ động ngắt bằng Ctrl+C.
FAILED      : model lỗi, chạm iteration limit, hoặc không hội tụ sau 3 blockers.
PAUSED      : không có enum TurnStatus riêng; thực tế turn vẫn RUNNING khi
              tool đang WAITING_APPROVAL hoặc RECOVERY_REQUIRED.

20 iterations : giới hạn làm việc dài trong một lần chạy/resume.
3 blockers    : giới hạn các lần liên tiếp cố kết thúc mà không tiến triển.
```
