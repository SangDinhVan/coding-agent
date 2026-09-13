# Coding Agent Harness — Ghi chú luồng hoạt động

## 1. Bức tranh tổng thể

Giả sử user yêu cầu:

> **“Sửa bug login trong project, sau đó chạy test để chắc chắn sửa đúng.”**

Harness sẽ quản lý công việc theo luồng:

```text
USER
"Sửa bug login + chạy test"
        │
        ▼
┌─────────────────────┐
│     TurnState       │
│ status = RUNNING    │
│ goal = sửa bug      │
└──────────┬──────────┘
           │
           ▼
      Model suy nghĩ
           │
           ▼
     Có cần PLAN?
        /      \
      không     có
       │         │
       │         ▼
       │      Plan
       │       │
       │       ├─ Step 1: tìm bug
       │       ├─ Step 2: sửa code
       │       └─ Step 3: chạy test
       │
       └──────────────┐
                      ▼
                 MODEL LOOP
                      │
               "Tôi muốn đọc file"
                      │
                      ▼
               ToolExecution
               status=PENDING
                      │
                      ▼
                PolicyDecision
               /      |      \
           ALLOW     ASK     DENY
             │        │       │
             │        │       └─ CANCELLED
             │        ▼
             │ WAITING_APPROVAL
             │        │
             │   user approve
             │        │
             └────────┘
                      ▼
                   RUNNING
                      │
                tool thực thi
                      │
           ┌──────────┼───────────┐
           ▼          ▼           ▼
       COMPLETED    FAILED    bị interrupt
                                   │
                                   ▼
                          RECOVERY_REQUIRED
```

Sau mỗi tool, kết quả quay lại model:

```text
Tool result
   │
   ▼
Model suy nghĩ tiếp
   │
   ▼
đề xuất action tiếp theo
```

Vòng lặp tiếp tục cho đến khi model yêu cầu hoàn thành và Harness xác nhận không còn blocker.

---

# 2. Turn — quản lý toàn bộ công việc

`TurnState` là hồ sơ của một yêu cầu user.

Ví dụ:

```text
TurnState
├─ turn_id = turn_001
├─ goal = "Sửa bug login + chạy test"
├─ status = RUNNING
├─ iteration = 0
└─ plan_mode = OPTIONAL
```

Turn trả lời các câu hỏi:

- Công việc user giao là gì?
- Công việc đang chạy hay đã xong?
- Đã chạy bao nhiêu vòng?
- Có plan hay không?
- Có lỗi hay không?

## TurnStatus

```text
                 START
                   │
                   ▼
                RUNNING
                   │
       ┌───────────┼────────────┐
       │           │            │
       ▼           ▼            ▼
  COMPLETED   INTERRUPTED      FAILED
```

- `RUNNING`: đang làm.
- `COMPLETED`: công việc hoàn tất hợp lệ.
- `INTERRUPTED`: công việc bị ngắt giữa chừng.
- `FAILED`: runtime kết luận công việc thất bại hoặc không thể tiếp tục.
- `IDLE`: chưa có công việc đang chạy.

---

# 3. Plan — quản lý những nghĩa vụ phải hoàn thành

Plan không bắt buộc cho mọi task.

Có hai chế độ:

```text
OPTIONAL
REQUIRED
```

Ví dụ task phức tạp:

```text
PLAN
│
├── Step 1: tìm nguyên nhân bug
├── Step 2: sửa login.py
└── Step 3: chạy test
```

Mỗi step có trạng thái:

```text
PENDING
   │
   ▼
IN_PROGRESS
   │
   ├──► COMPLETED
   │
   └──► FAILED
```

Ngoài ra có thể:

```text
PENDING / FAILED
      │
      ▼
   SKIPPED
```

## Ý nghĩa của `required`

Nếu một step có:

```text
required = True
```

thì Harness coi đó là nghĩa vụ phải được giải quyết trước khi task kết thúc.

Ví dụ:

```text
Step 1 = COMPLETED
Step 2 = COMPLETED
Step 3 = PENDING
```

Model nói:

> “Done.”

Harness vẫn không cho kết thúc vì Step 3 chưa hoàn thành.

---

# 4. CompletionPolicy — hoàn thành bằng lời nói hay bằng bằng chứng

Có hai kiểu:

## SELF_ATTESTED

Model tự xác nhận:

> “Tôi đã phân tích xong nguyên nhân bug.”

Phù hợp với các step thiên về reasoning.

## EVIDENCE_REQUIRED

Model phải có bằng chứng từ ToolExecution.

Ví dụ:

```text
Step: Chạy test

completion_policy = EVIDENCE_REQUIRED
```

Muốn hoàn thành step này cần có kiểu bằng chứng:

```text
ToolExecution:
pytest

status = COMPLETED
result.success = True
```

Sau đó step có thể lưu:

```text
evidence_execution_ids = ["exec_003"]
```

Cách nhớ:

```text
SELF_ATTESTED
= "Tôi nói tôi làm xong."

EVIDENCE_REQUIRED
= "Cho tôi bằng chứng execution."
```

---

# 5. ToolExecution — quản lý một lần thực thi tool

Model không trực tiếp sửa file hay chạy shell.

Model chỉ nói:

> “Tôi muốn gọi tool này.”

Harness tạo một `ToolExecution`.

Ví dụ:

```text
execution_id = exec_001
tool_name = read_file
arguments = {"path": "login.py"}
status = PENDING
```

Có thể hiểu:

> ToolExecution là hồ sơ của một lần thực thi tool.

Lifecycle cơ bản:

```text
PENDING
   │
   ▼
RUNNING
   │
   ├──► COMPLETED
   ├──► FAILED
   └──► RECOVERY_REQUIRED
```

---

# 6. Policy — có cho tool chạy hay không

Trước khi tool chạy, Harness kiểm tra policy.

Có ba quyết định:

```text
ALLOW
ASK
DENY
```

## ALLOW

Ví dụ:

```text
read_file
→ ALLOW
```

Tool chạy luôn.

## ASK

Ví dụ:

```text
delete_file("database.db")
→ ASK
```

Execution chuyển sang:

```text
WAITING_APPROVAL
```

User approve:

```text
WAITING_APPROVAL
       ↓
     PENDING
       ↓
     RUNNING
```

User reject:

```text
WAITING_APPROVAL
       ↓
    CANCELLED
```

## DENY

Tool bị từ chối ngay:

```text
PENDING
   ↓
CANCELLED
```

---

# 7. Tool result

Khi tool chạy xong, Harness lưu kết quả.

Ví dụ:

```text
ToolResultData
├─ raw = output đầy đủ
├─ compact = output rút gọn
├─ success = True
└─ exit_code = 0
```

Kết quả quay lại model để model quyết định bước tiếp theo.

Luồng agent cơ bản:

```text
MODEL
  │
  ▼
Action
  │
  ▼
HARNESS
  │
  ▼
TOOL
  │
  ▼
RESULT
  │
  └──────────────► MODEL
```

Đây chính là agent loop.

---

# 8. Khi nào task được dừng?

Model nói:

> “Tôi xong rồi.”

không có nghĩa task lập tức `COMPLETED`.

Ta phải phân biệt:

```text
Model nói xong
≠
Task đã xong
```

Model chỉ đang **request completion**.

Harness kiểm tra:

- Required plan step còn chưa xong không?
- Tool nào còn `RUNNING` không?
- Tool nào còn `WAITING_APPROVAL` không?
- Tool nào còn `RECOVERY_REQUIRED` không?
- Step cần evidence đã có evidence chưa?

Nếu còn vấn đề:

```text
CompletionBlocker
```

Ví dụ:

```text
code = "unfinished_required_steps"

message =
"Required plan steps are unfinished."

ids =
("step_3",)
```

Nếu:

```text
blockers = []
```

thì Harness mới cho:

```text
TurnStatus.COMPLETED
```

---

# 9. Thành công có hai cấp độ

## Tool thành công

Ví dụ:

```text
pytest
→ ToolExecution.COMPLETED
```

Chỉ có nghĩa:

> Lần thực thi tool đó đã hoàn tất.

## Turn thành công

Ví dụ:

```text
✓ tìm bug
✓ sửa code
✓ chạy test
✓ không còn blocker
```

thì:

```text
TurnStatus.COMPLETED
```

Cách nhớ:

```text
Tool COMPLETED
        ≠
Task COMPLETED
```

---

# 10. Interrupt khi model đang suy nghĩ

Ví dụ:

```text
Model đang generate...
User Ctrl+C
```

Nếu chưa có external side effect đang chạy:

```text
Turn → INTERRUPTED
```

Không phải `FAILED`.

Ý nghĩa:

> Công việc bị ngắt, nhưng state vẫn hợp lệ và có thể phục hồi hoặc tiếp tục theo cơ chế runtime.

---

# 11. Interrupt trước khi tool chạy

Nếu tool mới ở:

```text
PENDING
```

và user interrupt trước khi `START`:

```text
PENDING
   ↓
CANCELLED
```

Harness biết chắc:

> Tool chưa tạo side effect.

Đây là case dễ xử lý.

---

# 12. Interrupt đúng lúc tool đang chạy

Đây là case quan trọng.

Ví dụ:

```text
ToolStarted
    ↓
write_file("login.py")
    ↓
Ctrl+C / process crash
```

Harness không biết chắc:

- File đã sửa xong?
- Chưa sửa?
- Hay sửa một phần?

Không được tự kết luận:

```text
FAILED
```

và cũng không được tự kết luận:

```text
COMPLETED
```

Thay vào đó:

```text
RUNNING
   ↓
RECOVERY_REQUIRED
```

Ý nghĩa:

> Tool đã bắt đầu, nhưng outcome cuối cùng chưa xác định.

---

# 13. ReplayPolicy — sau crash có được chạy lại tool không?

Có ba loại:

## REPLAY_SAFE

Chạy lại an toàn.

Ví dụ:

```text
read_file
search
git status
```

Có thể retry.

## RECONCILABLE

Không retry ngay.

Harness kiểm tra trạng thái môi trường.

Ví dụ `edit_file`:

```text
before state
expected after state
current state
```

Nếu current giống expected after:

```text
RecoveryDecision.COMPLETED
```

Nếu current vẫn giống before:

```text
RecoveryDecision.RETRY
```

Nếu biết operation thất bại:

```text
RecoveryDecision.FAILED
```

## MANUAL

Harness không được tự quyết định.

Phù hợp với action nguy hiểm hoặc side effect không thể tự xác minh.

Ví dụ:

```text
deploy
gửi giao dịch
xóa dữ liệu quan trọng
```

---

# 14. RecoveryDecision

Chỉ dùng khi tool đang:

```text
RECOVERY_REQUIRED
```

Có ba kết quả:

```text
COMPLETED
= kiểm tra và xác nhận lần trước thực ra đã thành công

FAILED
= xác nhận operation thất bại

RETRY
= xác nhận có thể chạy lại
```

---

# 15. PendingRuntimeAction

Sau restart, Harness có thể phát hiện runtime còn việc cần xử lý.

Ví dụ:

```text
Tool A = WAITING_APPROVAL
```

thì tạo pending action:

```text
kind = approval
```

Hoặc:

```text
Tool B = RECOVERY_REQUIRED
```

thì:

```text
kind = recovery
```

Có thể hiểu:

> PendingRuntimeAction là danh sách các việc runtime phải giải quyết trước khi tiếp tục.

Nó khác với Plan:

```text
Plan
= việc model phải làm để hoàn thành goal.

PendingRuntimeAction
= việc runtime phải giải quyết để hệ thống tiếp tục an toàn.
```

---

# 16. RuntimeEvent — lịch sử đã xảy ra

Trong quá trình task chạy, Harness ghi event:

```text
seq 1  TurnStarted
seq 2  PlanCreated
seq 3  PlanStepStarted
seq 4  ToolRequested
seq 5  ToolStarted
seq 6  ToolCompleted
seq 7  PlanStepCompleted
...
seq 20 TurnCompleted
```

Mỗi dòng là một `RuntimeEvent`.

Có thể hiểu:

> RuntimeEvent là nhật ký “đã xảy ra chuyện gì”.

---

# 17. RuntimeState — trạng thái hiện tại

Sau tất cả event:

```text
RuntimeState
│
├── Turn
│   └── turn_001 = COMPLETED
│
├── Plan
│   └── plan_001
│       ├── step_1 COMPLETED
│       ├── step_2 COMPLETED
│       └── step_3 COMPLETED
│
└── Executions
    ├── exec_001 read_file COMPLETED
    ├── exec_002 edit_file COMPLETED
    └── exec_003 pytest COMPLETED
```

Cách nhớ:

```text
RuntimeEvent
= video / lịch sử

RuntimeState
= ảnh chụp hiện tại
```

---

# 18. PlanRevision

Plan có thể thay đổi.

Ban đầu:

```text
Revision 1
├─ sửa login.py
└─ test
```

Sau khi model đọc code và phát hiện thêm dependency:

```text
Revision 2
├─ sửa login.py
├─ sửa auth_service.py
└─ test
```

`PlanRevision` giúp giữ lịch sử:

- Ban đầu model định làm gì?
- Tại sao thay đổi plan?
- Plan hiện tại là revision nào?

---

# 19. Status và Transition khác nhau thế nào?

Cách nhớ:

```text
STATUS
= Tôi đang ở đâu?

TRANSITION
= Tôi muốn chuyển trạng thái bằng hành động gì?
```

Ví dụ:

```text
TurnStatus.RUNNING
+
TurnTransition.COMPLETE
=
TurnStatus.COMPLETED
```

Plan step:

```text
PlanStepStatus.PENDING
+
PlanStepTransition.START
=
PlanStepStatus.IN_PROGRESS
```

Tool:

```text
ToolExecutionStatus.PENDING
+
ToolTransition.START
=
ToolExecutionStatus.RUNNING
```

---

# 20. Gom toàn bộ hệ thống thành 5 tầng

```text
1. TURN
   "Công việc user giao là gì?"

            ↓

2. PLAN
   "Để hoàn thành cần làm những gì?"

            ↓

3. TOOL EXECUTION
   "Model muốn tương tác với máy tính như thế nào?"

            ↓

4. EVENT
   "Thực tế đã xảy ra những gì?"

            ↓

5. RUNTIME STATE
   "Hiện giờ toàn bộ hệ thống đang ở đâu?"
```

Harness bao quanh toàn bộ:

```text
                   HARNESS
        ┌─────────────────────────┐
        │                         │
USER → TURN → PLAN → MODEL        │
                    ↓            │
                 ACTION          │
                    ↓            │
               POLICY            │
                    ↓            │
              TOOL EXECUTION     │
                    ↓            │
                 RESULT          │
                    ↓            │
                  EVENT          │
                    ↓            │
             RUNTIME STATE       │
                    │            │
                    └──→ MODEL   │
                                 │
        Completion Check ────────┘
```

---

# 21. Câu chốt cần nhớ

**Turn** quản lý **toàn bộ công việc**.

**Plan** quản lý **những nghĩa vụ phải hoàn thành**.

**ToolExecution** quản lý **một hành động thực tế trên môi trường**.

**Policy** quyết định **có được phép thực thi không**.

**Replay/Recovery** quyết định **nếu bị ngắt thì xử lý tool thế nào**.

**RuntimeEvent** ghi lại **lịch sử đã xảy ra**.

**RuntimeState** cho biết **hiện tại đang ở đâu**.

**CompletionBlocker** ngăn model **dừng khi công việc chưa thật sự hoàn tất**.

Tư tưởng quan trọng nhất:

> **LLM có thể suy nghĩ tự do, nhưng execution và completion phải tuân theo luật của Harness.**

Hay nói ngắn hơn:

```text
Model quyết định "muốn làm gì".

Harness quyết định:
- có được làm không,
- làm như thế nào,
- đã thực sự làm xong chưa,
- khi nào toàn bộ task được phép kết thúc.
```
