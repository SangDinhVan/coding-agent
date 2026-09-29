# Codex Memory & Context Architecture

> Tài liệu này tổng hợp concept kiến trúc **context, compaction và long-term memory** của Codex theo hướng dễ hiểu để tham khảo khi thiết kế coding agent.
>
> Mục tiêu ở đây là mô tả **concept sát với kiến trúc Codex**, chưa thêm các cải tiến riêng cho agent khác.

---

# 1. Tổng quan kiến trúc

```text
                         CODEX MEMORY / CONTEXT ARCHITECTURE

┌───────────────────────────────────────────────────────────────────────┐
│                       FULL SESSION / ROLLOUT                         │
│                                                                       │
│  user msg → assistant → tool call → tool result → file edit → ...    │
│                                                                       │
│  Đây là lịch sử đầy đủ của session                                   │
└───────────────────────────────┬───────────────────────────────────────┘
                                │
                                │
                                ├─────────────────────────────────────┐
                                │                                     │
                                ▼                                     ▼
                 ┌─────────────────────────┐             ┌────────────────────────┐
                 │    ACTIVE CONTEXT       │             │    LONG-TERM MEMORY    │
                 │ model đang nhìn thấy    │             │    qua nhiều session   │
                 └────────────┬────────────┘             └────────────┬───────────┘
                              │                                       │
                              │                                       │
                    context còn đủ token?                             │
                              │                                       │
                       ┌──────┴──────┐                                │
                       │             │                                │
                      YES            NO                               │
                       │             │                                │
                       │             ▼                                │
                       │    ┌──────────────────────┐                   │
                       │    │      COMPACTION      │                   │
                       │    └──────────┬───────────┘                   │
                       │               │                               │
                       │               │ tạo HANDOFF SUMMARY           │
                       │               │                               │
                       │               ▼                               │
                       │    ┌──────────────────────────────┐           │
                       │    │ CompactionSummary            │           │
                       │    │                              │           │
                       │    │ - progress hiện tại          │           │
                       │    │ - decisions                  │           │
                       │    │ - constraints                │           │
                       │    │ - remaining work             │           │
                       │    │ - critical references        │           │
                       │    └──────────┬───────────────────┘           │
                       │               │                               │
                       │               ▼                               │
                       │    ┌──────────────────────────────┐           │
                       │    │ Rebuild model-visible history│           │
                       │    │                              │           │
                       │    │ 1. Initial context           │           │
                       │    │ 2. Recent user messages      │           │
                       │    │    <= 20k token budget       │           │
                       │    │ 3. CompactionSummary         │           │
                       │    └──────────┬───────────────────┘           │
                       │               │                               │
                       └───────────────┴──────────────┐                │
                                                    ▼                │
                                      ┌─────────────────────────┐     │
                                      │      MODEL CONTINUES    │     │
                                      │      CURRENT TASK       │     │
                                      └─────────────────────────┘     │
                                                                     │
                                                                     ▼
                                                   ┌───────────────────────────┐
                                                   │ PHASE 1: ROLLOUT MEMORY  │
                                                   │ EXTRACTION                │
                                                   └────────────┬──────────────┘
                                                                │
                                                                │ đọc rollout/session
                                                                ▼
                                                   ┌───────────────────────────┐
                                                   │ extract reusable info      │
                                                   │                           │
                                                   │ - raw_memory              │
                                                   │ - rollout_summary         │
                                                   │ - useful knowledge        │
                                                   └────────────┬──────────────┘
                                                                │
                                                                ▼
                                                   ┌───────────────────────────┐
                                                   │ PHASE 2: CONSOLIDATION   │
                                                   └────────────┬──────────────┘
                                                                │
                                              ┌─────────────────┼────────────────┐
                                              │                 │                │
                                              ▼                 ▼                ▼
                                  ┌────────────────────┐ ┌───────────────┐ ┌──────────────┐
                                  │ memory_summary.md  │ │  MEMORY.md    │ │   skills/    │
                                  │                    │ │               │ │              │
                                  │ small + dense      │ │ detailed      │ │ reusable     │
                                  │ navigation/index   │ │ memory        │ │ procedures   │
                                  └──────────┬─────────┘ └──────┬────────┘ └──────────────┘
                                             │                  │
                                             └─────────┬────────┘
                                                       │
                                                       ▼
                                            ┌──────────────────────┐
                                            │ MEMORY READ PATH     │
                                            └──────────┬───────────┘
                                                       │
                                                       ▼
                                            current task arrives
                                                       │
                                            need old memory?
                                                │
                                      ┌─────────┴─────────┐
                                      │                   │
                                     NO                  YES
                                      │                   │
                                      │                   ▼
                                      │        read memory_summary
                                      │                   │
                                      │                   ▼
                                      │             search MEMORY.md
                                      │                   │
                                      │                   ▼
                                      │         maybe load skill /
                                      │         rollout summary
                                      │
                                      └──────────────┬────┘
                                                     ▼
                                             build current context
                                                     │
                                                     ▼
                                                   MODEL
```

---

# 2. FULL SESSION / ROLLOUT

Rollout là toàn bộ những gì xảy ra trong một phiên làm việc của agent.

Ví dụ:

```text
User:
Sửa API login.

Assistant:
Tôi sẽ đọc auth.ts.

Tool:
read auth.ts

Assistant:
Tôi thấy bug ở verifyToken().

Tool:
edit auth.ts

Tool:
run npm test

Tool result:
2 tests failed

Assistant:
Bug nằm ở middleware order.

Tool:
edit middleware.ts

Tool:
run npm test

Tool result:
all passed
```

Toàn bộ chuỗi trên là một rollout.

Có thể hiểu đơn giản:

```text
Rollout = cuốn phim đầy đủ của một phiên làm việc.
```

Rollout chứa cả thông tin quan trọng lẫn noise:

- user messages
- assistant responses
- tool calls
- tool results
- file reads
- file edits
- test output
- lỗi tạm thời
- các bước suy luận và thử nghiệm

Điểm quan trọng:

```text
Rollout đầy đủ
!=
toàn bộ thứ model phải nhìn thấy ở mọi turn.
```

---

# 3. ACTIVE CONTEXT

Active context là phần thông tin **model thực sự đang nhìn thấy ở turn hiện tại**.

Ví dụ rollout đã dài 200k token, nhưng model có thể chỉ được đưa:

```text
System instructions
Project instructions
Recent context
Compaction summary
Current user message
```

Có thể hình dung:

```text
Rollout      = toàn bộ quyển sổ.
Active context = những trang đang mở trên bàn.
```

---

# 4. KHI CONTEXT QUÁ LỚN

Model có context window hữu hạn.

Ví dụ:

```text
context window = 256k token
```

Ban đầu:

```text
used = 20k
```

Sau nhiều turn:

```text
used = 180k
```

Sau nữa:

```text
used = 230k+
```

Nếu tiếp tục giữ nguyên toàn bộ history thì model sẽ không còn đủ không gian cho:

- file mới
- tool output mới
- reasoning
- response
- instructions mới

Khi đó Codex cần **compaction**.

---

# 5. COMPACTION

Compaction không có nghĩa là:

```text
xóa lịch sử.
```

Nó có nghĩa là:

```text
biến một phần history dài thành representation nhỏ hơn
để agent tiếp tục task hiện tại.
```

Ví dụ lịch sử cũ:

```text
User: sửa login
Assistant: đọc auth
Tool: ...
Tool: ...
Tool: ...
Assistant: ...
...
100k token
```

Sau compaction có thể trở thành:

```text
Current goal:
Fix login authentication.

Progress:
- Read auth.ts
- Found token verification bug
- Fixed middleware ordering
- Tests currently pass

Important decisions:
- Keep JWT validation inside auth middleware
- Do not move validation into controller

Remaining work:
- Add integration test
```

Điểm quan trọng:

```text
Compaction không cố kể lại toàn bộ cuộc hội thoại.

Nó cố tạo đủ state để agent tiếp tục công việc.
```

---

# 6. COMPACTION SUMMARY = HANDOFF SUMMARY

Compaction summary nên được hiểu như một tờ bàn giao giữa hai agent.

Agent A hết ca.

Agent B không được đọc toàn bộ chat cũ.

Agent A để lại:

```text
Mục tiêu:
Sửa authentication.

Đã làm:
- Fix verifyToken()
- Fix middleware order

Quyết định:
- JWT vẫn validate ở middleware

Test:
- Unit tests pass
- Integration test chưa viết

File quan trọng:
- src/auth.ts
- src/middleware/auth.ts

Việc tiếp theo:
Viết integration test cho expired token.
```

Đó là tinh thần của CompactionSummary.

Câu dễ nhớ:

```text
Compaction summary không kể lại quá khứ.
Nó chuẩn bị cho agent tiếp tục tương lai.
```

---

# 7. NHỮNG GÌ COMPACTION SUMMARY CẦN GIỮ

## 7.1 Current Progress

Trả lời:

```text
Ta đang ở đâu?
```

Ví dụ:

```text
Authentication bug đã được fix.
Unit test pass.
Integration test chưa chạy.
```

---

## 7.2 Key Decisions

Trả lời:

```text
Ta đã chốt những quyết định gì?
```

Ví dụ:

```text
Không dùng Redis.
Dùng PostgreSQL.

Không đổi public API.

JWT verification nằm ở middleware.
```

Nếu mất decision, agent sau compact rất dễ quay lại một hướng đã bị loại bỏ.

---

## 7.3 Constraints

Constraint là điều không được vi phạm.

Ví dụ:

```text
Không đổi response format.
Không thêm dependency mới.
Chỉ sửa backend.
Không đổi database schema.
```

---

## 7.4 Remaining Work

Trả lời:

```text
Còn gì chưa làm?
```

Ví dụ:

```text
DONE:
[x] fix auth bug
[x] update middleware

TODO:
[ ] add integration test
[ ] run lint
[ ] update docs
```

---

## 7.5 Critical References

Các pointer quan trọng:

```text
src/auth/token.ts
src/middleware/auth.ts

function:
verifyAccessToken()

test:
tests/auth.integration.ts

error:
"Invalid issuer"
```

Không cần lưu toàn bộ file.

Chỉ cần biết nơi cần quay lại.

---

# 8. REBUILD MODEL-VISIBLE HISTORY

Sau compaction, Codex không nhất thiết giữ model-visible history giống raw history ban đầu.

Nó dựng lại một view mới, nhỏ hơn.

Có thể hình dung:

```text
OLD CONTEXT

████████████████████████████████████████████████████████
```

sau compact:

```text
NEW CONTEXT

████ initial / canonical context
████ recent raw user messages
████ compaction summary
```

Điểm quan trọng:

```text
Storage history
!=
Model-visible history
```

---

# 9. INITIAL / CANONICAL CONTEXT

Initial context bao gồm những thông tin có tính authoritative:

```text
System instructions
Developer instructions
Project instructions
AGENTS.md
Repository rules
```

Ví dụ:

```text
- dùng pnpm
- không sửa generated files
- phải chạy test trước khi hoàn thành
```

Những thứ này không nên bị tóm tắt thành kiểu:

```text
"Repo có vẻ thích dùng pnpm."
```

Vì summary có thể làm mất độ chính xác.

Có thể hiểu:

```text
Initial context = luật gốc.
```

Sau compaction, phần này được giữ/reinject riêng.

---

# 10. RECENT USER MESSAGES VÀ CON SỐ 20K TOKEN

Trong local/inline compaction path của Codex hiện có constant:

```rust
const COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000;
```

Ý nghĩa:

```text
20k KHÔNG phải threshold để bắt đầu compact.

20k là maximum token budget dành cho
raw recent USER messages sau local compaction.
```

Codex đi từ user message mới nhất ngược về cũ và giữ cho tới khi hết budget.

Ví dụ:

```text
budget = 20k

msg 100 = 2k
remaining = 18k

msg 99 = 1k
remaining = 17k

msg 98 = 8k
remaining = 9k

msg 97 = 9k
remaining = 0

STOP
```

Nếu message cuối cùng dài hơn phần budget còn lại thì nó có thể bị truncate để vừa budget.

---

# 11. TẠI SAO LÀ 20K?

Điều có thể khẳng định từ source:

```text
Codex local compaction dùng hard-coded budget = 20,000 token.
```

Điều **không nên khẳng định** nếu không có bằng chứng chính thức:

```text
20k là con số tối ưu do benchmark.
20k = một tỷ lệ cụ thể của 256k context.
20k được chọn theo một công thức toán học cụ thể.
```

Trong source public hiện tại, đây là một engineering constant.

Nó không phải:

```text
context_window * 8%
```

mà là:

```text
20_000
```

Vì vậy nên hiểu:

```text
20k = fixed retention budget
cho raw recent user instructions.
```

---

# 12. TẠI SAO CẦN GIỮ RAW USER MESSAGES?

Summary luôn có khả năng mất nuance.

Ví dụ user nói:

```text
Đừng đổi API /login.
Riêng /refresh thì có thể thay đổi.
```

Summary có thể vô tình viết:

```text
Do not change auth API.
```

Như vậy mất nuance.

Recent raw user messages giữ nguyên wording gốc của user để model có nguồn tham chiếu gần nhất.

Có thể hiểu:

```text
Compaction summary
= compressed working state

Recent raw user messages
= recent source of truth
```

---

# 13. 20K KHÔNG CÓ NGHĨA CONTEXT SAU COMPACT CHỈ CÒN 20K

Đây là điểm rất dễ nhầm.

Sai:

```text
Sau compact, context chỉ còn 20k.
```

Đúng hơn:

```text
20k chỉ là cap cho một thành phần:
recent raw user messages.
```

Ngoài ra context sau compact còn có thể có:

```text
canonical initial context
+
CompactionSummary
+
recent user messages <= 20k
+
các context cần thiết khác của runtime
```

---

# 14. LONG-TERM MEMORY KHÁC COMPACTION

Hai hệ phục vụ hai mục tiêu khác nhau.

```text
COMPACTION
=
giữ cho current session tiếp tục được.
```

```text
LONG-TERM MEMORY
=
mang kiến thức hữu ích qua nhiều session.
```

Không nên hiểu:

```text
chat dài
  ↓
summary
  ↓
summary đó = memory
```

Mà nên hiểu:

```text
                 ┌──────── current context / compaction
FULL ROLLOUT ────┤
                 └──────── long-term memory extraction
```

---

# 15. PHASE 1 — ROLLOUT MEMORY EXTRACTION

Khi một rollout đủ điều kiện, memory writer đọc rollout và hỏi:

```text
Trong session này, điều gì đáng dùng lại về sau?
```

Ví dụ rollout có:

```text
100 lần đọc file
20 error tạm thời
15 lần sửa code
5 decisions
1 bug quan trọng
```

Memory extraction không cần giữ mọi thứ.

Nó có thể lấy:

```text
- Auth middleware chạy trước provider adapter.
- Service account dùng API key.
- Authorization header có thể bị strip bởi proxy normalization.
- Test auth chạy bằng pnpm test auth.
```

Có thể hiểu:

```text
experience
    ↓
reusable knowledge
```

---

# 16. RAW MEMORY

Raw memory là kiến thức vừa được extract từ một rollout, chưa được hợp nhất sâu với các memory khác.

Ví dụ:

```text
Session X:

- API keys được encrypted trước khi lưu.
- auth.ts chạy trước provider adapter.
- 401 Missing API key xảy ra khi Authorization header bị strip.
```

Nó giống:

```text
ghi chú sau một buổi làm việc.
```

---

# 17. ROLLOUT SUMMARY

Rollout summary mô tả session đó đã làm gì.

Ví dụ:

```text
Implemented service account authentication.

Files changed:
- auth.ts
- serviceAccount.ts

Main decision:
API-key verification happens before provider dispatch.

Validation:
auth test suite passed.
```

Có thể phân biệt:

```text
memory
= knowledge

rollout summary
= lịch sử / evidence của một session
```

---

# 18. PHASE 2 — CONSOLIDATION

Qua nhiều session sẽ có nhiều raw memory.

Ví dụ:

```text
Session 1:
Repo uses pnpm.

Session 3:
Need pnpm to run tests.

Session 7:
npm command failed; repo expects pnpm.
```

Không nên giữ cả ba như ba memory độc lập mãi mãi.

Consolidation biến chúng thành:

```text
Repository package manager: pnpm.
Use pnpm for install/test commands.
```

Mục tiêu:

```text
deduplicate
+
clean
+
consolidate
+
keep durable knowledge
```

---

# 19. memory_summary.md

Đây là lớp memory nhỏ và cô đặc.

Ví dụ:

```text
# AI Gateway

Authentication:
- Service account uses API keys.
- Auth middleware runs before provider adapter.
- See MEMORY.md#authentication

Providers:
- Adapter normalizes protocol.
- See MEMORY.md#providers
```

Nó không chứa mọi detail.

Vai trò của nó:

```text
memory_summary.md = bản đồ / index memory.
```

---

# 20. MEMORY.md

Đây là memory chi tiết hơn.

Ví dụ:

```text
# Authentication

Architecture:
request
→ security middleware
→ auth
→ provider adapter

Service accounts:
- authenticated via API key
- key encrypted at rest
- validation happens before provider dispatch

Known failure:

Symptom:
401 Missing API key

Cause:
Authorization header removed during proxy normalization.

Verification:
pnpm test auth
```

---

# 21. skills/

Skill không chỉ nói "biết gì", mà còn nói "làm như thế nào".

Ví dụ memory:

```text
Auth tests chạy bằng pnpm test auth.
```

Skill:

```text
Khi debug auth:

1. inspect Authorization header
2. inspect normalization middleware
3. confirm middleware order
4. run auth tests
```

Có thể hiểu:

```text
Memory = knowledge.
Skill = reusable procedure.
```

---

# 22. MEMORY READ PATH

Codex không cần load toàn bộ memory vào mọi request.

Flow:

```text
Current task
    │
    ▼
Need old memory?
    │
 ┌──┴──┐
 │     │
NO    YES
 │     │
 │     ▼
 │  memory_summary
 │     │
 │     ▼
 │  relevant MEMORY.md section
 │     │
 │     ▼
 │  maybe skill / rollout summary
 │
 └──────────────► build active context
```

Ví dụ:

```text
User:
1 + 1 bằng mấy?
```

Không cần memory.

Nhưng:

```text
User:
Tiếp tục phần service account hôm qua.
```

Cần memory.

---

# 23. PROGRESSIVE DISCLOSURE

Memory được đọc theo tầng:

```text
small
 ↓
medium
 ↓
detailed
```

Ví dụ:

```text
memory_summary.md
      ↓
MEMORY.md
      ↓
skill
      ↓
rollout summary
```

Không phải:

```text
LOAD EVERYTHING
```

Mục đích là giữ active context nhỏ và chỉ lấy knowledge liên quan.

---

# 24. GHÉP HAI HỆ LẠI

```text
                    PAST
                     │
                     ▼
              LONG-TERM MEMORY
                     │
                     │ retrieve relevant knowledge
                     ▼
USER → ACTIVE CONTEXT → MODEL
          │
          │ context grows
          ▼
       COMPACTION
          │
          └──────────────→ ACTIVE CONTEXT MỚI
```

Có hai chiều:

```text
CURRENT SESSION
→ Active Context + Compaction
```

và:

```text
PAST SESSIONS
→ Long-Term Memory
```

---

# 25. VÍ DỤ TOÀN BỘ FLOW

User:

```text
Sửa module login.
```

Agent làm:

```text
read auth.ts
read middleware.ts
edit
test
edit
test
...
```

Rollout tăng dần:

```text
10k
30k
80k
150k
...
```

Khi cần compact:

```text
150k raw history
       │
       ▼
handoff summary
```

Summary:

```text
Goal:
Fix login.

Done:
- fixed verifyToken
- corrected middleware order

Decision:
- keep auth in middleware

Current:
unit test pass

Next:
integration test
```

Active context mới:

```text
System / initial context
+
recent raw user messages <= 20k
+
CompactionSummary
```

Agent tiếp tục.

Cuối / sau session:

```text
rollout
   │
   ▼
memory extraction
```

Extract:

```text
- Auth middleware runs before adapter.
- verifyToken lives in auth.ts.
- auth tests run with pnpm test auth.
```

Qua consolidation:

```text
memory_summary.md
MEMORY.md
skills/
rollout summaries
```

Ngày hôm sau:

```text
User:
Tiếp tục phần auth hôm qua.
```

Agent:

```text
current chat gần như trống
        │
        ▼
memory needed
        │
        ▼
memory_summary
        │
        ▼
MEMORY.md#authentication
        │
        ▼
build active context
        │
        ▼
model tiếp tục task
```

---

# 26. CÂU CHỐT

```text
Compaction
=
làm nhẹ working context hiện tại
để task hiện tại tiếp tục.
```

```text
Long-term Memory
=
mang kiến thức có giá trị từ quá khứ
vào những task / session tương lai.
```

Và:

```text
20k
=
maximum recent raw USER-message budget
trong local Codex compaction path,
không phải total context sau compact
và không phải threshold bắt đầu compact.
```

---

# 27. Source tham khảo chính

- OpenAI Codex repository  
  https://github.com/openai/codex

- Compaction implementation  
  https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs

- Memory architecture overview  
  https://github.com/openai/codex/blob/main/codex-rs/memories/README.md

- Memory consolidation prompt  
  https://github.com/openai/codex/blob/main/codex-rs/memories/write/templates/memories/consolidation.md

- Memory read path  
  https://github.com/openai/codex/blob/main/codex-rs/ext/memories/templates/memories/read_path.md

---

# 28. Mental model ngắn nhất

```text
FULL ROLLOUT
   │
   ├──────────────► CURRENT SESSION
   │                   │
   │                   ▼
   │              ACTIVE CONTEXT
   │                   │
   │            context quá lớn
   │                   │
   │                   ▼
   │              COMPACTION
   │                   │
   │                   ▼
   │         HANDOFF SUMMARY
   │         + RECENT USER MSGS
   │                   │
   │                   ▼
   │             CONTINUE TASK
   │
   └──────────────► LONG-TERM MEMORY
                       │
                       ▼
                PHASE 1 EXTRACT
                       │
                       ▼
                 RAW MEMORY
                 ROLLOUT SUMMARY
                       │
                       ▼
                PHASE 2 CONSOLIDATE
                       │
              ┌────────┼────────┐
              ▼        ▼        ▼
       memory_summary  MEMORY   skills
              │
              ▼
       RETRIEVE WHEN NEEDED
```
