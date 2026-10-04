# Kiến trúc Memory của Coding Agent

> Đối chiếu với code hiện tại ngày **2026-10-03**.
> MVP memory/context đã triển khai từ 2026-09-29. Tài liệu này phân biệt
> behavior đang chạy với các đề xuất chưa triển khai.
>
> Spec nền: [Memory / Context MVP](superpowers/specs/2026-09-29-memory-context-design.md).
> Bước tiếp theo: [Project Memory Review](superpowers/specs/2026-10-02-project-memory-review-design.md).
> Spec review **chưa được triển khai**: code vẫn hỗ trợ `/remember`, chưa có
> LLM đề xuất memory để user duyệt sau turn hoặc khi exit.

## 1. Mục tiêu và các lớp dữ liệu

Memory phục vụ việc tiếp tục session dài sau compaction, resume session đã lưu,
và dùng lại kiến thức bền vững giữa các session của cùng workspace.

| Lớp | Vai trò | Implementation |
|---|---|---|
| Session journal | Lưu message/lifecycle events, replay runtime state | `src/memory/event_store.py`, `src/runtime/reducer.py` |
| Compaction checkpoint | Bản bàn giao do model tạo cho một session | `src/context/checkpoint.py`, `src/context/compactor.py` |
| Project memory | Facts user chủ động lưu, dùng chung trong cùng workspace identity | `src/memory/manager.py` |
| Active context | Messages thực sự gửi cho model ở mỗi iteration | `src/agent/loop.py:Agent._build_context()` |
| Canonical instructions | Luật và yêu cầu hiện hành, có ưu tiên hơn project memory | System prompt trong `src/agent/loop.py` và user messages |

Ví dụ: tool result thuộc journal; “đã sửa auth, còn thiếu test” thuộc
checkpoint; quy ước chạy test có thể thuộc project memory. Trạng thái turn,
plan, approval và recovery lấy từ runtime replay, không từ summary.

```text
Journal --replay--> Runtime state
   |
   +--message_records()-----------------------+
                                             v
PROJECT.md + runtime + workspace ----> Context builder ----> Main model
                                             ^     |
                                             |     +--vượt budget--> Compactor
                                             |                          |
                                             +--- latest checkpoint <---+
```

Ba lớp journal, checkpoint và project memory không thay thế nhau.

## 2. Trạng thái triển khai

| Thành phần | Behavior hiện tại |
|---|---|
| Session history | JSONL append-only trong hoạt động bình thường; có version, seq, fsync và single-writer lock |
| Resume runtime | Replay toàn bộ journal; checkpoint không quyết định lifecycle |
| Compact trigger | Estimated input tokens vượt available input budget |
| Recent context | Raw suffix theo token budget, giữ nhóm assistant tool call và result liền sau |
| Checkpoint | Schema có cấu trúc, persist latest checkpoint theo session |
| Compaction lặp | Merge checkpoint trước với message prefix chưa được checkpoint bao phủ |
| Project isolation | `projects/<workspace_identity>/PROJECT.md` |
| Memory update | User dùng `/remember` và `/forget`; không có model tự append sau turn |
| Memory loading | Đọc toàn bộ `PROJECT.md` vào system prompt mỗi lần build context |
| LLM memory review | Mới có spec, chưa triển khai |
| Retrieval / embeddings / consolidation | Chưa triển khai |

Flow hiện tại đã thay ngưỡng compact cố định 70%, giữ 10 message và shared
memory `projects/default/PROJECT.md` của thiết kế cũ.

## 3. Lưu trữ và workspace identity

CLI dùng chung `--state-root` cho journal, checkpoint, project memory và sandbox:

```text
<state-root>/
├── chats/<session-id>.jsonl
├── checkpoints/<session-id>.json
├── projects/<workspace-identity>/PROJECT.md
└── sandboxes/<session-id>/
    ├── metadata.json
    └── ... sandbox control artifacts
```

Root mặc định là `$XDG_STATE_HOME/sang-coding-agent`, hoặc
`~/.local/state/sang-coding-agent` nếu không có `XDG_STATE_HOME`.
Docker Compose dùng `/state` trong named volume `coding-agent-state`.
Khởi tạo `Agent` trực tiếp mà không truyền `state_root` thì dùng thư mục cha
của `events_path`; đây là fallback API, không phải default CLI.

`workspace_identity()` trong `src/core/paths.py` tính SHA-256 từ resolved
source workspace path, `st_dev` và `st_ino`. Scope dựa trên source workspace,
không phải shadow workspace riêng của từng sandbox. Các session cùng identity
và state root dùng chung memory; identity khác dùng file khác.
`src/memory/private/PROJECT.md` trong repo không phải file được manager load
ở runtime.

Identity chưa phải project ID ổn định: move, clone, tạo lại thư mục, worktree
hoặc đổi mount có thể sinh identity khác. Journal chưa persist/validate stable
project ID để bind session độc lập với workspace khi resume.

Sandbox metadata đã lưu workspace identity. CLI resume stopped container kiểm
tra container ID, session/workspace labels, image, mounts và resource/security
settings. Guard này không thay thế session-project binding trong journal,
đặc biệt khi metadata thiếu hoặc dùng `Agent` trực tiếp.

## 4. Session journal

`EventStore` ghi provider messages và runtime events, chẳng hạn:

```text
UserMessageRecorded
TurnStarted
AssistantToolCallsRecorded
ToolExecutionRequested
ToolCompleted / ToolFailed
ToolMessageRecorded
AssistantMessageRecorded
TurnCompleted / TurnFailed / TurnInterrupted
ContextCompacted
```

Envelope có schema version, event ID, seq, session/runtime IDs, timestamp,
aggregate và payload. Reader kiểm tra schema, sequence, event IDs và session
consistency. Writer giữ exclusive non-blocking lock; append flush và fsync.

`message_records()` chỉ project message events thành `(seq, provider_message)`.
Runtime-only events không được đưa thẳng vào conversation. Checkpoint cutoff
dùng **event sequence**, không phải số thứ tự message.

Compaction không xóa hay viết lại journal. Reader hỗ trợ legacy role records;
typed schema không được hỗ trợ hoặc journal hỏng sẽ fail closed.
`repair_trailing_partial()` là primitive sửa tail JSONL dở với backup `.bak`,
chỉ giữ record hoàn chỉnh; CLI không tự gọi repair khi resume.

Journal là nguồn chuẩn cho **runtime state đã ghi nhận**, không chứng minh
workspace hiện tại vẫn giống lúc tool chạy. Tool lifecycle events lưu cả
`raw` và `compact` result; provider tool message dùng compact result kèm
execution/verification metadata khi phù hợp.

## 5. Active context và precedence

`Agent._build_context()` dựng provider messages theo thứ tự:

1. Một system message gồm canonical instructions, toàn bộ `PROJECT.md`,
   precedence statement, runtime projection và workspace projection.
2. Latest valid checkpoint, nếu có, dưới dạng system message riêng.
3. Message records có `seq > checkpoint.covers_through_seq`; current user
   message đã nằm trong các records này, không append thêm lần nữa.

Prompt label `PROJECT.md` là advisory, nói rõ instructions, code/config, tests,
journal và runtime state có ưu tiên hơn memory. Memory không quyết định
`allowed_actions` hoặc plan lifecycle.

Advisory labeling hiện là guard trong prompt: memory vẫn nằm trong
`role=system`, và `CompactionCheckpoint.to_message()` cũng dùng `role=system`.
Chưa có cơ chế tách generated memory/checkpoint khỏi instruction authority ở
tầng provider role; không coi trust boundary này là đã đảm bảo hoàn toàn.

Trước khi đếm token, projection thay string arguments lớn của completed
`write`/`edit` bằng marker chứa byte length và SHA-256. Ngưỡng là 1.000 UTF-8
bytes cho mỗi field `content`, `old_string`, `new_string`. Một số control
tool interactions đã được runtime biểu diễn cũng được bỏ theo policy của
`Agent._control_tool_call_ids_to_omit()`. Projection không sửa journal gốc.

## 6. Token budget và auto compaction

`ContextBudget` có defaults:

| Giá trị | Tokens | Ý nghĩa |
|---|---:|---|
| `context_window` | 256.000 trong cấu hình hiện tại | Window để tính budget |
| `reserved_output_tokens` | 16.000 | Dành chỗ cho output |
| `reserved_tool_tokens` | 16.000 | Dành chỗ cho tools/schema |
| `safety_margin_tokens` | 8.000 | Safety margin |
| `checkpoint_max_tokens` | 8.000 | Giới hạn rendered checkpoint, trừ khi chọn raw suffix |
| `recent_user_max_tokens` | 20.000 | Additional older raw user messages sau compact |

```text
available_input_tokens
= context_window - reserved_output_tokens - reserved_tool_tokens - safety_margin_tokens
= 256.000 - 16.000 - 16.000 - 8.000
= 216.000

compact khi estimated_input_tokens > available_input_tokens
```

Budget là token-aware nhưng window **chưa được tự tra theo model**:
`llm.get_context_window(model)` luôn trả `core.config.CONTEXT_WINDOW = 256000`.
Reserve có thể truyền bằng code, chưa có CLI flags riêng; cũng chưa được
enforce thành API output-token limit. Đổi model cần đối chiếu window thực tế.

`20k` không phải compact threshold. Nó giới hạn older user messages giữ raw
ngoài newest user message. Compactor chọn raw suffix liên tục sau khi trừ
system message và checkpoint reserve; newest user message cùng các nhóm sau nó
được giữ nguyên. Nếu không thể rebuild context vừa budget thì báo lỗi, không
âm thầm truncate user text.

Token count dùng `cl100k_base`, fallback `len(text) // 4`, tính content và
tool-call payloads. Đây là ước lượng, chưa tính chính xác provider framing,
image tokens hoặc tool schema; schema được dự phòng bằng reserved tool budget.

## 7. Compaction checkpoint

Mỗi session có một latest checkpoint với schema:

```json
{
  "schema_version": 1,
  "session_id": "session-a",
  "covers_through_seq": 128,
  "created_at": "2026-10-03T10:00:00Z",
  "goal": "Sửa authentication flow",
  "progress": ["Đã sửa verify_token()"],
  "decisions": ["Giữ validation trong middleware"],
  "constraints": ["Không thay đổi response schema"],
  "blockers": [],
  "remaining_work": ["Thêm integration test"],
  "critical_references": ["src/auth.py:verify_token"],
  "verification": ["Chưa chạy integration test"]
}
```

Validation kiểm tra exact fields, version, session ID, cutoff không âm/không
vượt last journal seq, `goal` là string và các narrative fields còn lại là
lists of strings. Đây là validation cấu trúc, chưa kiểm chứng ngữ nghĩa hay
provenance của `progress`, `constraints`, `verification`.

Khi input vượt budget:

1. Lấy projected message records chưa được checkpoint bao phủ.
2. Gom assistant tool call và các result tương ứng liền sau thành nhóm.
3. Chọn raw suffix theo allowance và older-user budget, giữ newest user cùng
   các nhóm sau nó; prefix trước suffix là phần compact.
4. Gọi model với previous checkpoint và prefix mới để sinh JSON narrative;
   cutoff là seq của message cuối prefix.
5. Validate schema, checkpoint allowance và rebuilt context budget.
6. Agent lưu checkpoint bằng temp file + fsync + atomic replace, ghi
   `ContextCompacted`, rồi dùng rebuilt context cho main model.

Lần compact sau chỉ đưa previous checkpoint và uncovered prefix mới vào summary.
Raw history đã covered không bị gửi lại toàn bộ. Narrative cũ vẫn được model
merge lại, nên summary drift qua nhiều lần compact vẫn có thể xảy ra.
Summary prompt render text và tên tool calls, không nhúng base64 ảnh hoặc đầy
đủ tool-call arguments.

Atomic grouping giữ các interaction groups liền nhau khi chọn cutoff; chưa
thay thế validation đầy đủ cho orphan call/result trong journal lỗi. Nhóm bắt
buộc quá lớn có thể làm context construction fail.

Failure behavior hiện tại:

- Checkpoint thiếu: build từ journal messages.
- Checkpoint malformed, sai session hoặc ngoài seq bounds: cảnh báo và bỏ qua
  cho context; chỉ thay file khi compact mới thành công.
- Summary API lỗi, JSON/schema sai, checkpoint quá lớn hoặc rebuilt context
  vượt budget: báo lỗi, giữ checkpoint cũ và journal history.
- Main model iteration gặp exception: runtime ghi `TurnFailed`, REPL hiển thị
  lỗi; chưa có fallback/chunking hoặc compact-and-retry riêng cho provider
  `context_length_exceeded`.

`_create_checkpoint()` đã đếm `summary_input_tokens` để đo lường nhưng **chưa
preflight summary request có vừa window trước khi gọi API**. Main context có
budget không đồng nghĩa summary request đã được bảo vệ.

## 8. Project memory và cách cập nhật hiện tại

Manager tạo template khi file chưa có, gồm Overview, Architecture, Important
Decisions, Hard Constraints, Coding Conventions, Known Problems, Failed
Approaches và Curated Memories.

REPL xử lý trực tiếp, không gọi model hoặc tạo agent turn:

```text
/memory
/remember <fact>
/forget <id-or-exact-fact>
```

`/remember` normalize whitespace, từ chối fact rỗng, deduplicate bằng exact
case-insensitive match và ghi vào `## Curated Memories`:

```markdown
## Curated Memories

- [a1b2c3d4] Test suite dùng Python unittest.
```

ID gồm 8 ký tự hex từ UUID, là handle trong file, không phải content hash.
`/forget` chỉ xóa đúng một ID hoặc exact fact match; không match hoặc ambiguous
thì báo lỗi và giữ file. Manager giữ các section ngoài vùng curated.
Mutation có lock và atomic replace; directory/file dùng modes 0700/0600.

Manager **chưa tự kiểm chứng fact với code/config/test trước khi lưu**. User
chủ động chọn nội dung qua command. Không có automatic extraction khi
`TurnCompleted`, trong compaction, khi `exit` hay `Agent.close()`.

Nên lưu kiến trúc đã xác nhận, lệnh test/build đã dùng thành công, quyết định
bền vững hoặc constraint dài hạn. Iteration hiện tại, tool vừa gọi, test fail
tạm thời và approval đang chờ thuộc journal/checkpoint/runtime.

Toàn bộ file được load, chưa có size limit hoặc retrieval. Memory cũ chưa tự
được đánh dấu stale; cần kiểm tra facts quan trọng với repository hiện tại.

## 9. Session mới và resume

Session mới: CLI chọn source workspace/state root, tạo journal path và sandbox;
Agent load/tạo memory đúng identity, load checkpoint theo session ID và replay
runtime. Sau khi ghi user message, agent build context trước mỗi iteration.
Session mới dùng chung project memory trong cùng scope, nhưng không tự load
checkpoint/history của session cũ.

Resume: CLI chọn journal theo ID, unique prefix hoặc `--last`, restore sandbox
nếu có metadata và reconcile khi resume stopped container. Agent mở/validate
journal, load checkpoint theo session ID, load memory theo current workspace
identity và replay runtime. REPL xử lý pending approval/recovery trước khi
tiếp tục; context gồm system projection, checkpoint và uncovered messages,
compact lại chỉ khi vượt budget.

Replay khôi phục lifecycle đã ghi nhận, không tự khôi phục file hoặc revalidate
test. Checkpoint chưa chứa Git HEAD/branch/dirty digest hay dependency
fingerprint để đánh dấu verification stale. Workspace projection và sandbox
reconciliation đã có, chưa phải checkpoint-to-current-workspace validation.

## 10. Bước tiếp theo: LLM đề xuất, user duyệt memory

Spec ngày 2026-10-02 đề xuất thay public `/remember` bằng flow:

```text
TurnCompleted hoặc graceful session exit
  -> LLM extract durable candidates từ completed event range chưa review
  -> validate schema/provenance và loại exact duplicates
  -> user duyệt batch: all / các số / none
  -> lưu chỉ facts đã duyệt qua MemoryManager.remember()
  -> ghi review cursor vào journal
```

Thiết kế giữ `/memory`, `/forget` và internal write primitive
`MemoryManager.remember()`. Candidate cần source event sequences; cursor và
failure events hỗ trợ retry. Extraction độc lập với compaction, không promote
active/failed/interrupted work; review failure không làm fail turn hoặc chặn
exit. Exit review dự kiến dành cho `exit`/`quit`, không cho EOF/top-level
interrupt.

Đây là **behavior dự kiến**. Chưa có extractor, review cursor/events hoặc batch
approval pipeline trong implementation. Khi triển khai mới cập nhật command
surface và policy ở mục 8.

## 11. Giới hạn và hardening

| Phạm vi | Guard đã có | Phần còn thiếu |
|---|---|---|
| Identity | Memory theo workspace hash; sandbox labels/mount reconciliation | Stable project ID, session binding ở journal, explicit migration |
| Trust | Advisory label và precedence statement | Tách generated memory/checkpoint khỏi instruction authority |
| Evidence | Tool status/result, runtime evidence checks | Narrative verification/constraints có provenance, giữ wording gốc |
| Budget | Main context estimate, checkpoint cap, raw suffix budget | Model-specific window, summary preflight, bounded fallback/chunking |
| Reality drift | Runtime/workspace projection, sandbox reconciliation | Checkpoint fingerprint và revalidation khi code/environment đổi |
| Large output | Compact tool messages, projection completed write/edit | Offload raw result khỏi journal, xử lý oversized atomic groups |
| Secret persistence | Redact structured secret keys, kể cả JSON tool arguments | Redact secrets trong stdout/free-form content, review trace persistence |
| Replay/storage | Schema/seq validation, fsync, locks, explicit tail repair | Incremental replay/snapshots khi benchmark cho thấy cần |
| Memory quality | User-curated entries, exact dedup/forget, atomic mutation | Fact validation, stale/supersession policy, size cap |

Ưu tiên session-project binding, trust/evidence và summary budget trước khi thêm
retrieval phức tạp. Các giới hạn hiện tại chưa đòi hỏi vector database, memory
phân tán hoặc tự sinh skills.

## 12. Kiểm thử và đánh giá TLCN

Project dùng `unittest`. Nhóm test liên quan:

| File | Nội dung |
|---|---|
| `tests/test_event_store.py` | Envelope/seq/schema, locks, redaction, tail repair, message projection |
| `tests/test_checkpoint.py` | Schema/session/cutoff validation, deterministic render, atomic persistence |
| `tests/test_compactor.py` | Budget, newest user retention, atomic groups, incremental compaction, errors |
| `tests/test_memory_manager.py` | Workspace isolation, dedup, exact forget, concurrent mutation |
| `tests/test_agent_loop.py` | Context/resume, advisory prompt, checkpoint persistence và metrics |
| `tests/test_main.py` | REPL memory commands ngoài model turn |

Chạy từ repo root sau khi cài dependencies và cấu hình `API_KEY`, `MODEL`,
`BASE_URL` theo [run guide](run_guide.md):

```bash
PYTHONPATH=src python -m unittest \
  tests.test_event_store tests.test_checkpoint tests.test_compactor \
  tests.test_memory_manager tests.test_agent_loop tests.test_main
```

`ContextCompacted` lưu cutoff trước/sau, estimated input tokens trước/sau,
summary-input tokens và duration milliseconds. Reducer coi event này là runtime
no-op. Có thể dùng để đo token reduction, compaction frequency, summary cost và
latency; input metrics này không phải usage thực tế do provider xác nhận.

Các phép đánh giá tiếp theo nên gồm continuity qua nhiều lần compact/resume,
constraint drift, verification theo tool evidence, stale memory, resume sai
workspace và summary prefix lớn hơn window. Phân biệt guard đã test với
fault-injection scenario cho invariant chưa triển khai; schema hoặc advisory
label chưa chứng minh semantic correctness hay isolation hoàn chỉnh.
