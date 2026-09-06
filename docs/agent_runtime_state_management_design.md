# Agent Runtime State Management — Design Specification

> **Trạng thái:** Implemented và verified trên branch `feat/runtime-state-management`  
> **Phạm vi:** Đặc tả V1; implementation nằm trong `src/runtime`, runtime harness, tool adapters và CLI.
>
> **Làm rõ sau implementation:** secret-like keys được redacted đệ quy trước khi ghi journal; giá trị đã redacted không được tự động replay sau restart. File write/edit dùng SHA-256 để reconcile; arbitrary bash vẫn yêu cầu quyết định thủ công khi outcome không xác định.

## Mục tiêu và quyết định đã chốt

Thiết kế nâng cấp runtime với bốn khả năng:

1. lifecycle có transition xác định;
2. plan có cấu trúc và được harness enforce;
3. state có thể dựng lại sau khi process dừng;
4. mỗi lần thực thi tool có lifecycle, audit trail và recovery policy riêng.

Lõi suy luận hiện tại được giữ nguyên:

```text
                         ┌─────────────────────────────┐
                         │       RUNTIME HARNESS       │
                         │                             │
User ──► Agent.run_turn ─┤  LLM decides intent        │
                         │  Runtime validates effects │
                         └──────────────┬──────────────┘
                                        │
                                        ▼
                                  call the LLM
                                        │
                                ┌───────┴────────┐
                                │ tool_calls?    │
                                └───────┬────────┘
                                    yes │ no
                  ┌─────────────────────┘ └────────────────────┐
                  ▼                                            ▼
       validate / approve / execute                 request turn completion
                  │                                            │
                  ▼                                            ▼
       persist tool outcome                         deterministic plan guard
                  │                                      │              │
                  ▼                                   allow           block
       feed result back to LLM                         │              │
                  │                                    ▼              ▼
                  └──────────── loop ◄──────── final response   explain block
                                                                       │
                                                                       └── loop
```

Không đưa planning, coding, debugging hay reviewing vào Turn state. Không thay
loop bằng `Planner → Coder → Reviewer`. State machine chỉ quản lý các runtime
entity cần tính xác định.

Quyết định đã chốt:

- Plan mặc định `OPTIONAL`.
- User/caller có thể đặt `REQUIRED`.
- Model có thể tự tạo plan qua một runtime control tool có schema.
- Khi plan đã tồn tại, harness enforce transition và completion guard.
- Tool đã bắt đầu nhưng mất terminal event trở thành `RECOVERY_REQUIRED`, không
  bị đoán là failed.
- Không tự retry side effect có outcome chưa biết, đặc biệt là arbitrary shell.
- Runtime event journal là nguồn sự thật; prompt context chỉ là projection.

---

# A. Current Architecture Assessment

## A.1. Execution model hiện tại

Luồng chính nằm trong
[`Agent.run_turn()`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/loop.py#L241-L349):

```text
user input
    │
    ▼
append user message to EventStore
    │
    ▼
build context → llm.complete(stream=True)
    │
    ├── tool calls
    │      │
    │      ▼
    │   append assistant tool-call
    │      │
    │      ▼
    │   _execute_tool_call() → append compact ToolResult
    │      │
    │      └──────────────────────────► call model again
    │
    └── no tool calls
           │
           ▼
       append final assistant message → status = "done"
```

Đây đã là model-driven loop đúng hướng. Nên giữ quyết định lặp dựa trên
`tool_calls`, việc đưa tool result lại cho model và completion path khi model
không gọi tool nữa.

## A.2. Runtime state hiện tại

[`TurnState`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/state.py#L43-L69):

- status là string tự do `running | done | failed`;
- không có `turn_id`, timestamps hoặc iteration được persist;
- không có transition validation;
- `KeyboardInterrupt` bị gộp vào `failed`;
- restart tạo object mới, không reconstruct turn cũ.

[`PlanState`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/state.py#L14-L40):

- chỉ là `list[dict]`, status nhận string bất kỳ;
- không có plan ID, revision, required/optional hoặc evidence;
- không có guard chống xóa/skipping required work;
- ngoài khởi tạo và render prompt, code hiện tại không có call site tạo/cập nhật
  step;
- final response không kiểm tra plan nên plan chưa enforce gì.

[`ToolState`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/state.py#L72-L126)
không phải tool lifecycle. Nó đọc `cwd`, git branch và modified files. Vai trò
thực là workspace/environment snapshot; tên này sẽ gây nhầm với `ToolExecution`.

## A.3. Tool execution hiện tại

[`Agent._execute_tool_call()`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/loop.py#L365-L391):

```text
lookup tool → parse JSON → tool.run(**args) → ToolResult
```

Điểm yếu:

- không có execution identity độc lập với provider `tool_call_id`;
- không persist `ToolStarted` trước side effect;
- không phân biệt chưa chạy, chạy dở hay hoàn tất sau crash;
- không có approval/policy metadata;
- exception ngoài những exception từng tool tự bắt có thể làm vỡ turn;
- chưa có duplicate-execution protection.

[`BaseTool.run()`](file:///home/sang/working/TLCN/repo/coding-agent/src/tools/base.py#L51-L63)
có validation cơ bản và trả
[`ToolResult`](file:///home/sang/working/TLCN/repo/coding-agent/src/tools/base.py#L6-L20).
Contract này có thể giữ. Tuy nhiên docstring nói `raw` được ghi xuống history,
trong khi `run_turn()` chỉ persist `result.compact`.

## A.4. Persistence và resume hiện tại

[`EventStore`](file:///home/sang/working/TLCN/repo/coding-agent/src/memory/event_store.py#L91-L154)
là append-only JSONL nhưng schema map trực tiếp sang chat messages:

```json
{"seq": 1, "ts": "...", "role": "user", "content": "..."}
```

Nó tiếp tục sequence và dựng OpenAI-style messages, nhưng chưa thể trả lời:

- turn nào đang active;
- plan revision hiện tại là gì;
- tool nào đã bắt đầu;
- approval nào đã được cấp;
- side effect nào có outcome chưa biết;
- completion đã qua plan guard hay chưa.

[`main.py`](file:///home/sang/working/TLCN/repo/coding-agent/src/main.py#L21-L93)
“resume” theo nghĩa mở lại file chat, chưa reconstruct runtime hoặc recovery.

## A.5. Context, compaction và memory

[`Agent._build_context()`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/loop.py#L119-L133)
tạo system message mới với Turn/Plan/Tool state rồi ghép history. Đây là boundary
tốt: state có thể được project riêng thay vì nằm trong raw chat.

[`Compactor`](file:///home/sang/working/TLCN/repo/coding-agent/src/context/compactor.py#L42-L73)
chỉ rút gọn request gửi model, không sửa JSONL. Nhưng nó có thể chia đôi cặp
assistant tool-call/tool result và chưa có persisted checkpoint.

[`MemoryManager`](file:///home/sang/working/TLCN/repo/coding-agent/src/memory/manager.py#L54-L89)
quản lý project knowledge, không phải runtime state. Runtime correctness không
được phụ thuộc vào `PROJECT.md` hoặc daemon memory update.

## A.6. Các giả định xung đột

1. State tách khỏi messages thì không bị compact, nhưng vẫn mất khi process chết.
2. EventStore mới là conversation ledger, chưa phải runtime event store.
3. Tool failure hiện chỉ là `ToolResult.success=False`, chưa bao phủ exception,
   rejection, cancellation và unknown outcome.
4. “Không có tool call = done” phải đổi thành “model đề nghị completion”.
5. `KeyboardInterrupt = failed` làm mất khác biệt giữa lỗi và user interruption.

---

# B. Target Architecture

```text
┌─────────┐
│  USER   │
└────┬────┘
     │ request / approval / recovery decision
     ▼
┌───────────────────────────────────────────────────────────────┐
│                         CLI / CALLER                          │
│ render events, collect approval, choose recovery              │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                       Agent.run_turn()                        │
│  ┌──────────────── MODEL-DRIVEN LOOP ──────────────────────┐  │
│  │ build context → LLM → tool calls? → results → loop      │  │
│  └──────────────────────────┬───────────────────────────────┘  │
│                             │ proposed intent                  │
│  ┌──────────────────────────▼───────────────────────────────┐  │
│  │                 RUNTIME LIFECYCLE LAYER                  │  │
│  │ TurnMachine   Plan/StepMachine   ToolExecutionMachine    │  │
│  └──────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────┼─────────────────────────────────┘
                              │ append facts before side effects
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                SESSION RUNTIME EVENT JOURNAL                  │
│       append-only JSONL, total order, canonical truth         │
└─────────────────────────────┬─────────────────────────────────┘
                              │ replay
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                    REDUCER / PROJECTIONS                      │
│ RuntimeState     MessageProjection     RuntimePromptProjection │
└───────────┬──────────────────┬───────────────────┬─────────────┘
            │                  │                   │
            ▼                  ▼                   ▼
      resume/recovery      Compactor         system context
```

| Thành phần | Trách nhiệm |
|---|---|
| LLM | Đề xuất intent, tool call, plan update, final response |
| Agent loop | Điều phối model → tool → model, giữ iteration limit |
| State machines | Xác nhận transition hợp lệ |
| Plan guard | Chặn completion khi required work chưa hoàn thành |
| Tool executor | Validate, policy/approval, journal, execute, recovery |
| Event journal | Nguồn sự thật có thứ tự của session/runtime |
| Reducer | Dựng state từ event, không tạo side effect |
| Message projector | Chuyển message events thành provider messages |
| Compactor | Tối ưu prompt; không sở hữu runtime truth |
| MemoryManager | Project knowledge; không sở hữu runtime truth |
| CLI | Hiển thị và nhận approval/cancellation/recovery decision |

## State-machine core tối thiểu

Không dùng thư viện FSM ở V1. State/event dùng enum/dataclass; transitions là
một table trong code.

```text
incoming command/event
        │
        ▼
find (current_state, event_type) transition
        │
        ├── not found ──► reject; no mutation/effect
        ▼
evaluate pure guard(s)
        │
        ├── false ──────► TransitionRejected(reason)
        ▼
build accepted domain event
        │
        ▼
append + flush event durably
        │
        ├── failed ─────► fail closed
        ▼
exit action → transition action → update state → entry action
        │
        ▼
emit notification/projection update
```

Khác FSM in-memory thông thường, persistence phải xảy ra trước state mutation
và trước external side effect. Guard pure/deterministic; entry/exit/action không
được trực tiếp chạy shell, ghi file hoặc network. Khi replay, reducer áp event
mà không chạy live hooks.

---

# C. State Machines

## C.1. Turn lifecycle

```text
                         TurnStarted
             ┌──────┐ ───────────────► ┌─────────┐
             │ IDLE │                   │ RUNNING │
             └──────┘                   └────┬────┘
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    │                       │                       │
             TurnCompleted           TurnInterrupted          TurnFailed
                    ▼                       ▼                       ▼
             ┌───────────┐           ┌─────────────┐          ┌────────┐
             │ COMPLETED │           │ INTERRUPTED │          │ FAILED │
             └───────────┘           └─────────────┘          └────────┘
```

| Current | Event | Guard | Next |
|---|---|---|---|
| `IDLE` | `TurnStarted` | không có active turn | `RUNNING` |
| `RUNNING` | `TurnCompleted` | completion guard pass, final accepted | `COMPLETED` |
| `RUNNING` | `TurnInterrupted` | cancellation được tiếp nhận | `INTERRUPTED` |
| `RUNNING` | `TurnFailed` | có structured failure | `FAILED` |

Terminal states: `COMPLETED`, `INTERRUPTED`, `FAILED`. `CompletionRequested` và
`CompletionBlocked` chỉ là runtime events khi turn vẫn `RUNNING`.

### Completion guard

```text
model returns no tool_calls
          │
          ▼
CompletionRequested(candidate_text)
          │
          ▼
┌──────────────── completion guard ────────────────┐
│ PlanMode.REQUIRED and no plan?            BLOCK │
│ Active plan has unfinished required step? BLOCK │
│ Unresolved RECOVERY_REQUIRED tool?         BLOCK │
│ Otherwise?                                 ALLOW │
└──────────────────────┬───────────────────────────┘
                   allow│block
             ┌──────────┘ └─────────────┐
             ▼                          ▼
 persist final assistant        persist CompletionBlocked
 exactly once                    (reason + missing step IDs)
             │                          │
             ▼                          ▼
      TurnCompleted              feed blocker to LLM → loop
```

Candidate bị block không phải final assistant message. Vì
[`_consume_stream()`](file:///home/sang/working/TLCN/repo/coding-agent/src/agent/loop.py#L135-L239)
in ngay, CLI tương lai phải xem text là tentative; phương án đơn giản là buffer
candidate khi plan enforcement active.

Ba lần `CompletionBlocked` liên tiếp dẫn đến
`TurnFailed(reason="repeated_incomplete_plan")`. Global `max_iterations` vẫn là
giới hạn cuối.

### Cancellation/resume

- Interrupt khi stream → `TurnInterrupted`; partial text không phải final.
- Interrupt trước tool start → tool `CANCELLED`, turn `INTERRUPTED`.
- Interrupt khi tool chạy chỉ ghi `ToolCancelled` nếu xác nhận effect/process đã
  dừng; nếu không thì `RECOVERY_REQUIRED`.
- Clean interruption là terminal; tiếp tục bằng turn mới có `resumes_turn_id`.
- Crash để lại turn `RUNNING`; sau restart reconstruct và resolve unknown tool
  outcomes trước khi tiếp tục.

Persist: `turn_id`, goal, status events, timestamps, iteration, plan ID,
completion-block count, execution IDs, terminal result/error và
`runtime_instance_id`.

## C.2. Plan Step lifecycle

### Plan activation

```text
OPTIONAL + no plan ──► simple task may complete directly
OPTIONAL + plan created ──► plan enforcement becomes active
REQUIRED + no plan ──► completion blocked
REQUIRED + active plan ──► required steps must be resolved
```

Model thao tác plan qua một internal control tool:

```text
update_plan({
  "action": "create | set_step_status | revise",
  ...
})
```

Tool vẫn đi qua ToolExecution lifecycle để giữ tool-call protocol. Plan event
mang `causation_id=execution_id`, giúp recovery biết mutation đã áp dụng chưa.

### States và transitions

```text
                         ┌──────────────┐
                         │   PENDING    │
                         └──────┬───────┘
                                │ start
                                ▼
                         ┌──────────────┐
                 ┌───────│ IN_PROGRESS  │───────┐
                 │       └──────┬───────┘       │
                 │ fail         │ complete      │ revise/reset
                 ▼              ▼               ▼
          ┌────────────┐  ┌─────────────┐  ┌───────────┐
          │   FAILED   │  │  COMPLETED  │  │  PENDING  │
          └─────┬──────┘  └─────────────┘  └───────────┘
                │ retry
                └──────────────► IN_PROGRESS

PENDING / FAILED ── authorized skip ──► SKIPPED
```

| Current | Event | Guard | Next |
|---|---|---|---|
| `PENDING` | `PlanStepStarted` | không có step khác in-progress | `IN_PROGRESS` |
| `PENDING` | `PlanStepSkipped` | optional hoặc user override | `SKIPPED` |
| `IN_PROGRESS` | `PlanStepCompleted` | completion policy thỏa | `COMPLETED` |
| `IN_PROGRESS` | `PlanStepFailed` | có reason | `FAILED` |
| `IN_PROGRESS` | `PlanRevised` | revision giải thích reset | `PENDING` mới |
| `FAILED` | `PlanStepStarted` | retry được ghi nhận | `IN_PROGRESS` |
| `FAILED` | `PlanStepSkipped` | optional hoặc user override | `SKIPPED` |

`COMPLETED` và `SKIPPED` terminal trong cùng revision; `FAILED` chưa hoàn tất
required work. V1 có tối đa một step `IN_PROGRESS`; đây chỉ là bookkeeping,
không hạn chế model suy nghĩ.

### Completion policy

- `SELF_ATTESTED`: model complete kèm note; dùng cho phân tích/giải thích semantic.
- `EVIDENCE_REQUIRED`: phải tham chiếu successful execution IDs; dùng cho sửa
  file, chạy test hoặc work có outcome cấu trúc.

Runtime kiểm tra deterministic rằng evidence tồn tại, đúng context, terminal
`COMPLETED` và thỏa điều kiện cấu trúc. Runtime không giả vờ hiểu code đã “đẹp”
hay phân tích đã “đủ”; phần semantic vẫn là model/user attestation.

Required step resolved khi `COMPLETED`, hoặc `SKIPPED` bởi user/caller với reason.
`PENDING`, `IN_PROGRESS`, `FAILED` đều chặn completion. Model không thể tự xóa
required step, hạ `required=True`, hoặc skip required step.

### Plan revision

```text
Plan revision 1
   ├── step_1 COMPLETED
   ├── step_2 FAILED
   └── step_3 PENDING
             │ PlanRevised(reason, actor)
             ▼
Plan revision 2
   ├── step_1 COMPLETED   (preserved)
   ├── step_2a PENDING    (split from step_2)
   ├── step_2b PENDING
   └── step_3 PENDING
```

| Thao tác | Model | User/caller |
|---|---:|---:|
| Thêm step | Có | Có |
| Reword giữ nghĩa | Có | Có |
| Chia pending/failed step | Có | Có |
| Retry failed step | Có | Có |
| Xóa optional pending có reason | Có | Có |
| Xóa/skip required | Không | Có |
| Hạ required thành optional | Không | Có |
| Thay completed work | Chỉ qua revision mới | Có |

Revision ghi `from_revision`, `to_revision`, actor, reason và cấu trúc mới. Không
rewrite lịch sử.

## C.3. Tool Execution lifecycle

```text
                         ToolRequested
                               ▼
                         ┌───────────┐
                         │  PENDING  │
                         └─────┬─────┘
                               │ validate
                  ┌────────────┼─────────────┐
                  │ invalid    │ allow       │ approval required
                  ▼            │             ▼
              ┌────────┐       │     ┌──────────────────┐
              │ FAILED │       │     │ WAITING_APPROVAL │
              └────────┘       │     └────────┬─────────┘
                               │          reject│approve
                               │       ┌────────┘ └──────────┐
                               │       ▼                     │
                               │  ┌───────────┐              │
                               │  │ CANCELLED │              │
                               │  └───────────┘              │
                               └──────────────┬───────────────┘
                                              │ persist ToolStarted
                                              ▼
                                        ┌─────────┐
                                        │ RUNNING │
                                        └────┬────┘
                                             │
                   ┌─────────────────────────┼────────────────────────┐
                   │                         │                        │
             completed                    failed            outcome unknown
                   ▼                         ▼                        ▼
            ┌───────────┐              ┌────────┐       ┌───────────────────┐
            │ COMPLETED │              │ FAILED │       │ RECOVERY_REQUIRED │
            └───────────┘              └────────┘       └─────────┬─────────┘
                                                                  │
                                     ┌────────────────────────────┼─────────┐
                                     │ reconcile success          │ safe retry
                                     ▼                            ▼
                                COMPLETED                       PENDING
```

Terminal: `COMPLETED`, `FAILED`, `CANCELLED`. `RECOVERY_REQUIRED` non-terminal
và đủ để biểu diễn việc chờ recovery decision ở V1.

| Event | Ý nghĩa |
|---|---|
| `ToolRequested` | Cấp execution ID, tạo `PENDING` |
| `ToolValidated` | Tool/JSON/schema hợp lệ, args normalized |
| `ToolApprovalRequested` | `PENDING → WAITING_APPROVAL` |
| `ToolApproved` | Persist approval metadata |
| `ToolRejected` | `WAITING_APPROVAL → CANCELLED` |
| `ToolStarted` | Durable boundary trước effect, sang `RUNNING` |
| `ToolCompleted` | `ToolResult.success=True` |
| `ToolFailed` | Validation, exception hoặc unsuccessful result |
| `ToolCancelled` | Chắc chắn chưa chạy hoặc đã dừng |
| `ToolRecoveryRequired` | Started nhưng outcome không xác định |
| `ToolRecoveredAsCompleted` | Reconcile chứng minh effect xong |
| `ToolRecoveredAsFailed` | Reconcile chứng minh effect không xong |
| `ToolRetryScheduled` | Replay policy cho phép; tăng attempt |

Orchestrator bắt `Exception` quanh `tool.run()` thành structured `ToolFailed`.
`KeyboardInterrupt` xử lý riêng. Không biến `SystemExit`/fatal signal thành tool
failure giả.

```text
tool.run()
   ├── ToolResult(success=True)  ──► ToolCompleted
   ├── ToolResult(success=False) ──► ToolFailed
   ├── Exception                 ──► ToolFailed(error record)
   └── interruption
          ├── confirmed stopped  ──► ToolCancelled
          └── uncertain outcome  ──► ToolRecoveryRequired
```

Tool failure thường không crash turn; runtime tạo provider-facing tool result có
cùng `tool_call_id`, đưa lại cho LLM và loop tiếp tục. Journal failure là
infrastructure failure và phải fail closed.

V1 giữ multiple tool calls chạy tuần tự. Persist tất cả requests, sau đó execute
từng call và chỉ gọi lại model khi toàn batch có outcome hợp lệ.

---

# D. Persistence / Resume Design

## D.1. Source of truth

Một append-only runtime event journal cho mỗi session là canonical truth. Không
tách message log và runtime log ở V1 vì cần total ordering:

```text
UserMessageRecorded
TurnStarted
AssistantToolCallsRecorded
ToolRequested
ToolStarted
ToolCompleted
ToolMessageRecorded
...
```

Hai file có thể crash giữa hai lần ghi và mất thứ tự chung. Hướng ít phá nhất là
evolve `EventStore` hiện tại, giữ adapter cho record cũ.

## D.2. Event envelope

```json
{
  "schema_version": 1,
  "event_id": "evt_uuid",
  "seq": 42,
  "ts": "2026-09-06T08:00:00.000Z",
  "session_id": "session_uuid",
  "runtime_instance_id": "process_uuid",
  "event_type": "ToolStarted",
  "aggregate_type": "tool_execution",
  "aggregate_id": "exec_uuid",
  "turn_id": "turn_uuid",
  "causation_id": "prior_event_or_tool_call_id",
  "correlation_id": "turn_uuid",
  "payload": {}
}
```

Yêu cầu:

- `seq` tăng đơn điệu, `event_id` unique;
- UTC timestamp có timezone;
- stable string enum và versioned payload;
- `ToolCompleted` giữ cả raw và compact result;
- sensitive values phải redaction trước persist.

Legacy record có `role` nhưng thiếu `event_type` được đọc như
`LegacyMessageRecorded`. Không bịa lifecycle state cho session cũ.

## D.3. Durability boundaries

Persist:

1. user message và `TurnStarted` trước model call;
2. model response/tool calls sau khi assemble;
3. `ToolStarted` ngay trước external effect;
4. terminal/recovery event ngay sau outcome;
5. approval request và decision;
6. mọi plan mutation;
7. accepted final response và turn terminal event;
8. interruption/failure.

V1 dùng single writer + session file lock + append/flush/fsync tại lifecycle
boundary. Không cần database/event broker.

Nếu `ToolStarted` không persist được, không chạy tool. Nếu terminal event không
persist được sau effect, lần load sau thấy started-without-terminal và recovery,
không blind retry.

## D.4. Reconstruction

```text
open journal with exclusive writer lock
                 ▼
validate JSONL, schema version, seq, event IDs
                 ▼
replay in seq order through pure reducer
                 ▼
RuntimeState(turns, active_plan, executions, approvals)
                 ▼
scan incomplete aggregates
        ├── requested, no started  → PENDING (never started)
        ├── started, no terminal   → RECOVERY_REQUIRED
        ├── completed              → COMPLETED (never execute again)
        └── waiting approval       → restore approval prompt
                 ▼
resolve RECOVERY_REQUIRED before model/tool progress
                 ▼
resume active turn or start linked continuation
```

Reducer pure: cùng events luôn sinh cùng state; reconstruction không gọi model,
tool hoặc external service.

## D.5. Corruption

- Partial final line: phát hiện, backup và chỉ repair tới last complete newline
  qua explicit recovery.
- Corruption giữa file, duplicate seq hoặc invalid historical transition: fail
  closed và báo line/seq, không silently skip.
- Unsupported schema: từ chối mutate session nhưng cho phép export raw.
- Snapshot chỉ là cache có `last_seq`; V1 replay JSONL trực tiếp.

## D.6. Duplicate side effects

Hai identity:

- `tool_call_id`: từ provider, phục vụ message protocol;
- `execution_id`: UUID runtime, stable cho lifecycle.

```text
terminal execution          → reject duplicate start
RUNNING current instance    → reject concurrent start
RECOVERY_REQUIRED           → require recovery; never blind retry
```

| Tool/effect | Default recovery |
|---|---|
| Pure/read-only | Có thể replay sau persisted retry decision |
| File write/edit | Reconcile pre/post hash và expected state |
| Internal `update_plan` | Reconcile theo causation event |
| Arbitrary bash/mutation | Manual/reconciliation; không auto retry |

```text
persist ToolStarted(before_hash, expected_after_hash)
              ▼
        process crashes
              ▼
read current file after restart
       ├── hash == expected ──► RecoveredAsCompleted
       └── unexpected ────────► RECOVERY_REQUIRED + ask user
```

Không thể đảm bảo exactly-once cho arbitrary shell chỉ bằng JSONL. Thiết kế chỉ
bảo đảm terminal execution không chạy lại và unknown outcome không blind retry.
External API chỉ exactly-once nếu chính API có idempotency key/transaction.

---

# E. Proposed Data Models

Các interface dưới đây chỉ mô tả contract:

```python
class TurnStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"

class PlanMode(str, Enum):
    OPTIONAL = "optional"
    REQUIRED = "required"

class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

class CompletionPolicy(str, Enum):
    SELF_ATTESTED = "self_attested"
    EVIDENCE_REQUIRED = "evidence_required"

class ToolExecutionStatus(str, Enum):
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ReplayPolicy(str, Enum):
    REPLAY_SAFE = "replay_safe"
    RECONCILABLE = "reconcilable"
    MANUAL = "manual"
```

```python
@dataclass(frozen=True)
class RuntimeEvent:
    schema_version: int
    event_id: str
    seq: int
    ts: str
    session_id: str
    runtime_instance_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    turn_id: str | None
    causation_id: str | None
    correlation_id: str | None
    payload: dict[str, Any]

@dataclass
class TurnState:
    turn_id: str
    session_id: str
    goal: str
    status: TurnStatus
    started_at: str
    ended_at: str | None = None
    iteration: int = 0
    plan_id: str | None = None
    completion_block_count: int = 0
    resumes_turn_id: str | None = None
    final_text: str | None = None
    error: ErrorRecord | None = None
```

```python
@dataclass
class PlanStep:
    step_id: str
    task: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    required: bool = True
    completion_policy: CompletionPolicy = CompletionPolicy.SELF_ATTESTED
    note: str | None = None
    evidence_execution_ids: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class PlanRevision:
    revision: int
    created_at: str
    created_by: str                 # model | user | caller
    reason: str
    steps: tuple[PlanStep, ...]

@dataclass
class Plan:
    plan_id: str
    session_id: str
    mode: PlanMode
    active_revision: int
    revisions: list[PlanRevision]
```

```python
@dataclass(frozen=True)
class ErrorRecord:
    category: str
    message: str
    exception_type: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ToolResultData:
    raw: str
    compact: str
    success: bool
    exit_code: int | None = None

@dataclass
class ToolExecution:
    execution_id: str
    tool_call_id: str
    session_id: str
    turn_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: ToolExecutionStatus
    requested_at: str
    started_at: str | None = None
    finished_at: str | None = None
    attempt: int = 0
    replay_policy: ReplayPolicy = ReplayPolicy.MANUAL
    approval: ApprovalMetadata | None = None
    result: ToolResultData | None = None
    error: ErrorRecord | None = None
    retry_of_execution_id: str | None = None
    recovery_metadata: dict[str, Any] = field(default_factory=dict)
```

State machine tối thiểu:

```python
@dataclass(frozen=True)
class Transition(Generic[S, E, C]):
    source: S
    event_type: type[E]
    target: S
    guard: Callable[[C, E], bool] | None = None
    action: Callable[[C, E], None] | None = None

class StateMachine(Generic[S, E, C]):
    def validate(self, state: S, event: E, context: C) -> Transition: ...
    def dispatch(self, state: S, event: E, context: C) -> S: ...
```

Entry/exit handlers có thể là maps theo state. Không cần composite, parallel,
history, choice, DSL builder hoặc dependency mới.

---

# F. Integration Points

## Có thể giữ

- Shape model-driven của `Agent.run_turn()`.
- `BaseTool.run()` và `ToolResult` contract.
- Tool registry API.
- JSONL per-session, append-only direction.
- System message được build mới mỗi model call.
- `MemoryManager` là project knowledge riêng.

## Cần sửa

### `Agent.run_turn()`

Giữ vòng lặp nhưng dispatch Turn events, persist boundaries, giao execution cho
tool lifecycle, coi no-tool response là `CompletionRequested`, chạy guard trước
final message và không để tool exception thoát contract.

### `_execute_tool_call()`

Ban đầu giữ làm adapter nhưng bọc bằng lifecycle orchestrator. Chỉ tách
`ToolExecutor` sau nếu method/loop vẫn quá lớn.

### `EventStore`

Evolve thành typed event journal:

- thêm `append_event()`;
- validate schema/seq;
- giữ `append(role=...)` làm compatibility wrapper;
- `to_messages()` thành projector, bỏ runtime-only events;
- reducer reconstruct state;
- file locking/durability.

### `TurnState`, `PlanState`, `ToolState`

- Turn dùng enum/identity/timestamps và reducer updates.
- Plan thay `list[dict]` bằng Plan/Revision/Step.
- Đổi `ToolState` thành `WorkspaceState` hoặc `EnvironmentSnapshot`; logic
  snapshot hiện tại có thể giữ.

### `Compactor`

Chỉ compact provider-message projection theo complete tool interaction groups.
Active plan, unresolved executions và blockers luôn build lại từ RuntimeState.
Summary là cache, không phải truth.

### `MemoryManager`

Không dùng reconstruct runtime. Background daemon update phải nằm ngoài
completion critical path hoặc thành explicit operation được track riêng.

### `main.py`

Resume phải reduce journal, hiển thị pending approval/recovery, giữ writer lock
và phân biệt resume conversation với active turn.

### Registry/tools

Giữ API, thêm metadata tối thiểu:

```text
read        → REPLAY_SAFE
write/edit  → RECONCILABLE
bash        → MANUAL
update_plan → REPLAY_SAFE / internal
```

## Module mới tối thiểu

```text
src/runtime/
├── machine.py       # Transition, StateMachine, InvalidTransition
├── models.py        # enums/events/Turn/Plan/ToolExecution
└── reducer.py       # pure RuntimeState reconstruction
```

Không thêm plugin framework hoặc policy DSL.

---

# G. Migration Plan

Đây là thứ tự migration ở mức thiết kế, chưa phải implementation plan thực thi.

## Phase 0 — Characterize current loop

Fake model/registry deterministic; khóa direct final, tool success/failure,
multiple calls, max iterations và interrupt. Không gọi API thật.

## Phase 1 — Typed event foundation và lifecycle models

Thêm enums/dataclasses/minimal FSM, typed event envelope, legacy adapter và pure
reducer. Chưa đổi tool behavior.

## Phase 2 — Tool execution lifecycle

Wrap `_execute_tool_call()` bằng requested/validated/started/terminal events;
catch exception; execution ID; terminal dedup. Giữ tool implementations và
sequential execution.

## Phase 3 — Turn lifecycle

Thay string mutation bằng TurnMachine; phân biệt failed/interrupted; persist
iteration và terminal outcome; giữ outer model loop.

## Phase 4 — Structured plan và enforcement

Thêm `update_plan`, revision/step guards, completion request/guard, buffer
candidate khi enforcement active. Plan vẫn optional mặc định.

## Phase 5 — Resume và recovery

Reconstruct runtime, detect stale running execution, read replay, file reconcile,
bash manual, restore approval và tiếp tục active turn/linked turn.

## Phase 6 — Context projection và compaction safety

Tách runtime/message projections; compact complete interaction groups; persisted
summary checkpoint nếu cần; runtime state không qua summarizer.

## Phase 7 — Failure hardening

Corrupt/torn JSONL recovery, file locking, crash injection, duplicate-effect
suite và audit validation.

Journal foundation phải đi trước tool lifecycle; nếu để persistence cuối cùng thì
không thể tạo durability boundary an toàn cho `ToolStarted`.

---

# H. Invariants

## Journal/reconstruction

1. Journal là canonical truth; in-memory state reconstruct được từ journal.
2. Session seq tăng đơn điệu và event ID không trùng.
3. Cùng event sequence luôn tạo cùng RuntimeState.
4. Invalid transition không mutate state hoặc tạo external effect.
5. Persistence failure trước side effect phải fail closed.
6. Compaction không sửa/xóa/thay runtime events.

## Turn

7. Mỗi active turn có `turn_id` unique.
8. Mỗi turn có tối đa một terminal outcome.
9. Terminal turn không chuyển trạng thái nữa.
10. `TurnCompleted` chỉ sau accepted final response.
11. Partial stream không phải final response.
12. Max iterations luôn tạo terminal event.
13. Cancellation không bị ghi như generic failure.

## Plan

14. `OPTIONAL` + no plan không chặn simple task.
15. `REQUIRED` không complete nếu chưa có plan.
16. Active plan có required unfinished work phải block completion.
17. Model không skip/xóa required step hoặc hạ required flag.
18. `PENDING → COMPLETED` trực tiếp là invalid.
19. V1 tối đa một `IN_PROGRESS` step.
20. Completed/skipped step không mutate âm thầm trong cùng revision.
21. Structural change tạo revision có actor/reason.
22. Evidence phải trỏ tới successful terminal execution tồn tại.

## Tool execution

23. Mỗi model tool call có đúng một runtime execution ID.
24. Mọi running tool có durable `ToolStarted` trước effect.
25. Mỗi terminal execution có đúng một terminal outcome.
26. Terminal execution không start lại.
27. Tool exception thành lifecycle failure, không crash loop.
28. Approval rejection không chạy tool và vẫn tạo provider tool result.
29. Started-without-terminal sau restart thành `RECOVERY_REQUIRED`.
30. Recovery-required chặn progress tới khi resolved.
31. Arbitrary bash unknown outcome không auto retry.
32. Retry chỉ sau persisted decision và replay/reconcile guard.
33. Tool-call batch không gửi lại model nếu còn call thiếu outcome.
34. Completed event lưu raw cho audit và compact cho prompt.

## Runtime/context

35. Correctness không phụ thuộc model nhớ state từ history.
36. Active plan/approval/unresolved execution không sinh từ LLM summary.
37. Prompt context là projection có thể tái tạo.
38. `PROJECT.md` không quyết định lifecycle transition.
39. LLM quyết định intent; harness quyết định transition/effect hợp lệ.

---

# I. Test Plan

Repository chưa khai báo pytest, nên V1 có thể dùng stdlib `unittest` và fake
adapters deterministic.

## State-machine tests

- Tất cả valid/invalid transitions.
- Guard false không mutate.
- Persistence failure không chạy effect.
- Terminal state từ chối transition.
- Replay không chạy live hooks.

## Tool tests

- Success: requested → validated → started → completed.
- Unknown tool, invalid JSON, thiếu required args: không gọi implementation.
- Fake tool raise: structured failed, turn tiếp tục.
- Direct allow, approve, reject, stale/wrong-scope approval.
- Cancel pending/waiting/running; uncertain cancellation thành recovery.

## Crash injection

Crash mô phỏng:

1. trước `ToolStarted`;
2. sau durable start, trước `tool.run()`;
3. trong `tool.run()`;
4. sau effect, trước completed event;
5. sau completed event;
6. giữa plan mutation và internal tool result;
7. giữa final acceptance và turn completion.

Reload sau mỗi case và assert state/recovery action.

## Duplicate execution

- Completed tool không chạy lần hai sau resume.
- Concurrent start cùng execution bị reject.
- Replay-safe read tăng persisted attempt.
- File đạt expected hash recovered completed, không ghi lại.
- Unexpected hash giữ recovery-required.
- Bash unknown không tự chạy lại.

## Turn tests

- Direct text: idle → running → completed.
- Model error → structured failed.
- Interrupt stream → interrupted, không final.
- Max iteration đúng số call và reason.
- Tool failure không bắt buộc fail turn.
- Một terminal event duy nhất.

## Plan tests

- Optional/no plan cho completion.
- Required/no plan block.
- Pending required block; optional pending không block.
- Pending → completed bị reject.
- Model skip/remove required bị reject.
- User-authorized skip có reason.
- Evidence thiếu/failed/wrong session bị reject.
- Self-attested step accepted với note.
- Ba early completion attempts → failed đúng reason.
- Revision giữ completed work và audit change.

## Resume/corruption tests

- Resume completed session và active turn.
- Restore waiting approval.
- Phân biệt never-started, started-unknown, completed.
- Resolve recovery rồi tiếp tục loop.
- Legacy message-only JSONL vẫn đọc được.
- Partial final line, malformed middle line, duplicate seq/event ID, unsupported
  schema và invalid historical transition đều fail rõ line/seq.

## Context/compaction tests

- Runtime projection luôn theo reducer state mới nhất.
- Không chia đôi assistant tool-call/tool results.
- Blocked candidate không thành final message.
- Active plan/approval/recovery không mất sau compaction.
- Xóa summary cache vẫn dựng lại được context.

Commands dự kiến khi implementation bắt đầu:

```bash
python -m unittest discover -s tests -v
python -W error -m compileall -q src tests
```

Không dùng real model/network trong correctness suite.

---

# Runtime State và LLM Context

```text
                     CANONICAL / NEVER COMPACTED
┌──────────────────────────────────────────────────────────────┐
│ Runtime Event Journal                                        │
│ turn events · plan revisions · tool lifecycle · approvals    │
└─────────────────────────────┬────────────────────────────────┘
                              │ reducer
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ RuntimeState                                                 │
│ exact statuses · IDs · guards · unresolved side effects      │
└───────────────┬──────────────────────────────┬───────────────┘
                │ deterministic projection     │ controls harness
                ▼                              ▼
┌──────────────────────────────┐       transition / side effects
│ Runtime prompt projection    │
│ goal, active plan, outcomes, │
│ current blockers             │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────────────────────────────────────┐
│ LLM Context                                                  │
│ system projection + compactable conversation projection      │
└──────────────────────────────────────────────────────────────┘
```

| Data | Harness owns | Exposed to LLM | Compactable |
|---|---:|---:|---:|
| Exact turn status/IDs | Có | Projection cần thiết | Không |
| Active plan/revision/status | Có | Có | Không |
| Approval/policy metadata | Có | Khi cần | Không |
| Tool args/full raw result | Có | Compact projection | Prompt copy có |
| Unresolved execution | Có | Có sau recovery policy | Không |
| Old conversation messages | Journal | Có | Có trong prompt |
| Summary | Không canonical | Có | Có thể xóa/tạo lại |
| `PROJECT.md` facts | MemoryManager | Có | Không phải runtime truth |

Compaction có thể làm model quên chi tiết hội thoại nhưng không thể làm harness
quên required step, approval, terminal execution hoặc unknown side effect.

---

# Kết luận

```text
LLM decides:      what it wants to do next
Runtime decides:  whether the transition is legal
Policy decides:   whether the effect is allowed
Journal records:  what is durably known
Recovery decides: what may safely happen again
```

Giới hạn cố ý của V1:

- không giant global FSM;
- không cognitive workflow states;
- không parallel tool execution;
- không database/event broker;
- không automatic retry arbitrary side effects;
- không giả vờ exactly-once shell execution;
- không planner framework;
- không để LLM summary làm nguồn sự thật.

Đây là thay đổi nhỏ nhất tạo deterministic lifecycle, recoverability và plan
enforcement mà vẫn giữ `Agent.run_turn()` làm lõi model-driven.
