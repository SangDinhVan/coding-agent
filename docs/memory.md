# Kiến trúc Memory đề xuất cho Coding Agent

> Trạng thái: **MVP đã triển khai ngày 2026-09-29**.
> Tài liệu giữ nguyên phần lập luận và lộ trình ban đầu làm lịch sử thiết kế;
> behavior hiện hành được chốt tại
> [`docs/superpowers/specs/2026-09-29-memory-context-design.md`](superpowers/specs/2026-09-29-memory-context-design.md).
>
> Tài liệu này mô tả kiến trúc memory phù hợp với phạm vi TLCN của project.
> Thiết kế lấy cảm hứng từ cách tách context, compaction và long-term memory
> của Codex, nhưng chỉ giữ những phần cần thiết để có thể triển khai, kiểm thử
> và sử dụng thực tế.
>
> Cập nhật hardening ngày **2026-10-02**: các phần cuối tài liệu ghi rõ những
> guard implementation hiện đã có, các failure mode còn lại và invariant cần
> bổ sung. Nội dung đề xuất không đồng nghĩa code hiện tại đã triển khai toàn bộ.

---

# 1. Mục tiêu

Kiến trúc mới cần giải quyết bốn vấn đề:

1. Một session dài vẫn tiếp tục được khi context gần đầy.
2. Có thể đóng chương trình rồi resume đúng session cũ.
3. Các session của cùng một project có thể dùng chung kiến thức bền vững.
4. Project khác không đọc nhầm memory của nhau.

Ngoài bốn mục tiêu chức năng trên, hệ thống phải giữ bốn ranh giới an toàn:

```text
Identity boundary
= session và memory phải thuộc đúng project

Trust boundary
= checkpoint và project memory là dữ liệu tham khảo, không phải instruction

Resource boundary
= mọi request, kể cả request dùng để compact, đều phải có token budget hữu hạn

Reality boundary
= state đã lưu phải được reconcile với code và môi trường hiện tại
```

Thiết kế không cố lưu mọi thứ vào một file. Mỗi loại dữ liệu có vòng đời và
độ tin cậy khác nhau nên được tách riêng.

---

# 2. Mental model ngắn nhất

```text
Session Journal
= điều gì đã thực sự xảy ra trong một session

Compaction Checkpoint
= agent đang làm đến đâu và cần gì để tiếp tục session đó

Project Memory
= kiến thức bền vững có thể dùng lại qua nhiều session của cùng project

Canonical Instructions
= luật do user/developer viết, có độ ưu tiên cao hơn memory do model tạo
```

Ví dụ:

```text
"Tool read_file vừa đọc src/auth.py"
→ Session Journal

"Đã sửa verify_token(), còn thiếu integration test"
→ Compaction Checkpoint

"Repo chạy test bằng pytest"
→ Project Memory

"Không được thay đổi public API"
→ Canonical Instructions
```

---

# 3. Sơ đồ tổng quan

```text
                              CODING AGENT MEMORY

┌─────────────────────────────────────────────────────────────────────┐
│                    CANONICAL INSTRUCTIONS                           │
│                                                                     │
│  system prompt / project instructions / user constraints            │
│  Đây là luật gốc, không được model tự ý sửa bằng memory.             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         ACTIVE CONTEXT                              │
│                    phần model nhìn thấy hiện tại                    │
│                                                                     │
│  canonical instructions                                             │
│  + project memory liên quan                                         │
│  + latest compaction checkpoint                                     │
│  + recent user messages                                             │
│  + recent assistant/tool interactions                               │
│  + current runtime/workspace state                                  │
└───────────────┬───────────────────────────────┬─────────────────────┘
                │                               │
        model tiếp tục làm việc          context gần đầy
                │                               │
                ▼                               ▼
┌───────────────────────────┐       ┌───────────────────────────────┐
│      SESSION JOURNAL      │       │          COMPACTION           │
│                           │       │                               │
│ append-only JSONL         │       │ tạo structured handoff        │
│ lưu toàn bộ event gốc     │       │ cho chính session hiện tại    │
└─────────────┬─────────────┘       └──────────────┬────────────────┘
              │                                    │
              │                                    ▼
              │                     ┌───────────────────────────────┐
              │                     │    COMPACTION CHECKPOINT      │
              │                     │                               │
              │                     │ goal                          │
              │                     │ progress                      │
              │                     │ decisions / constraints       │
              │                     │ blockers / remaining work     │
              │                     │ references / verification     │
              │                     │ covers_through_seq             │
              │                     └──────────────┬────────────────┘
              │                                    │
              └──────────────────┬─────────────────┘
                                 │
                                 ▼
                         resume cùng session


┌─────────────────────────────────────────────────────────────────────┐
│                         PROJECT MEMORY                              │
│                                                                     │
│  projects/<workspace_identity>/PROJECT.md                           │
│                                                                     │
│  Chỉ chứa kiến thức bền vững, dùng chung cho các session            │
│  thuộc cùng một project.                                            │
└─────────────────────────────────────────────────────────────────────┘
```

Điểm quan trọng:

```text
Session Journal != Compaction Checkpoint != Project Memory
```

Ba lớp có liên quan nhưng không thay thế nhau.

---

# 4. Cấu trúc lưu trữ đề xuất

```text
<state-root>/
├── chats/
│   ├── <session-a>.jsonl
│   ├── <session-b>.jsonl
│   └── <session-c>.jsonl
│
├── checkpoints/
│   ├── <session-a>.json
│   ├── <session-b>.json
│   └── <session-c>.json
│
├── projects/
│   ├── <workspace-identity-a>/
│   │   └── PROJECT.md
│   └── <workspace-identity-b>/
│       └── PROJECT.md
│
└── sandboxes/
    ├── <session-a>/
    └── <session-b>/
```

Quan hệ giữa project và session:

```text
Project A
├── PROJECT.md
├── Session A1 journal + checkpoint
├── Session A2 journal + checkpoint
└── Session A3 journal + checkpoint

Project B
├── PROJECT.md khác
└── Session B1 journal + checkpoint
```

`PROJECT.md` được dùng qua nhiều session, nhưng chỉ trong cùng project.

---

# 5. Session Journal

Session journal là lịch sử đầy đủ của một phiên làm việc.

Nó có thể chứa:

- user message;
- assistant message;
- tool call;
- tool result;
- lifecycle event;
- plan event;
- approval và recovery event;
- kết quả kiểm thử;
- lỗi tạm thời trong quá trình làm việc.

Ví dụ:

```text
UserMessageRecorded
TurnStarted
AssistantToolCallsRecorded
ToolExecutionRequested
ToolCompleted
ToolMessageRecorded
AssistantMessageRecorded
TurnCompleted
```

Journal là **canonical source of truth** của session.

Quy tắc:

```text
- append-only;
- không xóa history cũ khi compact;
- có thể replay để dựng runtime state;
- checkpoint sai vẫn có thể tạo lại từ journal;
- không phụ thuộc PROJECT.md để xác định lifecycle chính xác.
```

---

# 6. Active Context

Active context là dữ liệu thực sự được gửi cho model ở một request.

Nó không cần chứa toàn bộ journal.

```text
Full journal
    │
    │ project thành model-visible view
    ▼
Active context
```

Active context đề xuất:

```text
1. Canonical instructions
2. Project memory của đúng workspace
3. Current runtime state
4. Latest compaction checkpoint, nếu có
5. Recent raw user messages
6. Recent atomic assistant/tool interactions
7. Current user message
```

Danh sách trên mô tả thứ tự dữ liệu, không có nghĩa tất cả thành phần nên được
gửi bằng cùng một provider role. Canonical instructions mới thuộc instruction
channel có độ ưu tiên cao. Project memory và checkpoint phải được render như
**untrusted/advisory data**, có delimiter rõ ràng và không được nâng thành luật
chỉ vì chúng xuất hiện trong một system message.

Invariant cần giữ:

```text
canonical instructions có thể điều khiển cách dùng memory
memory không thể tự tạo instruction để ghi đè canonical instructions
```

Các assistant tool call và tool result tương ứng phải được giữ thành nhóm
nguyên tử:

```text
assistant tool call
+ tool result 1
+ tool result 2
```

Không được giữ tool call nhưng làm rơi tool result, hoặc ngược lại.

---

# 7. Khi nào auto compact?

Không compact theo số message cố định và không dùng `20k` làm ngưỡng compact.

Ngưỡng nên dựa trên context budget còn lại:

```text
available_input_budget
= context_window
- reserved_output_tokens
- reserved_tool_tokens
- safety_margin_tokens
```

Kích hoạt auto compact khi:

```text
estimated_input_tokens > available_input_budget
```

Budget này phải áp dụng cho cả request chính và request tạo checkpoint. Không
được giả định rằng request compact luôn nhỏ hơn request đang bị overflow:

```text
summary_prompt_tokens
+ reserved_summary_output_tokens
+ safety_margin_tokens
<= compaction_model_context_window
```

Nếu prefix cần compact không vừa một request, hệ thống phải compact theo chunk
hoặc dùng một fallback xác định trước; không gửi toàn bộ prefix rồi chờ provider
trả lỗi context overflow.

Ví dụ với context window 256k:

```text
context window          256k
reserved output          16k
reserved tool calls      16k
safety margin             8k
--------------------------------
available input budget   216k
```

Trong ví dụ này, hệ thống compact khi input tiến gần `216k`, không phải khi
đạt một số lượng message cụ thể.

Các reserve nên là configuration vì mỗi model và workload có nhu cầu khác
nhau.

---

# 8. Vai trò của budget 20k token

`20k` không phải threshold bắt đầu compact.

Nó là giới hạn tối đa dành cho recent raw user messages được giữ lại sau
compaction:

```text
RECENT_USER_MESSAGE_MAX_TOKENS = 20_000
```

Mục đích là giữ nguyên wording gần đây của user, vì summary có thể làm mất
nuance.

Ví dụ user nói:

```text
Không đổi API /login.
Riêng /refresh thì có thể thay đổi.
```

Summary không tốt có thể biến thành:

```text
Không thay đổi auth API.
```

Raw user message giúp model kiểm tra lại yêu cầu gốc.

Tuy nhiên, project không nên áp dụng `20k` một cách mù quáng cho mọi model:

```text
recent_user_budget = min(20_000, safe_budget_for_current_model)
```

Ngoài raw user messages, hệ thống vẫn phải giữ recent tool interactions còn
phù hợp với input budget.

---

# 9. Compaction Checkpoint

Compaction checkpoint là bản bàn giao có cấu trúc để agent tiếp tục cùng một
session mà không cần gửi toàn bộ history cho model.

Schema khái niệm:

```json
{
  "session_id": "session-a",
  "covers_through_seq": 128,
  "created_at": "2026-09-29T10:00:00Z",
  "goal": "Sửa authentication flow",
  "progress": [
    "Đã đọc src/auth.py",
    "Đã sửa verify_token()"
  ],
  "decisions": [
    "Giữ JWT validation trong middleware"
  ],
  "constraints": [
    "Không thay đổi response schema"
  ],
  "blockers": [],
  "remaining_work": [
    "Thêm integration test cho expired token"
  ],
  "critical_references": [
    "src/auth.py:verify_token",
    "tests/test_auth.py"
  ],
  "verification": [
    "Unit tests đã pass",
    "Integration tests chưa chạy"
  ]
}
```

`covers_through_seq` cho biết checkpoint đã tóm tắt journal đến event nào.

Context mới được dựng bằng:

```text
latest checkpoint
+ journal events có seq > covers_through_seq
```

Nhờ đó, hệ thống không phải summary lại toàn bộ history ở mỗi request.

Checkpoint không thay thế journal. Nếu checkpoint bị lỗi hoặc mất, nó có thể
được tạo lại từ journal.

Checkpoint cũng không phải bằng chứng rằng một hành động đã thực sự xảy ra.
Trong MVP hiện tại, các field narrative vẫn do model sinh và schema validation
chỉ kiểm tra kiểu dữ liệu. Hướng hardening là tách:

```text
Narrative do model tổng hợp
= goal / progress / decisions / remaining_work

Evidence do runtime suy ra từ journal
= tool execution status / command / exit code / event seq / workspace fingerprint
```

Đặc biệt, `verification` không nên chỉ là câu model tự viết như "tests đã pass".
Mỗi claim quan trọng cần trỏ tới event hoặc tool result thật. Constraint quan
trọng nên giữ nguyên wording của user cùng source sequence để giảm telephone
game qua nhiều lần compact.

---

# 10. Project Memory

Project memory chứa kiến thức có khả năng còn hữu ích trong các session tương
lai của cùng project.

Đường dẫn đề xuất:

```text
<state-root>/projects/<workspace_identity>/PROJECT.md
```

Project hiện đã có `workspace_identity`, vì vậy có thể dùng identity này để
tách memory thay cho đường dẫn chung `projects/default/PROJECT.md`.

Trong phiên bản hiện tại, identity được tạo từ đường dẫn thật, device và inode
của workspace. Cách này cung cấp isolation cơ bản nhưng không phải project ID
ổn định:

```text
- move, clone hoặc tạo lại repo có thể sinh identity mới và mất liên kết memory;
- Git worktree có thể làm memory bị phân mảnh;
- bind mount, network filesystem hoặc Docker mount có thể đổi device/inode;
- tái sử dụng cùng path và inode có rủi ro nhận nhầm identity cũ;
- một monorepo có thể cần scope nhỏ hơn toàn workspace.
```

Quan trọng hơn, session journal hiện cũng phải được bind với project identity.
Chỉ tách đường dẫn `PROJECT.md` là chưa đủ: resume một journal của project A
trong workspace B sẽ trộn history A với code và memory B.

Hướng hardening tối thiểu:

```text
1. Tạo project_id ổn định cho local repository.
2. Persist project_id trong event khởi tạo session.
3. Khi resume, so session.project_id với current.project_id.
4. Mismatch thì fail closed; migrate/adopt phải là thao tác explicit.
```

Với Git, project ID có thể nằm trong Git common directory để move repo vẫn giữ
ID và các worktree dùng chung ID, nhưng clone mới không tự động nhận chung
memory. Hash remote URL hoặc file ID được commit đều có ambiguity, vì vậy không
nên xem chúng là lời giải duy nhất cho mọi workflow.

## 10.1 Nội dung nên lưu

```markdown
# Overview

- Đây là coding agent chạy tool trong Docker sandbox.

# Architecture

- Runtime state được replay từ append-only event journal.
- Tool call được thực thi thông qua ToolExecutor.

# Verified Commands

- Chạy test bằng `pytest`.

# Important Decisions

- Dùng OpenAI Python SDK trực tiếp.
- Session journal là canonical source of truth.

# Hard Constraints

- Không ghi secrets vào journal.
- Không tách assistant tool call khỏi tool result khi compact.

# Known Problems

- Compaction checkpoint chưa được persist.
```

## 10.2 Nội dung không nên lưu

```text
- Agent đang ở iteration 4.
- Tool vừa đọc src/auth.py.
- Test hiện đang fail trong lúc sửa.
- User vừa hỏi một câu tạm thời.
- Đang chờ approval của một tool call.
```

Các thông tin trên thuộc session journal hoặc checkpoint.

## 10.3 Câu hỏi phân loại

```text
Thông tin này có còn hữu ích trong một session mới vào tháng sau không?

YES → có thể là project memory
NO  → giữ trong session journal/checkpoint
```

---

# 11. Cách cập nhật Project Memory

Phiên bản đầu không nên để model tự append memory sau mọi turn.

Flow an toàn hơn:

```text
recent completed work
        │
        ▼
extract candidate memories
        │
        ▼
deduplicate + kiểm tra với code/config/test
        │
        ▼
user hoặc policy xác nhận
        │
        ▼
update PROJECT.md
```

Có thể cung cấp ba command đơn giản:

```text
/memory
/remember <fact>
/forget <fact hoặc memory id>
```

Một lựa chọn khác là đề xuất memory khi kết thúc session:

```text
Proposed project memories:

1. Repository dùng pytest.
2. Không thay đổi public auth API.
3. Tool-call groups phải được compact nguyên tử.

Save these memories? [y/N]
```

Việc model tự extract memory có thể được thêm sau, nhưng bước ghi chính thức
cần có validation và cơ chế thay thế thông tin cũ.

---

# 12. Canonical Instructions và Memory

Không nên dùng cùng một file cho cả luật bắt buộc và facts do model học được.

```text
Canonical instructions
= do user/developer quản lý
= authoritative

PROJECT.md
= facts do agent hoặc user ghi nhận
= advisory, có thể cũ hoặc sai
```

Thứ tự ưu tiên đề xuất:

```text
System và safety instructions
        ↓
Canonical project instructions và hard constraints
        ↓
Current user instruction trong phạm vi các luật trên
        ↓
Current code, configuration, tests và Git
        ↓
Session journal và runtime state
        ↓
Compaction checkpoint
        ↓
Project memory
```

Thứ tự này phải được phản ánh bằng cấu trúc request và validation trong code,
không chỉ là một câu nhắc trong prompt. Nếu checkpoint hoặc `PROJECT.md` được
nội suy trực tiếp thành provider `system` content thì chúng có quyền thực tế
cao hơn mức "advisory" mà tài liệu mong muốn, đồng thời mở đường cho stale
memory hoặc prompt injection trở thành instruction có độ ưu tiên cao.

Ví dụ `PROJECT.md` nói repo dùng `npm`, nhưng repository hiện có
`pnpm-lock.yaml` và CI chạy `pnpm`, agent phải tin trạng thái repository hiện
tại.

---

# 13. Flow khi bắt đầu session mới

```text
User mở project
      │
      ▼
xác định workspace_identity
      │
      ▼
load canonical instructions
      │
      ▼
load projects/<workspace_identity>/PROJECT.md
      │
      ▼
tạo session journal mới
      │
      ▼
build active context
      │
      ▼
model bắt đầu làm việc
```

Session mới không tự động nhận checkpoint của session cũ. Nếu user muốn tiếp
tục đúng công việc cũ, họ nên resume session đó hoặc chủ động dùng handoff đã
được lưu.

---

# 14. Flow khi resume session

```text
User chọn session cũ
      │
      ▼
load session journal
      │
      ▼
validate session.project_id == current.project_id
      │
      ├── replay runtime state
      │
      └── load latest checkpoint
                 │
                 ▼
      lấy events sau covers_through_seq
                 │
                 ▼
      so workspace fingerprint hiện tại
      với fingerprint lúc checkpoint
                 │
                 ▼
      load đúng project memory
                 │
                 ▼
      rebuild active context
                 │
                 ▼
      model tiếp tục session
```

Resume không phụ thuộc hoàn toàn vào checkpoint. Runtime lifecycle quan trọng
vẫn được replay từ event journal.

Replay chỉ khôi phục state đã ghi nhận, không tự khôi phục thế giới bên ngoài.
Khi resume cần reconcile ít nhất các giá trị có sẵn hoặc lấy được rẻ:

```text
project_id
git HEAD / branch / dirty-state digest
sandbox hoặc container identity
image/dependency fingerprint nếu có
```

Nếu fingerprint lệch, các claim như "đã sửa file X" hoặc "tests đã pass" phải
được đánh dấu stale và revalidate trước khi dùng làm bằng chứng.

---

# 15. Flow khi context gần đầy

```text
build context
      │
      ▼
estimate token usage
      │
      ▼
vượt available_input_budget?
      │
   ┌──┴──┐
   │     │
  NO    YES
   │     │
   │     ▼
   │  chọn history cần compact
   │     │
   │     ▼
   │  tạo structured checkpoint
   │     │
   │     ▼
   │  persist checkpoint
   │     │
   │     ▼
   │  giữ recent user messages <= 20k
   │  và recent atomic tool interactions
   │     │
   └─────┴──────────► rebuild context ► model
```

Nếu tạo checkpoint thất bại, journal vẫn không bị thay đổi. Hệ thống có thể
retry hoặc giảm recent context theo một fallback policy rõ ràng. Policy tối
thiểu cần xác định trước, tránh compact lỗi ở mọi request:

```text
1. Không retry vô hạn cùng một cutoff.
2. Ghi nhận compaction failure và nguyên nhân.
3. Offload/truncate low-value tool output trước.
4. Giữ current user message và atomic tool group đang hoạt động.
5. Nếu vẫn không vừa, fail rõ ràng thay vì gửi provider request chắc chắn lỗi.
6. Khi provider trả context_length_exceeded, compact và retry tối đa một lần.
```

---

# 16. Những phần chưa làm trong phiên bản đầu

Kiến trúc MVP chủ động chưa bao gồm:

```text
- automatic memory extraction sau mọi turn;
- hai-phase extraction và consolidation;
- memory_summary.md;
- vector database hoặc embedding retrieval;
- tự động sinh skills từ rollout;
- load memory từ project khác;
- hệ thống memory phân tán nhiều máy.
```

Lý do:

- chưa có đủ memory để cần search phức tạp;
- khó kiểm soát stale hoặc conflicting facts;
- tăng số lần gọi model, latency và chi phí;
- làm khó xác định nguyên nhân khi agent quên hoặc nhớ sai;
- vượt quá phạm vi cần thiết để chứng minh giá trị trong TLCN.

Các phần này chỉ nên được thêm khi MVP đã có số liệu cho thấy nhu cầu thực tế.

---

# 17. Phạm vi triển khai đề xuất

## Phase 1 - Project isolation

```text
- thay projects/default bằng projects/<workspace_identity>;
- mỗi project có PROJECT.md riêng;
- các session cùng project dùng chung file này;
- project khác không thể đọc nhầm memory.
```

## Phase 2 - Persistent compaction

```text
- thay KEEP_RECENT_MESSAGES bằng token budget;
- auto compact dựa trên available input budget;
- tạo structured checkpoint;
- persist checkpoint theo session;
- chỉ compact events chưa được checkpoint bao phủ.
```

## Phase 3 - Curated project memory

```text
- thêm /memory, /remember và /forget;
- giới hạn PROJECT.md ở durable facts;
- kiểm tra memory với trạng thái repository hiện tại;
- không auto-append sau mọi turn.
```

## Phase 4 - Optional research

```text
- memory candidate extraction;
- deduplication và supersession;
- relevance retrieval;
- consolidation qua nhiều session.
```

---

# 18. Tiêu chí đánh giá cho TLCN

Kiến trúc memory cần được đánh giá bằng số liệu, không chỉ bằng cảm giác.

## 18.1 Session continuity

```text
Sau compact hoặc resume, agent có nhớ đúng:
- goal hiện tại;
- việc đã hoàn thành;
- constraints;
- việc còn lại;
- kết quả test gần nhất hay không?
```

## 18.2 Token reduction

```text
So sánh token input:
- gửi toàn bộ history;
- checkpoint + recent interactions.
```

## 18.3 Compaction cost

```text
- số lần gọi model để compact;
- latency của mỗi lần compact;
- có summary lặp lại cùng history hay không.
```

## 18.4 Memory correctness

```text
- stale-memory rate;
- conflicting-memory rate;
- project cross-contamination rate;
- số memory được code/test xác nhận.
```

## 18.5 Recovery

```text
- checkpoint mất có rebuild từ journal được không;
- process crash có resume đúng runtime state không;
- tool call và tool result có còn nguyên nhóm không.
```

## 18.6 Fault-injection scenarios

Phần lớn tiêu chí không cần đánh giá cảm tính. Có thể tạo scenario xác định
trước và assert invariant:

```text
- compact lặp 3-5 lần rồi kiểm tra constraint gốc còn nguyên;
- resume session trong sai workspace phải bị từ chối;
- sửa Git HEAD hoặc file sau checkpoint phải làm verification thành stale;
- compaction prefix lớn hơn model window phải được chia chunk hoặc fail rõ;
- tool result chứa secret và output rất lớn không được ghi raw vào journal;
- crash tạo JSONL tail dở phải repair được mà không mất record hoàn chỉnh;
- thiếu một tool result không được tạo provider message sequence mồ côi;
- schema cũ phải migrate hoặc fail closed với thông báo rõ ràng;
- hai writer cùng session phải không thể cùng ghi;
- memory chứa prompt injection không được trở thành instruction authority.
```

Các metric như stale-memory rate hoặc conflicting-memory rate phải định nghĩa
rõ mẫu số, nguồn ground truth và ai gán nhãn. Human review chỉ nên dành cho
semantic quality; các invariant về identity, evidence, atomicity và overflow
nên được kiểm thử tự động.

---

# 19. So sánh kiến trúc hiện tại và kiến trúc đề xuất

| Thành phần | Hiện tại | Đề xuất |
|---|---|---|
| Session history | JSONL journal | Giữ nguyên làm source of truth |
| Resume | Đọc lại session cũ | Journal + checkpoint + recent events |
| Compact trigger | Ngưỡng cố định 70% | Dynamic available input budget |
| Recent context | 10 message | Token budget + atomic interaction groups |
| Compaction summary | Tạo lại khi cần | Persist theo session |
| Project memory | `projects/default/PROJECT.md` | `projects/<workspace_identity>/PROJECT.md` |
| Memory update | Model append sau mỗi turn | Curated/user-approved trong MVP |
| Memory loading | Load toàn bộ file | Load đúng project; retrieval để sau |
| Generated skills | Không có | Chưa cần trong MVP |

---

# 20. Hardening status và failure modes

## 20.1 Guard implementation hiện đã có

Các cơ chế sau đã tồn tại trong implementation và không nên tiếp tục mô tả như
phần hoàn toàn chưa xử lý:

```text
- journal có schema_version, sequence validation, fsync và single-writer lock;
- trailing partial JSONL có repair primitive và tạo backup;
- checkpoint validate session/sequence và persist bằng temp file + replace;
- PROJECT.md mutation có lock và atomic replace;
- curated memory có stable memory ID và exact-match forget;
- một số tool tạo compact preview trước khi đưa output vào model context;
- runtime có recovery flow cho tool execution dang dở.
```

Các primitive này giảm rủi ro storage-level nhưng chưa tự động đảm bảo toàn bộ
invariant end-to-end.

## 20.2 Failure modes còn lại

| Failure domain | Rủi ro chính | Hardening tối thiểu |
|---|---|---|
| Project identity | Move/clone/worktree làm mất hoặc phân mảnh memory | Stable project ID + explicit migration |
| Session binding | Resume journal của project khác | Persist và validate session project ID |
| Trust/precedence | Advisory data được gửi như system instruction | Tách instruction khỏi untrusted context data |
| Summary drift | Checkpoint cũ bị summary lại nhiều lần | Giữ verbatim constraints + provenance |
| Verification | Model hallucinate tests đã pass | Derive evidence từ runtime events |
| World drift | Code/environment đổi sau checkpoint | Workspace fingerprint + revalidation |
| Compaction overflow | Summary request tự vượt context | Budget summary call + chunking |
| Compaction failure | API/schema lỗi lặp lại mỗi request | Bounded retry + deterministic fallback |
| Large tool output | Atomic group không vừa hoặc journal phình | Preview + content-addressed blob/offload |
| Secret persistence | Secret nằm trong stdout/free-form content | Redact trước persistence, không chỉ theo key |
| Replay scaling | Full journal được đọc/replay nhiều lần | Incremental reducer hoặc periodic runtime snapshot |
| Schema evolution | Session cũ không đọc được sau đổi schema | Versioned reader/migration hoặc fail-closed policy |
| Project memory growth | Load toàn bộ file gây nhiễu và tốn context | Size limit trước khi thêm retrieval |

## 20.3 Hardening roadmap tối thiểu

Không cần thêm vector database hoặc hệ distributed memory để xử lý các lỗi
trên. Thứ tự ưu tiên ngắn nhất:

```text
1. Bind session với stable project ID.
2. Tách advisory memory/checkpoint khỏi instruction authority.
3. Ground verification và tool status bằng journal evidence.
4. Budget/chunk compaction request và định nghĩa fallback hữu hạn.
5. Reconcile workspace fingerprint khi resume.
6. Offload large raw tool output và redact trước khi persist.
7. Tối ưu replay khi benchmark cho thấy journal dài là bottleneck.
```

---

# 21. Câu chốt

```text
Journal giữ sự thật.

Checkpoint giữ khả năng tiếp tục một session dài.

PROJECT.md giữ kiến thức bền vững của đúng một project.

Canonical instructions giữ luật mà memory không được phép ghi đè.
```

Muốn bốn câu trên đúng trong implementation, hệ thống còn phải bảo vệ:

```text
Identity boundary: đang nhớ đúng project nào?
Trust boundary: dữ liệu nào là authority, dữ liệu nào chỉ là advisory?
Resource boundary: mọi request có chắc nằm trong budget không?
Reality boundary: state đã lưu còn khớp code và môi trường hiện tại không?
```

Phiên bản đầu nên ưu tiên:

```text
project isolation
+ persisted compaction checkpoint
+ token-aware context building
+ curated project memory
```

Đây là phạm vi đủ để agent dùng thực tế, có thể kiểm thử và có số liệu rõ ràng
cho báo cáo TLCN mà chưa cần xây toàn bộ hệ long-term memory phức tạp.
