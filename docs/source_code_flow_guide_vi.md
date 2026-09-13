# Hướng dẫn đọc toàn bộ source Coding Agent

> Tài liệu này mô tả **implementation đang có trong source**, không mô tả một kiến trúc lý tưởng trong tương lai.
>
> Mục tiêu là giúp bạn trả lời được ba câu hỏi:
>
> 1. Chương trình bắt đầu từ đâu và các object được nối với nhau thế nào?
> 2. Một yêu cầu của user đi qua model, tool và sandbox ra sao?
> 3. Nếu chương trình bị dừng giữa chừng, dữ liệu nào giúp nó khôi phục đúng trạng thái?

---

## 1. Phạm vi và cách đọc tài liệu

Repository này là một **coding agent chạy qua CLI**. Agent gọi LLM thông qua LiteLLM, nhận tool call từ model rồi thực thi tool trong một Docker sandbox bị giới hạn.

File `index.html` ở root hiện tại là **sản phẩm do agent sinh ra trong workspace**, không phải source lõi của coding agent. Source lõi nằm chủ yếu trong:

- [`src/`](../src/): control plane, agent loop, journal, reducer, tool và sandbox orchestration.
- [`sandbox-image/`](../sandbox-image/): image và helper chạy bên trong sandbox child.
- [`compose.yaml`](../compose.yaml), [`Dockerfile`](../Dockerfile), [`docker-entrypoint.sh`](../docker-entrypoint.sh): đường khởi động bằng Docker.
- [`tests/`](../tests/): behavioral contract của từng subsystem.

### 1.1. Các thuật ngữ chính

| Thuật ngữ | Ý nghĩa trong code |
|---|---|
| **Control plane** | Phần Python đáng tin cậy điều phối CLI, LLM, journal và Docker. Khi chạy bằng Compose, nó nằm trong container `coding-agent`. |
| **Sandbox child** | Container con không đáng tin cậy, nơi lệnh shell và filesystem tool thực sự chạy. |
| **Session/chat** | Một cuộc hội thoại bền vững, được nhận dạng bằng UUID và một file JSONL. |
| **Turn** | Một yêu cầu của user và toàn bộ các vòng model/tool để giải quyết yêu cầu đó. |
| **Iteration** | Một lần Agent gọi model trong cùng một turn. |
| **Tool call** | Yêu cầu gọi function do provider/model sinh ra, có `tool_call_id`. |
| **Tool execution** | Lifecycle nội bộ của một lần chạy tool, có `execution_id` riêng. |
| **Event journal** | File JSONL append-only, là nguồn sự thật của lifecycle runtime. |
| **Projection/reducer** | Cách phát lại event để dựng `RuntimeState` trong memory. |
| **Live workspace** | Mode mặc định hiện tại: sandbox mount trực tiếp workspace host và thay đổi file xuất hiện ngay. |
| **Shadow workspace** | Mode cũ: copy workspace sang vùng riêng, sau đó seal/apply nguyên changeset. |

### 1.2. Luồng nào đang được dùng thật?

Luồng CLI mặc định trong [`_default_agent_factory()`](../src/main.py) luôn tạo session với:

```python
mode=WorkspaceMode.LIVE
```

Vì vậy:

- **Live mode** là đường chạy chính hiện tại.
- **Shadow mode** vẫn còn đầy đủ code và test để resume metadata cũ hoặc gọi trực tiếp bằng code.
- `/changes` và `/apply` chỉ thực sự có tác dụng với shadow session cũ.

Ngoài ra có hai module tồn tại nhưng không nằm trên production call path hiện tại:

- [`src/runtime/machine.py`](../src/runtime/machine.py): state-machine generic, hiện chỉ được dùng bởi test riêng.
- [`src/tools/verbs.py`](../src/tools/verbs.py): thiết kế structured PowerShell verbs cũ, không được `ToolRegistry` đăng ký.

---

## 2. Bức tranh kiến trúc tổng thể

### 2.1. Hai trust zone

```text
┌──────────────────────────────── HOST ────────────────────────────────┐
│                                                                     │
│  Workspace thật                Rootless Docker daemon               │
│  /path/to/project              unix socket trong XDG_RUNTIME_DIR    │
│        ▲                                  ▲                         │
│        │ live bind mount                  │ Docker CLI              │
│        │                                  │                         │
│  ┌─────┴──────────────── CONTROL PLANE CONTAINER ────────────────┐  │
│  │                                                               │  │
│  │  CLI / REPL ── Agent ── LiteLLM ── API provider              │  │
│  │                 │                                             │  │
│  │                 ├── EventStore JSONL + Reducer                │  │
│  │                 ├── MemoryManager + Compactor                 │  │
│  │                 └── ToolExecutor ── SandboxSession            │  │
│  │                                          │                    │  │
│  │                                      DockerBackend            │  │
│  └──────────────────────────────────────────┼────────────────────┘  │
│                                             │ docker exec            │
│  ┌────────────────── UNTRUSTED SANDBOX CHILD ┴───────────────────┐  │
│  │                                                               │  │
│  │  /opt/coding-agent/bin/sandbox_fs.py                          │  │
│  │  /opt/coding-agent/bin/sandbox_exec.py                        │  │
│  │                                                               │  │
│  │  network=none        rootfs=read-only       caps=none          │  │
│  │  no-new-privileges   bounded CPU/RAM/PIDs   bounded output     │  │
│  │                                                               │  │
│  │  /workspace ───────────── bind mount ─────────► workspace thật │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

Điểm quan trọng nhất: **model không trực tiếp chạy shell trên host**. Model chỉ sinh tool call. Tool call phải đi qua `ToolExecutor`, tool đã bind với `SandboxSession`, `DockerBackend`, rồi mới đến helper trong sandbox child.

Tuy nhiên control-plane container có quyền truy cập Docker socket. Vì vậy ranh giới bảo mật chính là:

- control plane được tin cậy;
- code/lệnh sinh bởi model chỉ được chạy trong sandbox child;
- sandbox child không được mount Docker socket.

### 2.2. Dependency map của Python source

```text
main.py
  │
  ├── ControlPaths
  ├── DockerBackend
  ├── SandboxSession
  ├── ToolRegistry
  └── Agent
       │
       ├── EventStore ───────────► events.jsonl
       ├── replay/reduce_event ──► RuntimeState
       ├── MemoryManager ────────► PROJECT.md
       ├── Compactor ────────────► LiteLLM summary call
       ├── llm.complete ─────────► model provider
       ├── UpdatePlanTool ───────► Agent._apply_plan_action
       └── ToolExecutor
            │
            ├── Read/Write/Edit ─► SandboxSession.fs_call
            └── Bash ────────────► SandboxSession.exec
                                      │
                                      └── DockerBackend
                                           ├── sandbox_fs.py
                                           └── sandbox_exec.py
```

### 2.3. Trách nhiệm từng package

| Package/file | Trách nhiệm |
|---|---|
| [`src/main.py`](../src/main.py) | Parse CLI, chọn chat, tạo/resume sandbox, chạy REPL và xử lý control command. |
| [`src/agent/loop.py`](../src/agent/loop.py) | Vòng lặp model/tool, prompt, plan action, final-answer guard và memory update. |
| [`src/agent/state.py`](../src/agent/state.py) | Projection read-only của sandbox/workspace để đưa vào prompt. |
| [`src/memory/event_store.py`](../src/memory/event_store.py) | Journal JSONL append-only, khóa writer, validate, redaction và chuyển event thành model messages. |
| [`src/runtime/models.py`](../src/runtime/models.py) | Enum/dataclass cho turn, plan, tool execution, result và recovery. |
| [`src/runtime/reducer.py`](../src/runtime/reducer.py) | Dựng `RuntimeState` từ event và tính completion blocker/pending action. |
| [`src/runtime/executor.py`](../src/runtime/executor.py) | Điều phối lifecycle bền vững quanh mỗi tool side effect. |
| [`src/tools/`](../src/tools/) | Contract, schema và implementation của các tool model nhìn thấy. |
| [`src/sandbox/`](../src/sandbox/) | Path/state, workspace policy, Docker backend, session lifecycle và legacy changeset. |
| [`sandbox-image/`](../sandbox-image/) | Runtime image và hai helper an toàn chạy bên trong child. |
| [`src/context/compactor.py`](../src/context/compactor.py) | Rút gọn history khi gần đầy context window. |
| [`src/memory/manager.py`](../src/memory/manager.py) | Đọc và cập nhật memory `PROJECT.md` qua nhiều session. |
| [`src/model/llm.py`](../src/model/llm.py) | Adapter mỏng quanh LiteLLM và token counting. |

---

## 3. Luồng khởi động: từ `docker compose` đến REPL

### 3.1. Sơ đồ startup

```text
User
  │
  │ docker compose run --build --rm coding-agent
  ▼
compose.yaml
  │
  ├── build control-plane Dockerfile
  ├── mount repository vào đúng ${PWD}
  ├── mount state root
  └── mount rootless Docker socket vào /var/run/docker.sock
        │
        ▼
control-plane container
  │ ENTRYPOINT = coding-agent-entrypoint
  ▼
docker-entrypoint.sh
  │
  ├── docker build sandbox-image/
  ├── nhận immutable image ID sha256:...
  ├── reject nếu output không đúng dạng SHA-256
  └── exec coding-agent --workspace ... --state-root ... --image sha256:...
        │
        ▼
main.py
  │
  ├── parse args + ResourceLimits
  ├── create/resolve chat path
  ├── create ControlPaths
  ├── create/resume SandboxSession
  ├── bind Agent + ToolRegistry vào session
  └── run_repl()
```

### 3.2. Compose tạo control plane

[`compose.yaml`](../compose.yaml) cấu hình service `coding-agent`:

- `build: .` build [`Dockerfile`](../Dockerfile).
- `stdin_open: true` và `tty: true` cho REPL tương tác.
- `working_dir: ${PWD}` để đường dẫn trong control-plane giống host.
- đọc `API_KEY` từ `.env`.
- mount repository host vào cùng `${PWD}` trong control plane.
- mount state root host vào cùng đường dẫn.
- mount `${XDG_RUNTIME_DIR}/docker.sock` vào `/var/run/docker.sock`.
- đặt `DOCKER_HOST=unix:///var/run/docker.sock`.

Socket này phải là socket của **rootless Docker daemon**. Docker Desktop và rootless Docker là hai daemon/context độc lập. Nếu chương trình chạy qua rootless context thì container child xuất hiện ở daemon rootless, không nhất thiết xuất hiện trong giao diện Docker Desktop.

### 3.3. Control-plane image

[`Dockerfile`](../Dockerfile):

1. Lấy Docker CLI từ image Docker đã pin digest.
2. Dùng Python image đã pin digest làm base.
3. Cài `git`.
4. Copy `pyproject.toml`, dependencies và `src`.
5. `pip install .` để tạo command `coding-agent` từ entrypoint trong [`pyproject.toml`](../pyproject.toml).
6. Đặt [`docker-entrypoint.sh`](../docker-entrypoint.sh) làm container entrypoint.

Control plane cần Docker CLI vì nó tự build và điều khiển sandbox child thông qua socket được mount.

### 3.4. Build sandbox image tại startup

[`docker-entrypoint.sh`](../docker-entrypoint.sh) chạy:

```sh
docker build --quiet --tag sang-coding-agent-sandbox:local "$workspace/sandbox-image"
```

Docker trả về image ID dạng `sha256:<64 hex characters>`. Script kiểm tra đúng format này trước khi gọi CLI.

Lợi ích của việc truyền image ID bất biến:

- tag `:local` có thể bị trỏ sang image khác;
- image ID SHA-256 xác định đúng bytes/image đã build;
- `DockerBackend` có thể inspect và fail closed nếu identity không khớp.

### 3.5. Parse CLI và chọn chat

[`build_parser()`](../src/main.py) nhận:

- `--workspace`;
- `--state-root`;
- `--image` bắt buộc;
- CPU, memory, PID, tmpfs, tool timeout và workspace growth limit;
- subcommand `resume [session]` hoặc `resume --last`.

[`select_chat_path()`](../src/main.py):

- không có `resume` → sinh UUID mới qua `create_chat_path()`;
- `resume --last` → chọn chat mới cập nhật gần nhất;
- `resume <prefix>` → prefix phải match duy nhất;
- `resume` không argument → in danh sách để user chọn.

Chat được lưu dưới:

```text
<state-root>/chats/<session-id>.jsonl
```

### 3.6. Tạo các control path

[`ControlPaths.create()`](../src/core/paths.py) tạo vùng state riêng mode `0700`:

```text
<state-root>/
├── chats/
│   └── <session-id>.jsonl
├── projects/default/
│   └── PROJECT.md
└── sandboxes/<session-id>/
    ├── workspace/                 # dùng bởi shadow mode
    ├── shadow.git/                # baseline của shadow mode
    ├── rollback/
    ├── metadata.json
    ├── heartbeat
    ├── baseline-manifest.json
    ├── final-manifest.json
    ├── changeset.json
    ├── mask-file
    └── mask-directory/
```

`workspace_identity` là SHA-256 của:

```text
resolved source path + device ID + inode
```

Identity này ngăn resume nhầm cùng session với một workspace khác.

### 3.7. Tạo hoặc resume sandbox session

[`_default_agent_factory()`](../src/main.py) thực hiện:

1. Resolve workspace thật.
2. Tạo `ControlPaths` bằng chính chat ID.
3. Tạo `DockerBackend` với immutable image ID.
4. Nếu có metadata cũ, lấy lại `container_id`.
5. Tạo `SandboxSession` ở `WorkspaceMode.LIVE`.
6. Chạy `recover_incomplete_apply()` để hoàn tất rollback shadow apply cũ nếu cần.
7. Nếu session cũ là `STOPPED`, gọi `resume()` rồi bật watchdog.
8. Nếu session cũ là `SEALED`, giữ nguyên để user xử lý changeset legacy.
9. Các trạng thái cũ khác bị từ chối.
10. Session mới chạy `create()` → `start()` → `start_watchdog()`.
11. Tạo `Agent` và `ToolRegistry`, tất cả OS tool cùng bind vào đúng session.

### 3.8. REPL

[`run_repl()`](../src/main.py) xử lý các input đặc biệt trước khi gửi model:

| Input | Hành vi |
|---|---|
| `exit`, `quit` | Thoát vòng lặp. |
| `/img path1,path2 prompt` | Encode ảnh thành data URL và gửi cùng text. |
| `resume` | Tiếp tục active turn bị dừng trước đó. |
| `/changes` | Live: hướng dẫn dùng Git/IDE. Shadow: seal và hiển thị changeset. |
| `/apply` | Live: không cần. Shadow: yêu cầu exact hash và note trước khi apply. |
| `/discard` | Destroy sandbox; live changes vẫn còn trong workspace. |

Trước khi nhận turn mới, REPL gọi `handle_pending_runtime_actions()` để xử lý:

- approval đang chờ;
- execution cần recovery.

Trong `finally`, `agent.close()` luôn được gọi để đóng EventStore và stop sandbox nếu cần.

> **Chi tiết implementation hiện tại:** `run_repl()` kiểm tra `events_path.exists()` sau khi factory đã tạo `EventStore`. Vì mở journal bằng `a+` có thể tạo file mới, nhãn `New/Resumed` không phải nguồn sự thật đáng tin cậy cho lifecycle.

---

## 4. Một chat turn hoàn chỉnh

### 4.1. Happy path không gọi tool

```text
User input
  │
  ├── UserMessageRecorded
  ├── TurnStarted
  ▼
Agent._drive_turn
  │
  ├── TurnIterationAdvanced(iteration=1)
  ├── build context
  ├── llm.complete(stream=True)
  ├── consume streamed text
  ├── CompletionRequested(candidate)
  ├── completion_blockers() == []
  ├── AssistantMessageRecorded(final=true)
  ├── TurnCompleted
  └── background MemoryManager.update()
```

### 4.2. Happy path có tool call

```text
User input
  │
  ├── UserMessageRecorded
  ├── TurnStarted
  ▼
Iteration 1
  │
  ├── model stream: tool call write(...)
  ├── AssistantToolCallsRecorded
  ├── ToolRequested              ┐
  ├── ToolValidated              │ durable lifecycle
  ├── ToolStarted                │ fsync trước side effect
  ├── sandbox writes file        │
  ├── ToolCompleted              ┘
  ├── ToolMessageRecorded
  └── continue
        │
        ▼
Iteration 2
  │
  ├── model sees tool result
  ├── returns final text
  ├── CompletionRequested
  ├── completion guard passes
  ├── AssistantMessageRecorded(final=true)
  └── TurnCompleted
```

### 4.3. Bắt đầu turn

[`Agent.run_turn()`](../src/agent/loop.py):

1. Reject nếu vẫn có `active_turn_id`.
2. Sinh `turn_id` mới.
3. Append user message, có thể chứa ảnh.
4. Append `TurnStarted` với goal, plan mode và optional `resumes_turn_id`.
5. Đi vào `_drive_turn()`.

Mỗi event qua `_event()` đều append xuống journal rồi `_refresh()` bằng cách đọc và replay toàn bộ journal. Cách này đơn giản và deterministic nhưng chi phí tăng theo độ dài session.

### 4.4. Build system prompt

[`Agent._build_system_message()`](../src/agent/loop.py) ghép ba nguồn:

```text
SYSTEM_PROMPT_TEMPLATE
├── PROJECT.md                    # facts bền vững
├── runtime_prompt_projection     # active turn + blocker
└── WorkspaceState.render         # sandbox/image/network/file counts
```

[`Agent._build_context()`](../src/agent/loop.py) lấy message history từ journal, bỏ system message cũ, rồi đặt system message mới ở đầu.

Do đó system prompt luôn phản ánh projection runtime mới nhất, thay vì tin vào system message đã ghi từ trước.

### 4.5. Compaction

[`Compactor.should_compact()`](../src/context/compactor.py) compact khi estimated tokens vượt:

```text
CONTEXT_WINDOW × 70%
```

Với config hiện tại, `CONTEXT_WINDOW = 256000`.

Compactor:

- giữ system messages;
- giữ ít nhất 10 non-system messages gần nhất;
- tóm tắt phần cũ bằng một model call riêng;
- không tách assistant tool call khỏi các tool result tương ứng;
- trả summary như một system message tạm thời.

Summary compaction **không được ghi vào journal**. Journal vẫn giữ history gốc; compacted context chỉ tồn tại cho lần gọi model đó.

### 4.6. Gọi model và nhận stream

[`llm.complete()`](../src/model/llm.py) là wrapper quanh `litellm.completion()`:

- model/base URL/API key lấy từ [`src/core/config.py`](../src/core/config.py);
- timeout mặc định 120 giây;
- không tự bật retry wrapper;
- `_drive_turn()` luôn gọi với `stream=True`.

[`Agent._consume_stream()`](../src/agent/loop.py):

- nối text delta thành `full_text`;
- nối từng fragment của function name/arguments theo `tool_call.index`;
- báo tiến độ mỗi khi tổng tool arguments tăng thêm ít nhất 8 KiB;
- chỉ trả tool call hoàn chỉnh sau khi stream kết thúc.

Vì tool arguments có thể chứa toàn bộ nội dung một file lớn, phần model sinh JSON `write.content` có thể lâu hơn rất nhiều so với thời gian sandbox thực sự ghi file.

### 4.7. Nhánh tool call

Nếu response có tool call:

1. Ghi nguyên assistant tool-call batch vào journal.
2. `ToolExecutor.request_batch()` tạo **tất cả** `ToolRequested` trước side effect đầu tiên.
3. Agent lần lượt execute từng item trong batch.
4. Mỗi result compact được ghi thành `ToolMessageRecorded`.
5. Agent chuyển sang iteration kế tiếp để model đọc kết quả.

Nếu user Ctrl+C giữa batch:

- execution đang chạy có thể được đánh dấu recovery required;
- các item còn lại bị `ToolCancelled` mà không chạy;
- turn nhận `TurnInterrupted`.

### 4.8. Nhánh final answer

Nếu model không gọi tool:

1. Response text trở thành candidate.
2. Ghi `CompletionRequested`.
3. Chạy `completion_blockers()`.
4. Nếu có blocker, ghi `CompletionBlocked` và gọi model lại.
5. Sau ba lần completion liên tiếp vẫn bị block, turn fail với category `repeated_incomplete_plan`.
6. Nếu không có blocker, ghi final assistant message rồi `TurnCompleted`.

Nếu plan được enforced, text stream không in ngay. Agent chỉ in candidate sau khi completion guard chấp nhận, tránh hiển thị “đã xong” trước khi plan thực sự hoàn tất.

### 4.9. Max iteration

`run_turn()` mặc định tối đa 20 iteration. Nếu vòng `for` hết mà chưa complete:

```text
TurnFailed(category=max_iterations)
```

Đây là loop guard chống model/tool lặp vô hạn.

---

## 5. Event journal: nguồn sự thật bền vững

### 5.1. Vì sao journal là authoritative?

`RuntimeState` không được serialize trực tiếp. Mỗi lần cần trạng thái mới, code gọi:

```text
events.jsonl ── replay() ── reduce_event() ──► RuntimeState
```

Lợi ích:

- có audit trail;
- crash giữa chừng vẫn biết event cuối đã được ghi;
- state dựng lại deterministic;
- validate được historical transition;
- không cần đồng bộ nhiều bản state mutable trên disk.

`PROJECT.md` không thay thế journal. Nó chỉ chứa facts để hỗ trợ reasoning của model.

### 5.2. Event envelope

[`EventStore.append_event()`](../src/memory/event_store.py) ghi mỗi dòng JSON với:

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "seq": 42,
  "ts": "UTC ISO-8601",
  "session_id": "...",
  "runtime_instance_id": "...",
  "event_type": "ToolStarted",
  "aggregate_type": "tool_execution",
  "aggregate_id": "execution-id",
  "turn_id": "turn-id",
  "causation_id": "...",
  "correlation_id": "turn-id",
  "payload": {}
}
```

Các guarantee chính:

- chỉ một writer nhờ `fcntl.flock(LOCK_EX | LOCK_NB)`;
- `seq` tăng đơn điệu;
- event ID không trùng;
- write xong gọi `flush()` và `os.fsync()`;
- journal sai schema/transition fail closed;
- JSON bị cắt ở dòng cuối có thể repair bằng `repair_trailing_partial()` và tạo backup.

### 5.3. Secret redaction

Payload được `_redact()` trước khi persist. Các key như:

```text
password, secret, token, api_key, authorization, credential
```

bị thay bằng `[REDACTED]`, kể cả khi nằm trong JSON string `arguments` có thể parse được.

`ToolExecutor` giữ arguments thật trong `_transient_arguments` để lần chạy hiện tại vẫn thực thi được. Sau restart, nếu execution chưa chạy mà persisted arguments đã redacted, code từ chối replay thay vì chạy với dữ liệu giả.

### 5.4. Message event và lifecycle event

Message events:

- `UserMessageRecorded`;
- `AssistantToolCallsRecorded`;
- `ToolMessageRecorded`;
- `AssistantMessageRecorded`.

`EventStore.to_messages()` chỉ projection các event này thành OpenAI-style message history.

Lifecycle events như `TurnStarted`, `ToolStarted`, `PlanStepCompleted` không đi trực tiếp vào message history. Chúng được reducer dùng để dựng runtime state, sau đó phần cần thiết được render vào system prompt.

### 5.5. Legacy compatibility

EventStore vẫn chấp nhận record cũ chỉ có `role` mà chưa có typed envelope. Reducer bỏ qua chúng về mặt lifecycle nhưng `to_messages()` vẫn chuyển chúng thành conversation history.

---

## 6. Reducer và các state machine thực tế

[`reduce_event()`](../src/runtime/reducer.py) mutate một `RuntimeState` projection. [`replay()`](../src/runtime/reducer.py) tạo state rỗng rồi áp toàn bộ event theo thứ tự.

### 6.1. Turn states

```text
                 TurnCompleted
              ┌───────────────► COMPLETED
              │
IDLE ─TurnStarted─► RUNNING ──TurnInterrupted─► INTERRUPTED
              │
              └────TurnFailed───────────────► FAILED
```

Invariant:

- không có hai active turn cùng lúc;
- không start trùng turn ID;
- iteration phải tăng đúng `+1`;
- terminal turn không được transition tiếp;
- terminal event xóa `active_turn_id`.

### 6.2. Plan step states

```text
PENDING ──start──► IN_PROGRESS ──complete──► COMPLETED
   │                    │
   │                    └──fail──► FAILED ──start──► IN_PROGRESS
   │
   └──caller skip──► SKIPPED
```

Invariant:

- chỉ một step được `IN_PROGRESS` trong active revision;
- complete chỉ đi từ `IN_PROGRESS`;
- model không được skip required work;
- caller có thể skip qua `Agent.skip_plan_step()` nhưng bắt buộc có reason;
- required step đã completed/skipped được giữ khi revise plan;
- model không được remove hoặc downgrade required step cũ.

### 6.3. Tool execution states

```text
                         policy ASK
                      ┌──────────────► WAITING_APPROVAL
                      │                       │
                      │              approve │ reject
                      │                       ▼
ToolRequested ─► PENDING ────────────────► CANCELLED
                  │
                  │ ToolStarted (fsynced)
                  ▼
               RUNNING ──success──► COMPLETED
                  │
                  ├──known failure─► FAILED
                  │
                  └──unknown outcome──► RECOVERY_REQUIRED
                                           │
                         recovered complete ├──► COMPLETED
                         recovered failed   ├──► FAILED
                         safe retry          └──► PENDING
```

Terminal tool states:

- `COMPLETED`;
- `FAILED`;
- `CANCELLED`.

Reducer từ chối transition tiếp từ terminal state, giúp một execution không chạy hai lần do replay nhầm.

### 6.4. Stale running execution

`ToolStarted` lưu `_started_runtime_instance_id`. Khi replay:

- nếu execution là `RUNNING`;
- và runtime instance hiện tại khác instance đã start;

thì projection tự đổi thành `RECOVERY_REQUIRED`.

Đây là projection rule, không sửa lịch sử journal. Sau khi user quyết định recovery, một event recovery mới mới được append.

### 6.5. `runtime.machine.py` đứng ở đâu?

[`StateMachine`](../src/runtime/machine.py) cung cấp `Transition`, guard và dispatch generic. Hiện reducer dùng branch/validation trực tiếp và không import module này. Vì vậy hãy xem `runtime.machine.py` là utility đã được test nhưng **chưa điều khiển runtime lifecycle hiện tại**.

---

## 7. Structured plan và điều kiện hoàn tất

### 7.1. `update_plan` là tool nội bộ

[`UpdatePlanTool`](../src/tools/plan.py) được thêm vào tool schemas cạnh OS tools, nhưng nó không gọi sandbox. `ToolExecutor` phát hiện `execute_runtime()` và truyền:

- `execution_id`;
- `turn_id`;
- arguments.

Sau đó tool gọi [`Agent._apply_plan_action()`](../src/agent/loop.py), nơi append plan event vào chính runtime journal.

### 7.2. Các action

| Action | Tác dụng |
|---|---|
| `create` | Tạo plan mới, sinh step ID `step_1`, `step_2`, ... |
| `set_step_status` | Chuyển step thành `in_progress`, `completed` hoặc `failed`. |
| `revise` | Tạo revision mới và bảo toàn required work đã chốt. |

Schema có enum `skipped`, nhưng implementation chủ động reject model skip. Chỉ caller/user-side API mới được skip kèm lý do.

### 7.3. Completion policy

Mỗi step có:

- `SELF_ATTESTED`: complete cần note không rỗng;
- `EVIDENCE_REQUIRED`: complete cần ít nhất một execution ID đã `COMPLETED` trong cùng session.

Evidence là runtime-owned `execution_id`, không phải provider `tool_call_id`.

### 7.4. Completion blockers

[`completion_blockers()`](../src/runtime/reducer.py) chặn final answer khi:

1. turn bắt buộc plan nhưng chưa có plan;
2. còn required step chưa completed/skipped;
3. còn tool execution ở `WAITING_APPROVAL`, `RUNNING` hoặc `RECOVERY_REQUIRED`.

`PENDING` execution không nằm trong completion blockers vì pending batch phải được drain trước khi model tiếp tục; resume path xử lý việc này bằng `_drain_pending_executions()`.

---

## 8. Tool lifecycle: từ JSON của model đến side effect

### 8.1. Hai ID khác nhau

```text
Model/provider                        Runtime
──────────────                        ───────
tool_call_id = call_abc      ─────►  execution_id = UUID
                                       │
                                       ├── lifecycle events
                                       ├── approval/recovery
                                       └── evidence cho plan
```

`tool_call_id` dùng để ghép assistant tool call với tool message trong protocol của model.

`execution_id` do runtime sở hữu, dùng để bảo đảm lifecycle, audit và recovery không phụ thuộc provider.

### 8.2. `request_batch()`

[`ToolExecutor.request_batch()`](../src/runtime/executor.py):

1. Sinh `execution_id` cho mỗi call.
2. Lookup tool để lấy replay policy.
3. Parse JSON arguments.
4. Giữ arguments thật trong memory.
5. Append `ToolRequested` cho từng call.
6. Chỉ sau khi toàn bộ batch đã được persist, Agent mới execute item đầu tiên.

Nếu process crash sau bước này nhưng trước side effect, các item vẫn ở `PENDING` và có thể được resume/drain.

### 8.3. `execute()` theo thứ tự

```text
replay current state
  │
  ├── execution phải PENDING
  ├── tool phải tồn tại
  ├── arguments phải parse được
  ├── redacted persisted args không được replay
  ├── tool.validate(arguments)
  ├── ToolValidated
  ├── policy: ALLOW / ASK / DENY
  ├── tool.recovery_metadata()
  ├── ToolStarted + fsync
  ├── tool.execute_runtime() hoặc tool.run()
  └── ToolCompleted hoặc ToolFailed
```

### 8.4. Invariant quan trọng nhất

```text
ToolStarted được append + fsync TRƯỚC side effect
```

Nếu persist `ToolStarted` thất bại, tool không được chạy.

Nếu process chết sau `ToolStarted` nhưng trước terminal event, runtime không đoán tool thành công hay thất bại. Lần replay mới biến nó thành `RECOVERY_REQUIRED`.

### 8.5. Policy và approval

`ToolExecutor` hỗ trợ callback policy:

- `ALLOW`: chạy;
- `DENY`: append cancel, không chạy;
- `ASK`: append `ToolApprovalRequested`.

Factory mặc định hiện không truyền custom policy, nên default là `ALLOW`. Approval workflow vẫn tồn tại cho caller tích hợp policy khác hoặc journal cũ đang chờ.

Approval/rejection bắt buộc có note khi resolve qua CLI. Rejection kết thúc execution ở `CANCELLED`.

### 8.6. `ToolResult`

[`ToolResult`](../src/tools/base.py) có:

| Field | Ý nghĩa |
|---|---|
| `raw` | Kết quả đầy đủ để audit trong tool lifecycle payload. |
| `compact` | Bản ngắn gửi trở lại model qua tool message. |
| `success` | Kết quả machine-readable. |
| `exit_code` | Exit code nếu là process. |
| `metadata` | Sandbox ID, image, hash, violation status... |

Bash compact output còn tối đa 2.000 ký tự cuối, trong khi helper sandbox tự giới hạn tổng capture ở mức thấp hơn hard ceiling 10 MiB.

---

## 9. Các tool model nhìn thấy

[`ToolRegistry`](../src/tools/registry.py) đăng ký bốn OS tool:

```text
read, write, edit, bash
```

Agent thêm `update_plan` riêng khi gửi schemas cho model.

| Tool | Implementation | Replay policy | Boundary |
|---|---|---|---|
| `read` | `ReadTool` | `REPLAY_SAFE` | `sandbox.fs_call(read)` |
| `write` | `WriteTool` | `RECONCILABLE` | `sandbox.fs_call(write)` |
| `edit` | `EditTool` | `RECONCILABLE` | `sandbox.fs_call(edit)` |
| `bash` | `BashTool` | `MANUAL` | `sandbox.exec()` |
| `update_plan` | `UpdatePlanTool` | `REPLAY_SAFE` | Agent runtime, không qua OS |

### 9.1. Fail closed khi không có sandbox

Compatibility registry toàn cục được tạo với `sandbox=None`. Nếu code cũ gọi OS tool không bind session:

- filesystem tool trả `SandboxUnavailable`;
- bash từ chối host shell execution.

Không có fallback chạy lệnh trực tiếp trên host.

### 9.2. Read

`ReadTool`:

- chỉ chấp nhận path tương đối;
- đọc UTF-8 regular file qua helper;
- đánh số dòng;
- compact tối đa 50 dòng đầu.

### 9.3. Write

`WriteTool` trước khi chạy ghi recovery metadata:

```text
path
before_hash hoặc __missing__
expected_after_hash = sha256(content)
```

Sau crash, runtime có thể:

- xác nhận completed nếu fingerprint hiện tại bằng expected hash;
- retry nếu fingerprint vẫn bằng before hash;
- từ chối tự đoán nếu file ở trạng thái khác.

### 9.4. Edit

`EditTool` yêu cầu `old_string` xuất hiện đúng một lần. Helper đọc file, xác minh uniqueness rồi atomic replace.

Recovery metadata hiện lưu `before_hash`. Điều này hỗ trợ safe retry khi file vẫn giữ nguyên trạng thái trước edit. Khác với `write`, metadata hiện không tính trước `expected_after_hash`, nên đường reconcile “đã completed” không có cùng bằng chứng mạnh như write.

### 9.5. Bash

`BashTool` tạo `ExecRequest` với:

- command string;
- workspace-relative cwd;
- timeout mặc định 60 giây.

Replay policy là `MANUAL` vì runtime không thể suy ra một shell command tùy ý đã gây side effect gì. Khi outcome không rõ, user chỉ được đánh dấu completed/failed, không được retry tự động.

---

## 10. Theo dấu cụ thể một lệnh `write`

Giả sử model sinh:

```json
{
  "name": "write",
  "arguments": "{\"path\":\"hello.txt\",\"content\":\"hello\"}"
}
```

Luồng đầy đủ:

```text
Agent._consume_stream
  │ ghép arguments fragments
  ▼
AssistantToolCallsRecorded
  ▼
ToolExecutor.request_batch
  │ parse JSON
  │ ToolRequested(execution_id, tool_call_id, args, reconcilable)
  ▼
ToolExecutor.execute
  │ validate path + content tồn tại
  │ ToolValidated
  │ WriteTool.recovery_metadata
  │   ├── SandboxSession.fingerprint("hello.txt")
  │   └── DockerBackend.fs_call({operation:fingerprint})
  │
  │ ToolStarted(before_hash, expected_after_hash) + fsync
  ▼
WriteTool.execute
  ▼
SandboxSession.fs_call({operation:write, path, content})
  ▼
DockerBackend._helper
  │ docker exec -i <child> python /opt/.../sandbox_fs.py
  ▼
sandbox_fs.handle
  │ validate relative path
  │ walk parent bằng dir_fd + O_NOFOLLOW
  │ write temp file + fsync
  │ os.replace trong cùng directory
  │ fsync parent directory
  ▼
JSON {success:true, sha256:...}
  ▼
ToolCompleted
  ▼
ToolMessageRecorded("Successfully wrote hello.txt")
  ▼
model iteration kế tiếp
```

Trong **live mode**, `/workspace` là bind mount RW của source workspace, nên `hello.txt` xuất hiện trên host ngay tại thời điểm helper `os.replace()` thành công.

---

## 11. Docker sandbox và security contract

### 11.1. Preflight

[`DockerBackend.preflight()`](../src/sandbox/docker.py) kiểm tra:

1. Docker daemon báo security option rootless.
2. Cgroup version là 2.
3. Immutable image ID/digest tồn tại local và match chính xác.
4. Image config khai báo non-root user.
5. Lưu daemon ID để làm evidence.

Thiếu bất kỳ điều kiện nào đều fail trước khi child được tạo.

### 11.2. Container create flags

Sandbox child được tạo với:

```text
--pull never
--network none
--read-only
--cap-drop ALL
--security-opt no-new-privileges:true
--pids-limit <N>
--memory <bytes>
--memory-swap <same bytes>
--cpus <N>
--tmpfs /tmp:rw,nosuid,nodev,noexec,size=<bytes>
--mount type=bind,...,dst=/workspace
--workdir /workspace
```

Container còn có labels cho:

- managed flag;
- session ID;
- workspace identity;
- image identity;
- workspace mode.

### 11.3. Default resource profile

Từ [`ResourceLimits`](../src/sandbox/models.py):

| Resource | Default | Hard ceiling |
|---|---:|---:|
| CPU | 2 | 4 |
| Memory | 2 GiB | 4 GiB |
| PIDs | 256 | 512 |
| `/tmp` | 512 MiB | 1 GiB |
| Tool timeout | 60 s | 600 s |
| Captured output | 10 MiB | 10 MiB |
| Workspace growth | 4 GiB | 8 GiB |

### 11.4. Verify sau khi start

Sau `docker start`, [`verify_runtime()`](../src/sandbox/docker.py) kiểm tra hai lớp.

**Lớp Docker inspect:**

- đúng container ID;
- đang running;
- đúng image/user/mount;
- mount workspace RW;
- network none;
- rootfs read-only;
- đúng CPU/RAM/PID;
- drop all caps;
- no-new-privileges;
- mọi mask mount đều đúng source và read-only.

**Lớp probe trong container:**

- `/proc/self/status`: `NoNewPrivs=1`, effective capabilities bằng 0;
- cgroup files: memory/swap/PID/CPU đúng limit;
- route table không có route network ngoài header;
- `/` read-only;
- `/tmp` có `rw,nosuid,nodev,noexec`;
- không có writable mount ngoài allowlist.

Nếu probe fail, session stop child, persist trạng thái `ERROR` và không cho Agent dùng sandbox chưa được chứng minh an toàn.

### 11.5. Ambiguous transport

Nếu `docker exec` timeout, transport lỗi hoặc helper trả malformed JSON, runtime không biết side effect đã xảy ra chưa.

[`DockerBackend._helper()`](../src/sandbox/docker.py) sẽ cố stop container và inspect lại. Nếu không chứng minh được child đã dừng, nó nâng lỗi “outcome is ambiguous”. Tool lifecycle sau đó cần recovery thay vì tự retry.

---

## 12. Helper chạy bên trong sandbox

### 12.1. `sandbox_fs.py`

[`sandbox_fs.py`](../sandbox-image/sandbox_fs.py) không ghép path rồi tin vào string. Nó dùng descriptor-relative traversal:

1. `_parts()` reject absolute path, backslash, NUL, `..`, path chưa normalized và `.`.
2. Mở `/workspace` thành directory FD.
3. Mở từng parent với `O_DIRECTORY | O_NOFOLLOW`.
4. Target read với `O_NOFOLLOW`.
5. Chỉ chấp nhận regular file.

Cách này giảm race/symlink traversal so với chỉ gọi `Path.resolve()` một lần rồi thao tác bằng pathname.

Các operation:

| Operation | Hành vi |
|---|---|
| `read` | Đọc tối đa 32 MiB, decode UTF-8. |
| `fingerprint` | Trả SHA-256 và size. |
| `write` | Ghi temp file, fsync, chmod, atomic replace, fsync parent. |
| `edit` | Đọc, yêu cầu unique match, ghi atomically và check before hash. |

### 12.2. `sandbox_exec.py`

[`sandbox_exec.py`](../sandbox-image/sandbox_exec.py):

- validate `cwd` vẫn nằm dưới `/workspace`;
- chạy `/bin/bash -lc <command>`;
- tạo process session mới;
- capture stdout/stderr bằng selector;
- giới hạn tổng output;
- khi timeout: SIGTERM cả process group, chờ 0,5 giây rồi SIGKILL;
- trả JSON chứa status, output, exit code và `truncated`.

Sandbox shell vẫn là shell tự do, không dùng closed verb set trong `tools/verbs.py`. Isolation vật lý của child là boundary chính cho bash.

---

## 13. Live workspace: mode mặc định hiện tại

### 13.1. Mount trực tiếp

Trong live mode:

```text
host source workspace ── bind RW ──► sandbox /workspace
```

Tool thay đổi file trong child thì host nhìn thấy ngay. Không có bước copy-back hay apply.

### 13.2. Vì sao runtime user là `0:0`?

Sandbox image khai báo user `65532:65532` để preflight bảo đảm image không mặc định root.

Riêng live container được override thành `--user 0:0`. Vì daemon là rootless:

```text
UID 0 bên trong user namespace
        │ maps to
        ▼
UID không đặc quyền của user đang chạy rootless Docker trên host
```

Mục đích là file sinh ra trên bind mount thuộc đúng host user, không phải UID 65532 lạ. Các lớp bảo vệ khác vẫn giữ nguyên: no caps, no-new-privileges, rootfs RO, no network và cgroup limits.

### 13.3. Mask policy

[`prepare_live_workspace()`](../src/sandbox/workspace.py) scan workspace và tạo read-only bind masks cho entry đang tồn tại thuộc:

1. hard security denylist: `.git`, `.env*`, SSH/AWS/Docker credentials, key/cert, token/secret patterns...;
2. project `.agentignore`;
3. cache/build patterns: `.venv`, `node_modules`, `__pycache__`, `build`, `dist`, ...

Directory bị exclude không được scan sâu tiếp.

Mỗi excluded file được che bằng `mask-file`; mỗi excluded directory được che bằng `mask-directory`. Các mount này được verify là read-only sau container start và khi resume.

### 13.4. `.gitignore` không phải access-control

Live mode **không** dùng `.gitignore` để mask. Một file bị Git ignore vẫn có thể là output hợp lệ agent cần tạo hoặc sửa.

Phân biệt:

```text
.gitignore   = file nào Git không version
.agentignore = entry nào agent không được truy cập
hard denylist = secret/control data luôn bị che nếu đã tồn tại lúc scan
```

Shadow snapshot vẫn dùng `.gitignore` như performance exclusion vì nó phải copy dữ liệu sang workspace riêng.

### 13.5. Giới hạn mask hiện tại

Mask list được chụp lúc tạo session và persist trong metadata. Vì đây là bind mask theo **entry đang tồn tại**:

- secret có sẵn lúc startup được che;
- session resume dùng lại đúng mask cũ;
- path nhạy cảm mới được tạo sau startup không tự động sinh mount mask mới;
- bash tự do và filesystem helper không có lớp pattern denylist động riêng.

Đây là giới hạn V1 cần hiểu khi đánh giá live mode. Không nên xem mask snapshot là policy engine theo dõi mọi pathname mới theo thời gian thực.

### 13.6. Control commands trong live mode

- `/changes`: báo dùng `git diff` hoặc IDE.
- `/apply`: báo thay đổi đã ở workspace.
- `/discard`: destroy sandbox nhưng **không rollback live changes**.
- thoát bình thường: stop child; thay đổi file vẫn còn.

Undo/review là trách nhiệm của Git/IDE trên host.

### 13.7. Watchdog và disk budget

[`SandboxSession.start_watchdog()`](../src/sandbox/session.py) mỗi 5 giây:

- touch heartbeat;
- nếu đang running, kiểm tra workspace growth và free-space floor.

Nếu vượt budget hoặc host thiếu free space:

- stop child;
- set status `ERROR`;
- persist error metadata.

---

## 14. Shadow workspace: luồng legacy

Shadow mode không phải default cho session mới, nhưng code vẫn hoàn chỉnh để tương thích.

### 14.1. Snapshot

[`prepare_workspace()`](../src/sandbox/workspace.py):

1. Xóa/recreate state workspace riêng.
2. Scan source không follow symlink.
3. Exclude hard denylist, `.agentignore`, Git-ignored và performance paths.
4. Reject symlink thoát workspace, special inode, collision case/Unicode.
5. Copy regular file theo chunks và hash lại để phát hiện source đổi giữa lúc copy.
6. Sanitize mode thành `0644` hoặc `0755`.
7. Tạo bare shadow Git repo và commit baseline.
8. Persist baseline manifest.

Sandbox mount state workspace copy, không mount source thật.

### 14.2. Seal changes

`/changes` gọi:

```text
SandboxSession.prepare_changes()
  ├── stop sandbox
  ├── build_changeset()
  ├── persist final manifest + changeset
  └── status = SEALED
```

[`build_changeset()`](../src/sandbox/changes.py):

- so baseline với final scan;
- classify create/modify/delete;
- tạo unified diff cho UTF-8 text;
- đánh dấu binary;
- reject denylisted changes và mọi symlink mutation;
- enforce path/file/total/entry limits;
- canonicalize rồi hash nguyên changeset.

Nếu có violation thì `change_set_hash=None` và changeset không approvable.

### 14.3. Whole-set apply

`/apply` yêu cầu user nhập chính xác:

```text
approve <change_set_hash>
```

và note không rỗng.

[`apply_changeset()`](../src/sandbox/changes.py):

1. Verify exact approved hash.
2. Rebuild changeset để chắc sealed data chưa đổi.
3. Stage file và verify hash.
4. Lock workspace theo workspace identity.
5. Check conflict trên mọi changed path trước khi mutation.
6. Ghi durable apply journal.
7. Backup file cũ.
8. Atomic copy hoặc delete từng path.
9. Verify toàn bộ kết quả.
10. Xóa staging/backup/journal khi thành công.

Nếu bất kỳ operation fail, code rollback theo thứ tự ngược. Nếu rollback cũng fail, journal giữ state `recovery_required` để lần startup sau `recover_incomplete_apply()` xử lý hoặc yêu cầu recovery thủ công.

### 14.4. Vì sao vẫn giữ shadow code?

`SandboxSession.__init__()` xem metadata không có `workspace_mode` là legacy shadow. Điều này cho phép resume session được tạo trước khi live mode trở thành mặc định mà không mount nhầm source workspace.

---

## 15. Resume, interruption và recovery

### 15.1. Resume đúng sandbox

[`SandboxSession.resume()`](../src/sandbox/session.py) không chỉ gọi `docker start`. Trước hết nó inspect và đối chiếu:

- exact container ID;
- image identity;
- workspace mount source và RW mode;
- resource limits;
- network/rootfs/user;
- managed/session/workspace/mode labels;
- cap drop + no-new-privileges;
- toàn bộ persisted masks.

Mismatch bất kỳ field nào → fail closed với `sandbox reconciliation mismatch`.

Sau reconciliation, `start()` chạy lại runtime probe rồi mới set `RUNNING`.

### 15.2. Resume active turn

[`Agent.resume_active_turn()`](../src/agent/loop.py):

1. Yêu cầu có active turn.
2. Từ chối nếu còn pending approval/recovery action.
3. Drain các tool execution `PENDING` của turn.
4. Nếu final assistant message đã persist nhưng `TurnCompleted` chưa persist, append `TurnCompleted` mà không gọi model lại.
5. Nếu chưa có final, tiếp tục `_drive_turn()`.

User message cũ không được append lần nữa.

### 15.3. Approval khi startup

REPL xử lý pending action trước prompt mới:

```text
WAITING_APPROVAL
  ├── approve + note ─► execute tool
  └── reject + note  ─► cancel tool
```

Nếu execution dùng manual replay policy, menu recovery không cho chọn retry.

### 15.4. Recovery decision

Với `RECOVERY_REQUIRED`, user chọn:

- `completed`: xác nhận side effect đã hoàn tất;
- `failed`: xác nhận side effect thất bại;
- `retry`: chỉ có khi replay policy không phải manual.

Với `RECONCILABLE`:

- retry yêu cầu current fingerprint bằng `before_hash`;
- completed yêu cầu current fingerprint bằng `expected_after_hash`.

Với `MANUAL`, runtime không tự chạy lại vì có thể gây side effect lần hai.

### 15.5. Ctrl+C khi model đang stream

`_drive_turn()` bắt `KeyboardInterrupt` quanh model call:

- in cancelled;
- append `TurnInterrupted(reason=user)`;
- return text đã có trước đó nếu có.

Không có tool side effect mới nếu chưa đi đến execute.

### 15.6. Ctrl+C khi tool đang chạy

Nếu interruption xảy ra sau `ToolStarted` mà outcome chưa rõ:

- execution được giữ/đổi thành `RECOVERY_REQUIRED`;
- tool message ghi rằng outcome cần recovery;
- các tool còn lại trong batch bị cancel mà không execute;
- turn bị interrupt.

Điều này ưu tiên không chạy trùng side effect hơn việc tự động “thử lại cho chắc”.

---

## 16. Memory dài hạn và context ngắn hạn

### 16.1. `PROJECT.md` thực tế nằm ở đâu?

Default của [`MemoryManager`](../src/memory/manager.py) là:

```text
<state-root>/projects/default/PROJECT.md
```

File tracked [`src/memory/private/PROJECT.md`](../src/memory/private/PROJECT.md) **không phải default runtime path** trong code hiện tại. Nó là file repository cũ/mẫu dữ liệu, không nên dùng để suy luận memory mà session đang đọc nếu chưa kiểm tra state root.

### 16.2. Đọc memory

Mỗi lần build system prompt, Agent gọi `memory_manager.read()`. Do đó facts append sau turn trước có thể vào prompt turn sau.

### 16.3. Cập nhật memory

Sau khi turn `COMPLETED`:

1. Agent render tối đa 20 message gần nhất.
2. Spawn daemon thread.
3. Gọi model bằng prompt yêu cầu JSON `{project_md_append: ...}`.
4. Nếu parse được và nội dung không rỗng, append vào `PROJECT.md`.

Memory update không block final response. Nếu process thoát ngay, daemon thread không được guarantee hoàn tất.

### 16.4. Ba loại “state” không nên nhầm

| State | Nguồn | Mục đích |
|---|---|---|
| Runtime lifecycle | JSONL journal | Authoritative turn/plan/tool/recovery. |
| Project memory | `PROJECT.md` | Facts hỗ trợ reasoning qua nhiều chat. |
| Workspace projection | baseline manifest + sandbox metadata | Thông tin môi trường đưa vào prompt. |

---

## 17. Vòng đời container và dữ liệu trên disk

### 17.1. Khi thoát bình thường

`Agent.close()`:

- stop sandbox nếu status là running/created/error;
- đóng và unlock journal.

Stop không đồng nghĩa remove. Child container có thể còn ở trạng thái stopped để session resume.

Control-plane container được chạy bằng Compose `--rm`, nên nó biến mất sau exit. Điều này không có nghĩa sandbox child cũng bị remove.

### 17.2. Khi `/discard`

`SandboxSession.destroy()`:

- stop watchdog;
- `docker rm --force` exact child;
- set status `DESTROYED`;
- persist destroy reason.

Live workspace changes vẫn ở host vì destroy mount/container không hoàn tác file đã ghi.

### 17.3. Cleanup session hết hạn

[`cleanup_expired()`](../src/sandbox/session.py) có thể:

- scan sandbox state directories;
- chỉ chọn metadata `managed=true` và đúng session ID;
- destroy exact container;
- xóa session state directory khi quá TTL.

Hiện function này được test nhưng **không được CLI/main tự động gọi**. Muốn production cleanup định kỳ cần caller/scheduler riêng.

---

## 18. Vì sao đôi lúc agent trông như “thinking” rất lâu?

Một turn có nhiều vùng thời gian khác nhau:

```text
[build image]
      + [sandbox preflight/start/probe]
      + [model generates text/tool JSON]
      + [tool executes in sandbox]
      + [model reads result and generates final]
      + [optional background memory update]
```

### 18.1. Dấu hiệu quan sát

- `[model] waiting for response (iteration N)...`: đang chờ/stream từ provider.
- `[model] receiving tool call write: X KiB`: model đang sinh arguments lớn, chưa chắc tool đã chạy.
- `tool> write`: Agent đã nhận xong call và bắt đầu tool lifecycle.
- `[memory] updating PROJECT.md...`: turn đã complete, memory update chạy background.

### 18.2. Timeout khác nhau

- LLM request: mặc định 120 giây trong `llm.complete()`.
- Bash request: mặc định 60 giây.
- Sandbox helper transport cho bash: request timeout + 2 giây.
- Filesystem helper transport: 65 giây.

Một file HTML 40–50 KiB được truyền nguyên trong JSON string có thể tốn nhiều thời gian model generation, trong khi atomic filesystem write chỉ mất một phần rất nhỏ.

---

## 19. Failure map

| Triệu chứng | Component phát hiện | Hành vi |
|---|---|---|
| Docker không rootless | `DockerBackend.preflight()` | Không tạo child. |
| Cgroups không phải v2 | `DockerBackend.preflight()` | Không tạo child. |
| Image là mutable tag/sai digest | `DockerBackend.__init__/preflight` | Fail closed. |
| Runtime flags/mount/cgroup sai | `verify_runtime()` | Stop child, session error. |
| Workspace có FIFO/special inode | workspace scan | Reject toàn session. |
| Symlink thoát workspace | workspace scan/helper | Reject hoặc blocked. |
| Tool JSON invalid | `ToolExecutor` | `ToolFailed`, không `ToolStarted`. |
| Thiếu required argument | tool validation | `ToolFailed`, không side effect. |
| Helper/transport outcome mơ hồ | `DockerBackend` + executor | Stop child, recovery required. |
| Model provider lỗi | `Agent._drive_turn()` | `TurnFailed(category=model)`. |
| Final khi plan chưa xong | completion guard | `CompletionBlocked`; lần ba fail turn. |
| Hết 20 iteration | Agent loop | `TurnFailed(category=max_iterations)`. |
| Journal transition sai | reducer/replay | `JournalCorruptionError`. |
| Hai process mở cùng journal | EventStore lock | `JournalLockedError`. |
| Shadow host conflict | apply precheck | Reject toàn changeset, không mutate. |

---

## 20. Bản đồ test theo subsystem

Repository dùng `unittest`. Các test file là nơi tốt nhất để xem behavioral contract ngắn gọn.

### 20.1. Agent, CLI và model

| Test file | Phạm vi |
|---|---|
| [`test_main.py`](../tests/test_main.py) | Chat selection/resume, REPL control commands, sandbox factory, approval CLI. |
| [`test_agent_loop.py`](../tests/test_agent_loop.py) | Model stream, tool batches, interruption, final messages, progress và memory. |
| [`test_model_llm.py`](../tests/test_model_llm.py) | Timeout mặc định và explicit retry passthrough. |
| [`test_compactor.py`](../tests/test_compactor.py) | Threshold, summary và atomic tool interaction groups. |

### 20.2. Journal, reducer, plan và recovery

| Test file | Phạm vi |
|---|---|
| [`test_event_store.py`](../tests/test_event_store.py) | Envelope, fsync/lock, corruption, redaction, legacy messages. |
| [`test_reducer.py`](../tests/test_reducer.py) | Deterministic replay, strict transitions, blockers, stale running. |
| [`test_plan_lifecycle.py`](../tests/test_plan_lifecycle.py) | Required/optional plan, evidence, step guards, completion. |
| [`test_tool_lifecycle.py`](../tests/test_tool_lifecycle.py) | Requested→validated→started→terminal, batch ordering, policy và persist-before-effect. |
| [`test_recovery.py`](../tests/test_recovery.py) | Crash/replay, approval, fingerprints, pending batch và persisted final. |
| [`test_machine.py`](../tests/test_machine.py) | Generic state machine utility độc lập. |

### 20.3. Sandbox contract

| Test file | Phạm vi |
|---|---|
| [`test_sandbox_models.py`](../tests/test_sandbox_models.py) | Safe path, hard resource ceilings và control paths. |
| [`test_workspace_snapshot.py`](../tests/test_workspace_snapshot.py) | Denylist, `.agentignore`, Git ignore, symlink/special inode, live masks. |
| [`test_docker_backend.py`](../tests/test_docker_backend.py) | Docker argv, preflight, inspect/probe và ambiguous transport. |
| [`test_sandbox_session.py`](../tests/test_sandbox_session.py) | Session transition, metadata, resume, watchdog, live/shadow. |
| [`test_sandbox_helpers.py`](../tests/test_sandbox_helpers.py) | `sandbox_fs.py` và `sandbox_exec.py` trực tiếp. |
| [`test_sandbox_tools.py`](../tests/test_sandbox_tools.py) | Tool chỉ delegate qua bound sandbox và fail closed. |
| [`test_compose_launcher.py`](../tests/test_compose_launcher.py) | Compose/entrypoint build child image và truyền immutable ID. |
| [`test_sandbox_integration.py`](../tests/test_sandbox_integration.py) | Real rootless Docker: network, cgroup, mask, ownership, resume, apply/rollback. |

### 20.4. Legacy changeset

| Test file | Phạm vi |
|---|---|
| [`test_changeset.py`](../tests/test_changeset.py) | Deterministic diff/hash, violation và limits. |
| [`test_changeset_apply.py`](../tests/test_changeset_apply.py) | Exact hash, conflict, staging, atomic apply và rollback. |

### 20.5. Chọn test khi sửa code

```text
Sửa Agent loop       → test_agent_loop + plan/recovery/tool lifecycle
Sửa journal/reducer  → test_event_store + test_reducer + recovery
Sửa tool             → test_sandbox_tools + test_tool_lifecycle
Sửa path/helper      → test_sandbox_models + helpers + workspace snapshot
Sửa Docker/session   → backend + session + compose + integration
Sửa shadow apply     → changeset + changeset_apply + integration
```

Real integration tests có thể skip nếu môi trường không có rootless Docker/cgroups v2/image phù hợp. Unit test pass không thay thế runtime integration cho security contract.

---

## 21. Những module dễ gây hiểu nhầm

### 21.1. `tools/verbs.py`

File này mô tả closed PowerShell verb set và danger levels, nhưng:

- không được import bởi `ToolRegistry`;
- tool shell thật hiện tại là `BashTool` với command tự do;
- sandbox image là Linux, không phải Windows PowerShell.

Do đó không dùng `VERBS` để suy luận policy production hiện tại.

### 21.2. `runtime/machine.py`

Generic state machine có test nhưng reducer production dùng transition validation riêng. Hai nơi chưa được nối với nhau.

### 21.3. `src/memory/private/PROJECT.md`

Không phải memory default của `MemoryManager`. Runtime default dùng file ngoài repo dưới state root.

### 21.4. Tài liệu thiết kế cũ

Các file trong [`docs/`](../docs/) chứa lịch sử thiết kế, implementation plan và operations. Chúng hữu ích để biết lý do kiến trúc, nhưng khi có khác biệt thì ưu tiên theo thứ tự:

```text
source hiện tại + tests hiện tại
        > operations doc
        > design/implementation plan cũ
```

---

## 22. Lộ trình đọc source đề xuất

### 22.1. Tuyến ngắn: hiểu khoảng 80% hệ thống

Đọc theo thứ tự:

1. [`compose.yaml`](../compose.yaml)
2. [`docker-entrypoint.sh`](../docker-entrypoint.sh)
3. [`src/main.py`](../src/main.py)
4. [`src/agent/loop.py`](../src/agent/loop.py)
5. [`src/memory/event_store.py`](../src/memory/event_store.py)
6. [`src/runtime/reducer.py`](../src/runtime/reducer.py)
7. [`src/runtime/executor.py`](../src/runtime/executor.py)
8. [`src/tools/registry.py`](../src/tools/registry.py)
9. [`src/sandbox/session.py`](../src/sandbox/session.py)
10. [`src/sandbox/docker.py`](../src/sandbox/docker.py)
11. [`sandbox-image/sandbox_fs.py`](../sandbox-image/sandbox_fs.py)
12. [`sandbox-image/sandbox_exec.py`](../sandbox-image/sandbox_exec.py)

### 22.2. Tuyến theo một lần write

```text
Agent._consume_stream
→ ToolExecutor.request_batch
→ ToolExecutor.execute
→ WriteTool
→ SandboxSession.fs_call
→ DockerBackend._helper
→ sandbox_fs.handle
```

### 22.3. Tuyến theo một final answer

```text
Agent._drive_turn
→ CompletionRequested
→ completion_blockers
→ AssistantMessageRecorded(final)
→ TurnCompleted
→ MemoryManager.update (background)
```

### 22.4. Tuyến theo restart

```text
select_chat_path
→ _default_agent_factory
→ SandboxSession.__init__ từ metadata
→ SandboxSession.resume + reconcile
→ Agent.__init__
→ EventStore.read_all
→ replay
→ pending_runtime_actions
→ resume_active_turn
```

### 22.5. Tuyến theo security boundary

```text
ResourceLimits/SandboxPath
→ prepare_live_workspace hoặc prepare_workspace
→ DockerBackend.preflight/create/verify_runtime
→ SandboxSession
→ filesystem/terminal tools
→ sandbox_fs/sandbox_exec
→ rootless integration tests
```

---

## 23. Các invariant cần nhớ

1. JSONL journal là nguồn sự thật của runtime lifecycle.
2. Chỉ một active turn trong một session.
3. Mỗi model tool call có một runtime execution ID riêng.
4. Toàn bộ batch `ToolRequested` được persist trước side effect đầu tiên.
5. `ToolStarted` phải fsync trước khi tool chạy.
6. Terminal execution không được chạy lại.
7. Unknown outcome không được biến thành success bằng phỏng đoán.
8. Bash unknown outcome không auto retry.
9. Final answer phải qua completion blockers.
10. Model không được tự skip required plan work.
11. OS tools không bind sandbox phải fail closed.
12. Sandbox image phải immutable và daemon phải rootless/cgroups v2.
13. Container runtime phải được inspect và probe sau start.
14. Live changes xuất hiện ngay; `/discard` không rollback workspace.
15. `.gitignore` không phải security policy của live mode.
16. Shadow apply là exact whole-set apply, không apply từng phần.
17. Resume phải reconcile identity và security contract trước khi tiếp tục.
18. `PROJECT.md` hỗ trợ reasoning nhưng không quyết định lifecycle.

---

## 24. Tóm tắt toàn bộ hệ thống trong một sơ đồ

```text
USER
 │
 │ input / resume / control command
 ▼
main.py REPL
 │
 ├──── session selection ──────────────► EventStore JSONL
 │                                           │
 │                                           │ replay
 │                                           ▼
 │                                      RuntimeState
 │                                  turn / plan / execution
 │
 ▼
Agent.run_turn / resume_active_turn
 │
 ├──── build prompt
 │       ├── PROJECT.md
 │       ├── RuntimeState projection
 │       ├── WorkspaceState projection
 │       └── message history ── optional Compactor
 │
 ├──── LiteLLM stream
 │       ├── text delta
 │       └── tool-call fragments
 │
 ├──── no tool ──► completion blockers ──► final + TurnCompleted
 │
 └──── tool batch
         │ persist all ToolRequested
         ▼
     ToolExecutor
         │ validate / policy / approval
         │ recovery metadata
         │ persist + fsync ToolStarted
         ▼
     Bound Tool
         ├── update_plan ──────────────► runtime events
         ├── read/write/edit ──────────► SandboxSession.fs_call
         └── bash ─────────────────────► SandboxSession.exec
                                              │
                                              ▼
                                         DockerBackend
                                              │ docker exec
                                              ▼
                                   rootless sandbox child
                                   ├── sandbox_fs.py
                                   ├── sandbox_exec.py
                                   └── /workspace bind mount
                                              │
                                              ▼
                                      LIVE HOST WORKSPACE

Mọi kết quả quay lại theo chiều ngược:
helper JSON → ToolResult → terminal tool event → ToolMessageRecorded
→ iteration model kế tiếp → completion guard → final answer.
```

Nếu chỉ ghi nhớ một câu, hãy ghi nhớ câu này:

> **Coding Agent là một event-driven control plane gọi model, còn mọi OS side effect được bọc trong durable tool lifecycle và đẩy qua một rootless Docker sandbox child đã được verify.**
