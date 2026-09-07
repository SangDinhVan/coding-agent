# Lifecycle và luồng dữ liệu của Coding Agent

> Tài liệu này mô tả **implementation hiện tại**, tập trung vào bốn góc nhìn:
> lifecycle của Tool Executor, Agent Memory, state machine của Agent Loop và luồng
> end-to-end tổng hợp.

## Bản đồ thành phần

| Thành phần | Trách nhiệm chính |
|---|---|
| [`src/main.py`](../src/main.py) | CLI, mở/resume session, xử lý approval và recovery |
| [`src/agent/loop.py`](../src/agent/loop.py) | Điều phối turn, gọi LLM, chạy tool và chốt kết quả |
| [`src/runtime/executor.py`](../src/runtime/executor.py) | Lifecycle bền vững của từng tool execution |
| [`src/runtime/reducer.py`](../src/runtime/reducer.py) | Replay event thành `RuntimeState` và kiểm tra transition |
| [`src/runtime/models.py`](../src/runtime/models.py) | Enum và data model cho turn, plan, tool execution |
| [`src/memory/event_store.py`](../src/memory/event_store.py) | Append-only JSONL journal và message projection |
| [`src/memory/manager.py`](../src/memory/manager.py) | Project memory bền vững trong `PROJECT.md` |
| [`src/context/compactor.py`](../src/context/compactor.py) | Rút gọn context cũ khi gần giới hạn token |
| [`src/agent/state.py`](../src/agent/state.py) | Snapshot trực tiếp của workspace và Git |

---

## 1. Lifecycle của Tool Executor

Một model tool call được tách thành hai ID:

- `tool_call_id`: ID từ provider/model, dùng trong protocol message;
- `execution_id`: ID do runtime tạo, dùng cho approval, retry và recovery.

`request_batch()` ghi toàn bộ `ToolRequested` của batch trước khi chạy tool đầu tiên.
Mỗi lần `execute()` đều replay journal để lấy trạng thái canonical mới nhất.

```mermaid
flowchart TD
    A["Model trả một batch tool calls"] --> B["Ghi AssistantToolCallsRecorded"]
    B --> C["ToolExecutor.request_batch"]
    C --> D["Parse arguments + tạo execution_id"]
    D --> E["Ghi ToolRequested cho toàn bộ batch"]
    E --> F["ToolExecutor.execute từng execution"]
    F --> G["Replay journal → ToolExecution hiện tại"]
    G --> H{"Status là PENDING?"}
    H -->|Không| ERR0["Trả lỗi: execution đã được xử lý"]
    H -->|Có| I{"Tool tồn tại + JSON hợp lệ?"}
    I -->|Không| FAIL0["ToolFailed"]
    I -->|Có| J{"validate arguments"}
    J -->|Lỗi| FAIL1["ToolFailed: validation"]
    J -->|Hợp lệ| K["ToolValidated"]
    K --> L{"Policy decision"}

    L -->|DENY| CANCEL["ToolCancelled"]
    L -->|ASK| ASK["ToolApprovalRequested"]
    ASK --> WAIT["WAITING_APPROVAL"]
    WAIT -->|Reject| REJECT["ToolRejected → CANCELLED"]
    WAIT -->|Approve| APPROVE["ToolApproved → PENDING"]
    APPROVE --> META
    L -->|ALLOW| META["Tạo recovery metadata"]

    META -->|Lỗi| FAIL2["ToolFailed: recovery_metadata"]
    META -->|Thành công| START["Ghi ToolStarted + fsync"]
    START --> RUN["RUNNING: gọi execute_runtime hoặc run"]
    RUN -->|success=true| DONE["ToolCompleted"]
    RUN -->|success=false / exception| FAIL3["ToolFailed"]
    RUN -->|Crash / outcome không rõ| RECOVERY["RECOVERY_REQUIRED"]

    RECOVERY -->|Xác nhận effect đã xong| REC_DONE["ToolRecoveredAsCompleted"]
    RECOVERY -->|Xác nhận thất bại| REC_FAIL["ToolRecoveredAsFailed"]
    RECOVERY -->|Retry được policy cho phép| RETRY["ToolRetryScheduled → PENDING"]
    RETRY --> F

    DONE --> MSG["Ghi ToolMessageRecorded"]
    FAIL0 --> MSG
    FAIL1 --> MSG
    FAIL2 --> MSG
    FAIL3 --> MSG
    CANCEL --> MSG
    REJECT --> MSG
    REC_DONE --> MSG
    REC_FAIL --> MSG
    MSG --> LOOP["Quay lại Agent Loop"]
```

### State machine của một tool execution

```mermaid
stateDiagram-v2
    [*] --> PENDING: ToolRequested
    PENDING --> PENDING: ToolValidated
    PENDING --> WAITING_APPROVAL: ToolApprovalRequested
    WAITING_APPROVAL --> PENDING: ToolApproved
    WAITING_APPROVAL --> CANCELLED: ToolRejected

    PENDING --> RUNNING: ToolStarted
    PENDING --> FAILED: lookup / parse / validate / metadata error
    PENDING --> CANCELLED: policy denied

    RUNNING --> COMPLETED: ToolCompleted
    RUNNING --> FAILED: ToolFailed
    RUNNING --> RECOVERY_REQUIRED: crash hoặc interruption

    RECOVERY_REQUIRED --> COMPLETED: ToolRecoveredAsCompleted
    RECOVERY_REQUIRED --> FAILED: ToolRecoveredAsFailed
    RECOVERY_REQUIRED --> PENDING: ToolRetryScheduled

    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

Các trạng thái terminal là `COMPLETED`, `FAILED`, `CANCELLED`.
`RECOVERY_REQUIRED` chưa terminal vì cần người dùng xác nhận outcome hoặc retry.

### Ranh giới an toàn quan trọng

1. `ToolStarted` được append, flush và `fsync` **trước side effect**.
2. Tool chỉ được chạy khi execution đang `PENDING`.
3. Arguments có key nhạy cảm bị ghi thành `[REDACTED]`; bản thật chỉ giữ tạm trong RAM.
4. Sau restart, tool đang `RUNNING` từ runtime instance cũ được project thành
   `RECOVERY_REQUIRED` thay vì tự động chạy lại.
5. Retry phụ thuộc `ReplayPolicy`: `REPLAY_SAFE`, `RECONCILABLE` hoặc `MANUAL`.

---

## 2. Luồng Agent Memory

Agent có nhiều lớp “memory”, mỗi lớp có vai trò khác nhau:

| Lớp | Nguồn | Tuổi thọ | Mục đích |
|---|---|---|---|
| Session journal | `memory/chats/<session>.jsonl` | Xuyên restart | Nguồn sự thật cho message và runtime lifecycle |
| Runtime projection | `replay(events)` | Trong RAM, dựng lại được | Trạng thái turn, plan và tool execution hiện tại |
| Provider context | System prompt + message history | Một lần gọi model | Ngữ cảnh trực tiếp gửi cho LLM |
| Compacted context | Summary + nhóm message gần nhất | Một lần gọi model | Giảm token mà không làm vỡ tool-call/result group |
| Project memory | `src/memory/private/PROJECT.md` | Xuyên session | Facts, kiến trúc, quyết định và vấn đề lâu dài |
| Workspace snapshot | Filesystem + Git | Đọc mới khi build context | `cwd`, branch và file đang thay đổi |

```mermaid
flowchart TD
    subgraph WRITE["Ghi memory trong một turn"]
        U["User / Assistant / Tool / Runtime event"] --> APPEND["EventStore.append hoặc append_event"]
        APPEND --> REDACT["Redact secret-like fields"]
        REDACT --> JSONL["Append JSONL + flush + fsync"]
    end

    subgraph REBUILD["Dựng state canonical"]
        JSONL --> READ["EventStore.read_all"]
        READ --> REPLAY["replay + reduce_event"]
        REPLAY --> STATE["RuntimeState trong RAM"]
        STATE --> PROJ["runtime_prompt_projection"]
    end

    subgraph CONTEXT["Dựng context cho LLM"]
        JSONL --> TOMSG["EventStore.to_messages"]
        TOMSG --> HISTORY["user / assistant / tool history"]
        PROJECT["PROJECT.md"] --> SYS["System message"]
        PROJ --> SYS
        WORKSPACE["WorkspaceState.snapshot"] --> SYS
        SYS --> MERGE["System message + history"]
        HISTORY --> MERGE
        MERGE --> CHECK{"Vượt 70% context window?"}
        CHECK -->|Không| PROVIDER["Messages gửi LLM"]
        CHECK -->|Có| COMPACT["Compactor: summary phần cũ"]
        COMPACT --> RECENT["Giữ nguyên các interaction group gần nhất"]
        RECENT --> PROVIDER
    end

    subgraph LONGTERM["Cập nhật project memory sau turn"]
        COMPLETE["TurnCompleted"] --> RECENT_TEXT["Render tối đa 20 message gần nhất"]
        RECENT_TEXT --> BG["Background thread"]
        BG --> MEM_LLM["LLM chọn facts mới quan trọng"]
        MEM_LLM -->|JSON hợp lệ + có nội dung| PROJECT
        MEM_LLM -->|Không có thay đổi / JSON lỗi| NOOP["Không ghi thêm"]
    end

    PROVIDER --> LLM["LLM completion"]
    STATE --> COMPLETE
```

### Nguyên tắc dữ liệu

- **Journal là source of truth**; `RuntimeState` chỉ là projection dựng lại được.
- `PROJECT.md` hỗ trợ reasoning nhưng không được dùng để quyết định transition.
- `WorkspaceState` phản ánh môi trường thật tại thời điểm đọc, không phải lifecycle state.
- Runtime event không được gửi thẳng cho model như chat message; model chỉ nhận một
  projection ngắn của runtime state trong system prompt.
- Compaction chỉ biến đổi context gửi model, không sửa hoặc xóa journal gốc.
- Assistant tool call và các tool result tương ứng được compact như một nhóm nguyên tử.

---

## 3. State machine của Agent Loop

Trong implementation hiện tại, `IDLE` không tồn tại dưới dạng một `TurnState`.
Session rảnh được biểu diễn bằng `RuntimeState.active_turn_id is None`; `TurnStarted`
tạo trực tiếp một turn ở trạng thái `RUNNING`.

```mermaid
stateDiagram-v2
    [*] --> SESSION_IDLE: active_turn_id = None
    SESSION_IDLE --> RUNNING: UserMessageRecorded + TurnStarted

    state RUNNING {
        [*] --> ADVANCE_ITERATION
        ADVANCE_ITERATION --> BUILD_CONTEXT: TurnIterationAdvanced
        BUILD_CONTEXT --> CALL_MODEL
        CALL_MODEL --> TOOL_BRANCH: Có tool_calls
        CALL_MODEL --> COMPLETION_BRANCH: Không có tool_calls

        TOOL_BRANCH --> PERSIST_BATCH: AssistantToolCallsRecorded
        PERSIST_BATCH --> EXECUTE_TOOLS: ToolRequested cho toàn bộ batch
        EXECUTE_TOOLS --> BUILD_CONTEXT: Mọi execution đã có outcome
        EXECUTE_TOOLS --> WAIT_EXTERNAL: Chờ approval / recovery

        COMPLETION_BRANCH --> CHECK_BLOCKERS: CompletionRequested
        CHECK_BLOCKERS --> ADVANCE_ITERATION: Có completion blocker
        CHECK_BLOCKERS --> ACCEPT_FINAL: Không có blocker
    }

    RUNNING --> COMPLETED: AssistantMessageRecorded(final) + TurnCompleted
    RUNNING --> INTERRUPTED: Ctrl+C → TurnInterrupted
    RUNNING --> FAILED: model error / max iterations / block 3 lần
    RUNNING --> RUNNING: process restart + resume hợp lệ

    COMPLETED --> SESSION_IDLE: clear active_turn_id
    INTERRUPTED --> SESSION_IDLE: clear active_turn_id
    FAILED --> SESSION_IDLE: clear active_turn_id
```

### Completion blockers

Trước khi chấp nhận final text, `completion_blockers()` kiểm tra:

1. plan bắt buộc nhưng chưa được tạo;
2. required plan step chưa `COMPLETED` hoặc `SKIPPED`;
3. tool của turn còn `WAITING_APPROVAL`, `RUNNING` hoặc `RECOVERY_REQUIRED`.

Nếu bị block, agent ghi `CompletionBlocked` và chạy iteration tiếp theo. Ba lần block
liên tiếp dẫn đến `TurnFailed(category="repeated_incomplete_plan")`.

### Terminal paths

| Kết quả | Event | Điều kiện chính |
|---|---|---|
| Thành công | `TurnCompleted` | Candidate không còn completion blocker |
| Gián đoạn | `TurnInterrupted` | Người dùng ngắt model/tool flow |
| Thất bại | `TurnFailed` | Model exception, hết iteration hoặc block lặp lại |
| Tạm dừng | Không terminal | Đang chờ approval hoặc recovery |

---

## 4. Luồng tổng hợp end-to-end

Sơ đồ sau kết nối CLI, Agent Loop, Memory, LLM và Tool Executor trong một turn.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI as main.run_repl
    participant Agent as Agent
    participant Store as EventStore / JSONL
    participant Reducer as replay / RuntimeState
    participant Memory as PROJECT.md + Workspace
    participant Compactor
    participant LLM
    participant Executor as ToolExecutor
    participant Tool

    User->>CLI: Nhập prompt
    CLI->>Agent: run_turn(user_input)
    Agent->>Store: UserMessageRecorded
    Agent->>Store: TurnStarted
    Store-->>Reducer: read_all events
    Reducer-->>Agent: RuntimeState(active turn)

    loop Tối đa max_iterations
        Agent->>Store: TurnIterationAdvanced
        Agent->>Memory: Đọc PROJECT.md + workspace snapshot
        Agent->>Store: to_messages()
        Store-->>Agent: Conversation history
        Agent->>Reducer: runtime_prompt_projection(state)
        Reducer-->>Agent: Runtime state ngắn gọn
        Agent->>Compactor: System + history
        alt Context vượt ngưỡng
            Compactor->>LLM: Tóm tắt history cũ
            LLM-->>Compactor: Summary
        end
        Agent->>LLM: complete(messages, tools, stream=True)
        LLM-->>Agent: Text hoặc tool calls

        alt Model trả tool calls
            Agent->>Store: AssistantToolCallsRecorded
            Agent->>Executor: request_batch(tool_calls, turn_id)
            loop Mỗi call trong batch
                Executor->>Store: ToolRequested
            end
            loop Mỗi execution theo thứ tự
                Agent->>Executor: execute(execution_id)
                Executor->>Store: Replay state
                Executor->>Executor: Lookup + validate + policy
                alt Cần approval
                    Executor->>Store: ToolApprovalRequested
                    Store-->>CLI: Pending approval khi resume/handle
                    User->>CLI: Approve hoặc reject + note
                    CLI->>Executor: resolve_approval
                end
                Executor->>Store: ToolStarted + fsync
                Executor->>Tool: execute_runtime hoặc run
                Tool-->>Executor: ToolResult
                Executor->>Store: ToolCompleted hoặc ToolFailed
                Executor-->>Agent: Compact result
                Agent->>Store: ToolMessageRecorded
            end
            Note over Agent,LLM: Sau batch, loop gọi model lại với tool results
        else Model trả final candidate
            Agent->>Store: CompletionRequested
            Agent->>Reducer: completion_blockers(state, turn_id)
            alt Còn blocker
                Reducer-->>Agent: Danh sách blocker
                Agent->>Store: CompletionBlocked
                Note over Agent,LLM: Tiếp tục iteration
            else Không còn blocker
                Agent->>Store: AssistantMessageRecorded(final=true)
                Agent->>Store: TurnCompleted
                Agent-->>CLI: final_text
                CLI-->>User: In câu trả lời
            end
        end
    end

    opt Turn đã COMPLETED
        Agent->>Memory: Background update từ 20 message gần nhất
        Memory->>LLM: Chọn project facts bền vững
        LLM-->>Memory: project_md_append
        Memory->>Memory: Append PROJECT.md nếu có nội dung mới
    end
```

### Resume sau restart

```mermaid
flowchart LR
    START["Process mới mở journal cũ"] --> LOCK["Lấy exclusive writer lock"]
    LOCK --> REPLAY["Replay toàn bộ events"]
    REPLAY --> DETECT{"Có execution RUNNING từ instance cũ?"}
    DETECT -->|Có| REC["Project thành RECOVERY_REQUIRED"]
    DETECT -->|Không| PENDING{"Có execution PENDING?"}
    REC --> USER["Người dùng resolve recovery"]
    USER --> DRAIN["Drain pending executions"]
    PENDING -->|Có| DRAIN
    PENDING -->|Không| FINAL{"Đã persist final nhưng thiếu TurnCompleted?"}
    DRAIN --> FINAL
    FINAL -->|Có| COMPLETE["Ghi TurnCompleted, không gọi LLM lại"]
    FINAL -->|Không| LOOP["Tiếp tục _drive_turn"]
```

---

## 5. Tóm tắt các invariant

1. Mỗi session chỉ có một writer và tối đa một active turn.
2. Event journal là append-only, có `seq` tăng dần và được `fsync`.
3. Mọi lifecycle transition đều xuất phát từ event và được reducer kiểm tra.
4. Tool side effect chỉ bắt đầu sau khi `ToolStarted` đã được persist.
5. Tool terminal không được transition thêm hoặc chạy lần hai.
6. Outcome không rõ sau restart phải qua recovery, không tự động đoán.
7. Final answer chỉ được persist khi không còn completion blocker.
8. Compaction và `PROJECT.md` không thay thế runtime state canonical.
