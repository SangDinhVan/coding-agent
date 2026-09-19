# Coding Agent Safety Architecture — Draft Baseline

## 1. Core Safety Flow

```text
                         USER REQUEST
                              │
                              ▼
                         MAIN LLM
                              │
                      plan / tool call
                              │
                              ▼
                 ┌────────────────────────┐
                 │   ACTION NORMALIZER    │
                 │                        │
                 │ parse command/tool     │
                 │ normalize path/target  │
                 │ identify action type   │
                 └────────────┬───────────┘
                              │
                              ▼
                 ┌────────────────────────┐
                 │   HARD POLICY ENGINE   │
                 │        REGEX           │
                 └────────────┬───────────┘
                              │
              ┌───────────────┼────────────────┐
              │               │                │
            ALLOW           REVIEW            DENY
              │               │                │
              │               ▼                ▼
              │       ┌─────────────────┐   HARD BLOCK
              │       │   SLM SAFETY    │
              │       │    SUB-AGENT    │
              │       │                 │
              │       │ context-aware   │
              │       │ risk reviewer   │
              │       └────────┬────────┘
              │                │
              │        ┌───────┼────────┐
              │        │       │        │
              │      ALLOW    ASK      DENY
              │        │       │        │
              │        │       ▼        ▼
              │        │     HUMAN    BLOCK
              │        │    APPROVAL
              │        │    │      │
              │        │   YES     NO
              │        │    │       │
              │        │    │      BLOCK
              │        │    │
              └────────┴────┘
                       │
                       ▼
              ┌──────────────────────┐
              │ CAPABILITY / SCOPE   │
              │       GRANT          │
              └──────────┬───────────┘
                         │
                         ▼
                   CHECKPOINT
                         │
                         ▼
              ┌──────────────────────┐
              │      OS SANDBOX      │
              │                      │
              │ filesystem boundary  │
              │ network boundary     │
              │ process boundary     │
              │ credential boundary  │
              └──────────┬───────────┘
                         │
                         ▼
                   TOOL EXECUTION
                         │
                  ┌──────┴──────┐
                  ▼             ▼
             AUDIT / LOG     RECOVERY
                         │
                         ▼
                    TOOL RESULT
                         │
                         ▼
                INJECTION SCREENING 
                         │
                         ▼
                  MAIN LLM CONTINUES
```

---

## 2. Meaning of Hard Policy Decisions

### ALLOW

`ALLOW` is a narrow fast-path for operations that the deterministic policy considers clearly safe.

Typical properties:

- Low-risk operation.
- Narrow and predictable scope.
- No sensitive credential access.
- No dangerous external side effect.
- No need for contextual reasoning by the SLM.

Flow:

```text
Hard Policy
    │
  ALLOW
    │
    ▼
Capability Grant
    │
    ▼
Sandbox
    │
    ▼
Execute
```

`ALLOW` should be conservative and relatively uncommon.

---

### REVIEW

`REVIEW` is the default path for actions whose safety depends on context.

Examples may include:

- Running project code.
- Installing dependencies.
- Network access.
- Deleting or modifying files.
- Git operations with side effects.
- Deployment operations.
- External services or MCP tools.
- Actions whose necessity depends on the user's current request.

Flow:

```text
Hard Policy
    │
  REVIEW
    │
    ▼
SLM Safety Sub-Agent
    │
 ┌──┼─────────┐
 │  │         │
ALLOW ASK    DENY
 │   │         │
 │   ▼         ▼
 │ HUMAN      BLOCK
 │   │
 │ YES
 └───┘
   │
   ▼
Sandbox
   │
   ▼
Execute
```

The SLM is responsible for context-aware safety reasoning.

---

### DENY

`DENY` represents a hard security invariant.

Examples may include:

- Explicit protected credential paths.
- Clearly destructive host operations.
- Known sandbox escape patterns.
- Operations forbidden by system policy.

Flow:

```text
Hard Policy
    │
  DENY
    │
    ▼
  BLOCK
```

The SLM does **not** override a hard deny.

---

## 3. Role Separation

```text
Main LLM
    │
    │ proposes actions
    ▼

Hard Policy / Regex
    │
    │ identifies known-safe,
    │ contextual, or forbidden actions
    ▼

SLM Safety Sub-Agent
    │
    │ reviews contextual / uncertain actions
    ▼

Capability Layer
    │
    │ grants only the required scope
    ▼

OS Sandbox
    │
    │ physically enforces the final boundary
    ▼

Tool Execution
```

Core separation:

```text
Planner ≠ Reviewer ≠ Enforcer

Main LLM       = Planner
SLM Safety     = Reviewer
OS Sandbox     = Enforcer
```

---

## 4. Design Principle

> The main LLM proposes actions.  
> The hard policy handles deterministic rules.  
> The SLM reviews context-dependent actions.  
> The sandbox enforces the final execution boundary.

Another way to summarize the architecture:

```text
Regex:
"Is this action obviously safe, contextual, or forbidden?"

SLM:
"Should this contextual action be allowed for this specific task?"

Sandbox:
"Regardless of previous decisions, what can the process physically access?"
```

---

## 5. Current Research Boundary

This document only fixes the high-level architecture.

Topics to investigate separately later:

- Exact input/output schema of the Action Normalizer.
- Regex policy format and precedence.
- Exact input context provided to the SLM.
- SLM decision schema and confidence handling.
- Human approval strategy.
- Capability calculation.
- Sandbox implementation.
- Credential isolation.
- Network isolation.
- Checkpoint/recovery strategy.
- Audit/telemetry schema.
- Prompt-injection protection for tool results.
- Evaluation metrics and ablation experiments.

## 2.1 Action Normalizer → Hard Policy Engine (Input Schema)

```json
{
  "action_id": "uuid",
  "action_type": "shell_exec | file_write | file_delete | network_request | git_op | mcp_call | package_install",
  "tool_name": "Bash",
  "raw_input": "rm -rf ./build",
  "normalized": {
    "verb": "delete",
    "targets": [
      { "path": "/workspace/project/build", "path_class": "workspace_relative" }
    ],
    "flags": ["-r", "-f"],
    "network": null,
    "credentials_touched": []
  },
  "session_context": {
    "sandbox_active": true,
    "cwd": "/workspace/project",
    "git_status": "clean"
  }
}
```

**Field notes:**

- `normalized` — luôn match rule trên structure đã parse (verb/targets/flags), không match trên `raw_input` thô, để tránh lỗi shell-operator bypass (vd `safe-cmd && rm -rf /`).
- `path_class` — phân loại path theo nhóm (`workspace_relative`, `home_relative`, `system`, `credential_dir`...) thay vì hardcode path tuyệt đối, để rule viết theo class.
- `session_context` — chỉ chứa dữ kiện kỹ thuật khách quan (sandbox on/off, git state...), không chứa suy luận ý định người dùng — đó là việc của SLM, không phải Hard Policy.
- `credentials_touched` — tách riêng khỏi `targets` vì đây thường là nhóm hard-deny ưu tiên cao nhất, cần match độc lập, nhanh.

---

## 2.2 Hard Policy Engine → SLM / Capability Grant (Output Schema)

```json
{
  "action_id": "uuid",
  "decision": "ALLOW | REVIEW | DENY",
  "matched_rule": {
    "rule_id": "hard_deny_credential_paths_v3",
    "rule_type": "hard_deny | hard_allow | review_default",
    "pattern": "path_class:credential_dir"
  },
  "reason": "target path matches protected credential directory",
  "requires_capability_scope": {
    "fs_write": ["/workspace/project/build"],
    "network": [],
    "process_spawn": false
  },
  "context_for_reviewer": {
    "why_flagged": "delete operation outside explicit allow-list, but inside workspace",
    "risk_signals": ["recursive_delete", "no_dry_run"]
  },
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `matched_rule` — luôn trả về, kể cả khi `ALLOW`. Không có quyết định nào "tự nhiên pass" — audit log cần biết *vì sao*, không chỉ *rằng*.
- `rule_type` tách khỏi `decision` — vì `hard_deny` là ranh giới tuyệt đối SLM không được override, còn `review_default` thì SLM có quyền quyết. Gộp chung sẽ mất phân biệt này ở downstream.
- `requires_capability_scope` — tính ngay tại Hard Policy (nơi duy nhất biết chính xác action cần gì từ `normalized`), truyền thẳng xuống Capability Grant, tránh phải parse lại raw action lần hai.
- `context_for_reviewer` — chỉ xuất hiện khi `decision = REVIEW`. Đây là "biên bản bàn giao" cho SLM, không phải quyết định thay SLM — giúp SLM không phải re-derive risk signal từ đầu.
- `policy_version` — bắt buộc cho audit/compliance, để biết chính xác rule set nào đang chạy khi có sự cố.

## 3.1 Hard Policy Engine → SLM Safety Sub-Agent (Input Schema)

```json
{
  "action_id": "uuid",
  "hard_policy_output": {
    "decision": "REVIEW",
    "matched_rule": {
      "rule_id": "review_default_delete_ops",
      "rule_type": "review_default",
      "pattern": "verb:delete AND path_class:workspace_relative"
    },
    "context_for_reviewer": {
      "why_flagged": "delete operation outside explicit allow-list, but inside workspace",
      "risk_signals": ["recursive_delete", "no_dry_run"]
    }
  },
  "task_context": {
    "user_request_summary": "Clean up build artifacts before running fresh build",
    "recent_actions": [
      { "action_type": "shell_exec", "verb": "read", "target": "package.json", "outcome": "success" }
    ],
    "turn_index": 4
  },
  "session_context": {
    "sandbox_active": true,
    "cwd": "/workspace/project",
    "git_status": "clean",
    "prior_flags_this_session": 0
  },
  "injection_flags": []
}
```

**Field notes:**

- `hard_policy_output` — SLM chỉ nhận phần đã lọc qua Hard Policy (không nhận raw action lại từ đầu), giữ nguyên nguyên tắc mỗi tầng chỉ xử lý phần việc của mình.
- `task_context.user_request_summary` — bắt buộc, vì đây là thứ Hard Policy (deterministic) không có nhưng SLM cần để đánh giá "hành động này có hợp lý với ý định người dùng không". Đây là lằn ranh phân công: Hard Policy không suy luận ý định, SLM thì có.
- `recent_actions` — cửa sổ ngắn (vài action gần nhất), không phải toàn bộ lịch sử phiên, để tránh SLM phải xử lý context quá dài mỗi lần review.
- `prior_flags_this_session` — cần thiết để SLM (hoặc tầng ngay sau nó) biết đang gần ngưỡng escalate-về-người hay chưa (ví dụ pattern 3-lần-liên-tiếp/20-lần-phiên).
- `injection_flags` — nếu tool result gần nhất từng bị đánh dấu nghi ngờ prompt-injection, field này không rỗng; SLM phải nhìn thấy nó để tăng mức nghi ngờ với hành động hiện tại dù bản thân hành động trông bình thường.

---

## 3.2 SLM Safety Sub-Agent → Output (theo từng case quyết định)

### Case: ALLOW

```json
{
  "action_id": "uuid",
  "decision": "ALLOW",
  "confidence": 0.94,
  "reason": "Delete target matches build-artifact pattern; consistent with stated user intent; no credential or system path involved.",
  "risk_assessment": {
    "reversibility": "low_impact",
    "blast_radius": "workspace_only"
  },
  "policy_version": "2026-08-13.1"
}
```

- `confidence` — bắt buộc trên mọi case, kể cả ALLOW. Đây là chỗ khác Hard Policy: Hard Policy là binary chắc chắn, SLM là suy luận xác suất — output phải phản ánh đúng bản chất đó, không giả vờ chắc chắn tuyệt đối.
- `risk_assessment` — không phải để quyết định (đã quyết ở `decision` rồi), mà để audit log và để Capability Grant tầng sau biết mức độ nới scope hợp lý.

### Case: ASK (chuyển lên Human Approval)

```json
{
  "action_id": "uuid",
  "decision": "ASK",
  "confidence": 0.51,
  "reason": "Recursive delete with no dry-run; user intent plausible but not explicit; irreversible if wrong.",
  "human_prompt": {
    "summary": "Delete ./build directory (recursive, no confirmation prompt from the command itself)",
    "what_could_go_wrong": "If build/ contains uncommitted output not reproducible by a rebuild, it will be lost permanently.",
    "suggested_options": ["approve_once", "approve_for_session", "deny"]
  },
  "escalation_meta": {
    "consecutive_blocks_this_session": 1,
    "escalation_threshold_hit": false
  },
  "policy_version": "2026-08-13.1"
}
```

- `human_prompt` — tách riêng khỏi `reason`. `reason` là log nội bộ cho hệ thống; `human_prompt` là thứ hiển thị cho người, phải ngắn, dễ hiểu, không lẫn thuật ngữ nội bộ (`rule_id`, `path_class`...).
- `escalation_meta` — SLM không tự quyết định khi nào fallback về manual-approval-toàn-phần; nó chỉ báo cáo con số, còn ngưỡng (vd 3 liên tiếp / 20 mỗi phiên) do tầng Human Approval Strategy xử lý — giữ đúng nguyên tắc SLM không tự ý mở rộng quyền hạn của chính nó.

### Case: DENY

```json
{
  "action_id": "uuid",
  "decision": "DENY",
  "confidence": 0.88,
  "reason": "Delete target overlaps with a path outside workspace boundary despite passing normalization; contradicts stated task scope.",
  "risk_assessment": {
    "reversibility": "irreversible",
    "blast_radius": "outside_workspace"
  },
  "policy_version": "2026-08-13.1"
}
```

- Không có `human_prompt` ở case này — SLM DENY vẫn là **soft deny** (khác `hard_deny` của Hard Policy Engine): nó chặn hành động nhưng không nhất thiết chặn cả phiên; Main LLM có thể thử phương án khác thay vì phải dừng chờ người. Nếu muốn cho phép người override một SLM-DENY, nên thiết kế đó là một luồng riêng (người chủ động yêu cầu "cho tôi ghi đè"), không tự động kèm trong output này.

**Ghi chú xuyên suốt cả 3 case:**

- SLM **không bao giờ được set `decision` khác giá trị Hard Policy đã cho phép nó set** — nếu Hard Policy trả `hard_deny`, action không bao giờ tới được SLM (theo đúng luồng bạn đã vẽ: "SLM does not override a hard deny"). Vì vậy output schema của SLM chỉ hợp lệ khi `hard_policy_output.decision = "REVIEW"`.
- `policy_version` giữ nguyên xuyên suốt các tầng để một sự cố có thể trace lại đúng version rule + đúng version prompt/model của SLM tại thời điểm quyết định.

## 3.3 SLM Safety Sub-Agent (ASK) → Human Approval (Input Schema)

```json
{
  "action_id": "uuid",
  "human_prompt": {
    "summary": "Delete ./build directory (recursive, no confirmation prompt from the command itself)",
    "what_could_go_wrong": "If build/ contains uncommitted output not reproducible by a rebuild, it will be lost permanently.",
    "suggested_options": ["approve_once", "approve_for_session", "deny"]
  },
  "task_context": {
    "user_request_summary": "Clean up build artifacts before running fresh build"
  },
  "slm_assessment": {
    "confidence": 0.51,
    "reason": "Recursive delete with no dry-run; user intent plausible but not explicit; irreversible if wrong."
  },
  "escalation_meta": {
    "consecutive_blocks_this_session": 1,
    "escalation_threshold_hit": false
  },
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `human_prompt` là phần duy nhất **bắt buộc hiển thị** cho người; `slm_assessment` và `task_context` là phụ trợ — có thể ẩn sau nút "xem chi tiết" để không làm loãng UX của quyết định chính.
- `slm_assessment.reason` giữ nguyên (không rewrite lại cho người) để giữ tính minh bạch — người dùng thấy đúng lý do máy đưa ra, không phải bản đã được "làm mềm".
- Không đưa `matched_rule.rule_id` hay các field nội bộ khác của Hard Policy vào đây — người dùng không cần biết cấu trúc rule, chỉ cần biết cái gì sắp xảy ra và rủi ro gì.

---

## 3.4 Human Approval → Output Schema

```json
{
  "action_id": "uuid",
  "human_decision": "YES | NO",
  "scope_of_approval": "once | session | never_ask_again_for_pattern",
  "approved_by": "user_id_or_session_owner",
  "timestamp": "2026-08-13T10:42:11Z",
  "note": null
}
```

**Field notes:**

- `scope_of_approval` — tách riêng khỏi `human_decision` vì "YES" không đồng nghĩa "cho phép mọi lần sau". Ba giá trị tương ứng: chỉ lần này / cả phiên hiện tại / thêm vào allow-list lâu dài (tương đương thêm rule mới vào Hard Policy — nên đây là điểm nối ngược lại tầng 2, không phải chỉ dừng ở phiên này).
- `never_ask_again_for_pattern` — nếu người chọn giá trị này, hệ thống cần một bước riêng để **ghi lại thành rule mới** ở Hard Policy Engine (không tự động, cần review định kỳ) — tránh tích lũy allow-list vô tội vạ qua thời gian mà không ai kiểm lại.
- `approved_by` — bắt buộc cho audit, đặc biệt trong môi trường nhiều người dùng chung một session/agent (CI, shared workspace).
- `note` — optional, cho người giải thích lý do approve/deny, hữu ích khi review lại log sau này.

---

## 4.1 (SLM/Human) ALLOW → Capability / Scope Grant (Input Schema)

```json
{
  "action_id": "uuid",
  "final_decision": "ALLOW",
  "decided_by": "hard_policy | slm | human",
  "requires_capability_scope": {
    "fs_write": ["/workspace/project/build"],
    "network": [],
    "process_spawn": false
  },
  "scope_of_approval": "once",
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `decided_by` — bắt buộc, vì Capability Grant (và audit log sau này) cần biết quyết định tới từ tầng nào — một action được Hard Policy tự ALLOW rủi ro thấp hơn nhiều so với action được Human override sau khi SLM ASK, dù cả hai đều đi tới đây với `final_decision = ALLOW`.
- `requires_capability_scope` **không tính lại ở đây** — nó được kế thừa nguyên vẹn từ output gốc của Hard Policy Engine (mục 2.2). Capability Grant chỉ có nhiệm vụ **cấp đúng bằng scope đó, không hơn** — đây là nơi dễ bị "scope creep" nhất nếu để tầng này tự suy diễn lại quyền cần thiết.
- `scope_of_approval` truyền tiếp xuống để Capability Grant biết cấp quyền cho 1 lần thực thi hay cho cả phiên (ảnh hưởng tới thời gian sống của capability token).

---

## 4.2 Capability / Scope Grant → Output Schema (giao cho Checkpoint / Sandbox)

```json
{
  "action_id": "uuid",
  "capability_token": "cap_9f2a...",
  "granted_scope": {
    "fs_write": ["/workspace/project/build"],
    "fs_read": ["/workspace/project"],
    "network": [],
    "process_spawn": false,
    "env_vars_exposed": []
  },
  "expires": "single_use | end_of_session | timestamp",
  "issued_at": "2026-08-13T10:42:12Z",
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `capability_token` — sandbox không nên nhận lại toàn bộ lịch sử quyết định (Hard Policy → SLM → Human), chỉ nhận một token đại diện cho scope đã được duyệt. Giữ nguyên tắc: sandbox chỉ cần biết "được làm gì", không cần biết "tại sao" — giảm bề mặt tấn công nếu logic quyết định phía trên có lỗi.
- `fs_read` xuất hiện dù ban đầu chỉ request `fs_write` — vì ghi file thường ngầm định cần đọc trước (kiểm tra tồn tại, permission...). Đây là chỗ Capability Grant **được phép mở rộng nhẹ** dựa trên hiểu biết kỹ thuật về action_type, khác với việc tự ý mở rộng theo suy đoán rủi ro (điều đó là việc của SLM, không phải của tầng này).
- `expires` — bắt buộc rõ ràng, không để mặc định ngầm. `single_use` nên là default an toàn nhất trừ khi `scope_of_approval = session` được truyền từ tầng trên.

## 5.1 Capability Grant → Checkpoint (Input Schema)

```json
{
  "action_id": "uuid",
  "capability_token": "cap_9f2a...",
  "action_type": "file_delete",
  "granted_scope": {
    "fs_write": ["/workspace/project/build"],
    "fs_read": ["/workspace/project"],
    "network": [],
    "process_spawn": false
  },
  "checkpoint_policy": {
    "required": true,
    "strategy": "fs_snapshot | git_stash | process_state | none",
    "snapshot_targets": ["/workspace/project/build"]
  },
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `checkpoint_policy.required` — không phải mọi action đều cần checkpoint. Ví dụ `network_request` read-only thì `strategy: none`. Field này nên được set sẵn từ Hard Policy Engine (theo `action_type`) chứ không tự Checkpoint quyết định — giữ nguyên tắc mỗi tầng chỉ làm đúng việc của mình.
- `snapshot_targets` — luôn là tập con của `granted_scope.fs_write`, không bao giờ lớn hơn. Checkpoint không tự ý mở rộng phạm vi snapshot ra ngoài scope đã cấp — nếu cần snapshot rộng hơn, đó là dấu hiệu `granted_scope` ở tầng trước bị tính thiếu, không phải việc Checkpoint tự bù.
- `strategy` khác nhau theo action_type: `fs_snapshot` cho file ops, `git_stash`/`git_ref` cho git ops phá hủy, `process_state` cho việc kill/restart process. Không dùng chung một cơ chế cho mọi loại action vì chi phí và tính khả thi rollback khác nhau hoàn toàn.

---

## 5.2 Checkpoint → Output Schema (giao cho OS Sandbox)

```json
{
  "action_id": "uuid",
  "checkpoint_id": "ckpt_7b1e...",
  "checkpoint_status": "captured | skipped | failed",
  "snapshot_ref": {
    "type": "fs_snapshot",
    "location": "/var/agent-checkpoints/ckpt_7b1e",
    "targets_captured": ["/workspace/project/build"]
  },
  "captured_at": "2026-08-13T10:42:12Z",
  "rollback_available": true,
  "ttl": "end_of_session"
}
```

**Field notes:**

- `checkpoint_status = "failed"` **phải chặn execution**, không được âm thầm tiếp tục — nếu checkpoint bắt buộc (`required: true`) nhưng không chụp được, Sandbox không nên nhận quyền chạy action đó. Đây là điểm dễ bị bỏ sót: người ta hay coi checkpoint là "nice to have" nên để fail-open, nhưng với action `checkpoint_policy.required = true` thì phải fail-closed.
- `rollback_available` — tách riêng khỏi `checkpoint_status`, vì có trường hợp chụp được (`captured`) nhưng rollback không khả thi về mặt kỹ thuật (ví dụ process đã bị kill, không "un-kill" được — chỉ có thể restart, không phải rollback thật). Recovery tầng sau cần biết sự khác biệt này để chọn đúng chiến lược (rollback vs restart-and-reconcile).
- `snapshot_ref.location` — lưu ngoài phạm vi mà action sắp thao tác (không lưu trong chính `/workspace/project/build` sắp bị xóa!) — nghe hiển nhiên nhưng là lỗi thực tế phổ biến khi checkpoint dùng chung filesystem với target.
- `ttl` — checkpoint không nên tồn tại vĩnh viễn; theo `scope_of_approval` kế thừa từ tầng trước, thường dọn theo cuối phiên hoặc theo thời gian cố định để tránh phình dung lượng.

---

## 5.3 Khi Recovery cần dùng lại Checkpoint (Input Schema)

```json
{
  "action_id": "uuid",
  "checkpoint_id": "ckpt_7b1e...",
  "trigger_reason": "post_execution_audit_flag | execution_error | user_requested_undo",
  "recovery_mode": "full_restore | partial_restore | restart_and_reconcile"
}
```

**Field notes:**

- `trigger_reason` phân biệt rõ 3 nguồn gọi Recovery: lỗi kỹ thuật khi thực thi (`execution_error`), phát hiện sai sau khi Audit/Log phân tích lại (`post_execution_audit_flag` — đúng ví dụ `rm -rf` dính nhầm file chưa commit), hoặc người dùng chủ động yêu cầu undo. Ba nguồn này cần log riêng vì ý nghĩa rủi ro khác nhau (lỗi hệ thống vs. lỗi phán đoán an toàn).
- `recovery_mode` không mặc định `full_restore` — với process/network action, `restart_and_reconcile` mới hợp lý vì không có state để "restore" theo nghĩa file.

## 6.1 Checkpoint → OS Sandbox (Input Schema)

```json
{
  "action_id": "uuid",
  "capability_token": "cap_9f2a...",
  "checkpoint_id": "ckpt_7b1e...",
  "action_type": "file_delete",
  "raw_input": "rm -rf ./build",
  "granted_scope": {
    "fs_write": ["/workspace/project/build"],
    "fs_read": ["/workspace/project"],
    "network": [],
    "process_spawn": false,
    "env_vars_exposed": []
  },
  "sandbox_mode": "kernel_enforced | container | vm",
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- Sandbox nhận `capability_token`, **không nhận lại** `matched_rule`, `slm_assessment`, hay bất kỳ metadata quyết định nào từ các tầng trước — đúng nguyên tắc ở mục 4.2: sandbox chỉ cần biết "được làm gì", không cần biết "tại sao". Nếu token đủ để enforce scope thì không có lý do truyền thêm context quyết định xuống đây — càng ít thông tin sandbox phải tin tưởng, bề mặt lỗi càng nhỏ.
- `raw_input` vẫn cần truyền xuống (dù đã bị normalize và duyệt ở các tầng trên) vì đây là tầng **thực sự chạy lệnh** — nhưng sandbox không dùng nó để ra quyết định, chỉ dùng để thực thi trong biên đã bị khóa bởi `granted_scope`. Nếu lệnh thực tế cố vượt ra ngoài `granted_scope` (dù đã qua hết các tầng review), sandbox phải chặn tại đây — đây là lưới an toàn cuối, độc lập với việc các tầng trên có sai hay không.
- `sandbox_mode` — khai báo tường minh loại enforcement đang dùng (kernel-level như Seatbelt/landlock, hay container, hay VM), vì audit log cần biết mức độ cô lập thực tế tại thời điểm chạy, không chỉ biết "có sandbox".

---

## 6.2 OS Sandbox → Output Schema (Tool Result)

```json
{
  "action_id": "uuid",
  "execution_status": "success | blocked_by_sandbox | runtime_error",
  "sandbox_violation": {
    "occurred": false,
    "attempted_target": null,
    "violation_type": null
  },
  "stdout": "...",
  "stderr": "",
  "exit_code": 0,
  "executed_at": "2026-08-13T10:42:13Z",
  "checkpoint_id": "ckpt_7b1e..."
}
```

**Field notes:**

- `sandbox_violation` — **luôn có mặt trong output**, kể cả khi `occurred: false`. Đây là field quan trọng nhất về mặt an toàn: nó cho biết liệu lệnh thực tế có cố gắng vượt ra ngoài `granted_scope` hay không, bất kể exit code là gì. Một lệnh có thể vẫn "success" theo exit code nhưng đã bị sandbox âm thầm chặn một phần thao tác (ví dụ ghi file bị từ chối nhưng phần còn lại của script vẫn chạy tiếp) — nếu không tách riêng field này, tín hiệu đó dễ bị nuốt mất trong `stdout/stderr` thô.
- `execution_status = "blocked_by_sandbox"` là tín hiệu khác hẳn `runtime_error` — cái đầu nghĩa là lệnh cố làm gì đó ngoài quyền được cấp (cần escalate ngược lên SLM/audit để xem xét: có phải injection, có phải action bị hiểu sai từ đầu), cái sau chỉ là lỗi thực thi bình thường (file không tồn tại, permission hệ thống thật...). Gộp hai loại này lại sẽ làm mất khả năng phân biệt "có ai đó đang cố vượt rào" với "lệnh chỉ đơn giản là lỗi".
- `checkpoint_id` được trả kèm lại trong output — để `AUDIT/LOG` và `RECOVERY` (bước ngay sau trong diagram gốc của bạn) không phải tra cứu lại checkpoint nào tương ứng với action nào; giữ liên kết action ↔ checkpoint xuyên suốt tới cuối pipeline.
- Không có field `reason` hay `decision` ở tầng này — Sandbox không "quyết định" theo nghĩa các tầng trước, nó chỉ **báo cáo sự thật đã xảy ra**. Nếu thấy mình đang muốn thêm field kiểu giải thích/đánh giá rủi ro vào đây, đó là dấu hiệu logic đang bị đặt sai tầng.

---

## 6.3 Nhánh rẽ sau Tool Execution (theo đúng diagram gốc)
- `AUDIT/LOG` luôn chạy, không điều kiện — mọi `action_id` phải có bản ghi, kể cả khi mọi thứ suôn sẻ.
- `RECOVERY` chỉ chạy có điều kiện, dùng schema đã định nghĩa ở mục 5.3 (`trigger_reason` tương ứng với 3 nguồn: `execution_error`, `post_execution_audit_flag`, `user_requested_undo`).

## 7.1 Tool Execution → Audit/Log (Input Schema)

```json
{
  "action_id": "uuid",
  "trace": {
    "hard_policy_decision": "REVIEW",
    "hard_policy_rule_id": "review_default_delete_ops",
    "slm_decision": "ASK",
    "slm_confidence": 0.51,
    "human_decision": "YES",
    "human_scope_of_approval": "once",
    "decided_by": "human"
  },
  "capability_token": "cap_9f2a...",
  "checkpoint_id": "ckpt_7b1e...",
  "execution_status": "success",
  "sandbox_violation": { "occurred": false },
  "raw_input": "rm -rf ./build",
  "actor": {
    "session_id": "sess_xyz",
    "user_id": "user_id_or_session_owner",
    "agent_version": "main_llm_v_x"
  },
  "timestamps": {
    "requested_at": "2026-08-13T10:42:10Z",
    "decided_at": "2026-08-13T10:42:12Z",
    "executed_at": "2026-08-13T10:42:13Z"
  },
  "injection_flags": [],
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `trace` gộp lại quyết định từ **tất cả các tầng đã đi qua**, không chỉ tầng cuối. Đây là điểm khác biệt quan trọng so với mọi schema trước đó trong pipeline: các tầng trước (Sandbox, Capability Grant) cố tình *không* nhận lại các field này để giảm bề mặt tin cậy, nhưng Audit/Log thì ngược lại — nó là nơi **duy nhất** cần full trace để trả lời câu hỏi "tại sao hành động này được phép chạy" khi có sự cố.
- `decided_by` xuất hiện lại ở đây (đã có từ mục 4.1) vì đây là field hay được dùng để filter/alert nhất: một tổ chức có thể muốn cảnh báo riêng cho mọi action có `decided_by: human` mà sau đó `execution_status != success`, để đánh giá lại chất lượng approval của con người theo thời gian.
- `timestamps` tách 3 mốc riêng (`requested_at`, `decided_at`, `executed_at`) thay vì một timestamp duy nhất — vì độ trễ giữa các mốc này (đặc biệt `requested_at → decided_at`) là chỉ số hữu ích để đo approval fatigue hoặc bottleneck ở tầng SLM/Human, tách biệt với thời gian thực thi thật.
- `injection_flags` được giữ lại xuyên suốt tới đây — nếu action này từng bị flag nghi ngờ injection ở bất kỳ điểm nào trong task, audit log cần giữ lại bằng chứng đó ngay cả khi cuối cùng action vẫn được ALLOW hợp lệ (không phải injection thật) — để phục vụ việc cải thiện classifier injection sau này bằng false-positive/false-negative thực tế.

---

## 7.2 Audit/Log → Output (ghi vào audit store, không trả ngược lên pipeline)

```json
{
  "action_id": "uuid",
  "log_id": "log_4d8e...",
  "logged_at": "2026-08-13T10:42:14Z",
  "retention_class": "standard | extended_incident | legal_hold",
  "anomaly_flags": [],
  "storage_ref": "s3://agent-audit/2026-08-13/log_4d8e"
}
```

**Field notes:**

- `retention_class` — không phải mọi log giữ như nhau. Log của action có `sandbox_violation.occurred = true` hoặc `injection_flags` không rỗng nên tự động nâng lên `extended_incident`, tách biệt khỏi log routine để không bị dọn tự động theo TTL thông thường.
- `anomaly_flags` — đây là chỗ để một hệ thống phân tích **sau thực thi** (không phải real-time) gắn thêm phát hiện muộn, ví dụ: "action này match pattern của một vụ injection đã biết ở phiên khác" — chính là nguồn kích hoạt `trigger_reason: post_execution_audit_flag` cho Recovery ở mục 5.3.
- Output này **không quay lại pipeline chính** — nó là nhánh cụt về mặt luồng thực thi (khớp với diagram gốc: `AUDIT/LOG` không có mũi tên nối tiếp sang `TOOL RESULT`, chỉ `RECOVERY` mới có).

---

## 7.3 Tool Result → Main LLM Continues (Input Schema — khép vòng lặp)

```json
{
  "action_id": "uuid",
  "tool_name": "Bash",
  "execution_status": "success",
  "output_for_model": {
    "stdout": "...",
    "stderr": "",
    "exit_code": 0
  },
  "safety_annotations": {
    "injection_screened": true,
    "injection_detected": false,
    "sandbox_violation": false
  },
  "policy_version": "2026-08-13.1"
}
```

**Field notes:**

- `output_for_model` tách riêng khỏi mọi field an toàn/quyết định khác — đây là **phần duy nhất** thực sự đi vào context của Main LLM. Toàn bộ `trace`, `capability_token`, `checkpoint_id`... dừng lại ở Audit/Log, không rò xuống model — giữ đúng nguyên tắc "mỗi tầng chỉ biết đủ phần việc của mình", áp dụng ngược cả cho chính Main LLM.
- `safety_annotations` là ngoại lệ duy nhất được phép lọt vào cùng model — vì đây chính là điểm nối với tầng "Injection Screening" đã bàn ở lượt trước: nếu `injection_detected: true`, annotation này cần hiển thị cho Main LLM biết để nó tự điều chỉnh cách diễn giải nội dung vừa nhận, **đồng thời** cần được Action Normalizer/Hard Policy đọc lại ở vòng lặp kế tiếp (hành động dựa trên tool result này nên tự động bị đẩy lên REVIEW dù bình thường sẽ ALLOW) — đúng ý đã thống nhất trước đó về injection flag phải "chảy ngược" vào Hard Policy.

---

Vậy là đã khép trọn vòng: **Action Normalizer → Hard Policy → SLM → Human → Capability Grant → Checkpoint → Sandbox → Execution → Audit/Recovery → Tool Result → Main LLM.** Mỗi mũi tên trong diagram gốc giờ đã có input/output schema tương ứng.
---

## 8. Implementation Status — Sandbox V1 (2026-09-12)

The implemented V1 scope is the **OS Sandbox / Physical Enforcer**, not the
complete target pipeline described above. New sessions use live workspace mode;
metadata created before this migration remains legacy shadow mode.

### Enforced now

- All model-controlled shell and filesystem tools are bound to one Docker child
  session and fail closed when no running session exists. Daemon preflight
  accepts Linux rootless (`ROOTLESS`) and Docker Desktop on Windows/macOS
  (`VM_ISOLATED`), while direct rootful daemons (`ROOTFUL_BARE`) are rejected.
  `VM_ISOLATED` relies on Docker Desktop's Linux VM and is not equivalent to
  genuine rootless Docker; that weaker boundary is accepted for this project's
  scope.
- The container uses an immutable image, `network=none`, a read-only root
  filesystem, all capabilities dropped, no-new-privileges, cgroups v2
  CPU/memory/PID limits, bounded `/tmp`, and one exact writable workspace bind.
  These hardening arguments are identical for every isolation classification.
- New live sessions mount the host repository RW, so successful tool effects are
  visible immediately. The child runs as `0:0` only inside the daemon boundary:
  a rootless user namespace on Linux or Docker Desktop's Linux VM on
  Windows/macOS. Legacy shadow sessions retain image user `65532:65532`.
- Existing `.git`, `.env*`, credential paths, known cache/build directories,
  and paths selected by `.agentignore` are replaced inside live children by
  protected empty read-only masks. Ordinary `.gitignore` entries remain writable;
  Git versioning policy is not treated as an access-control boundary. Host home,
  Docker socket, inherited host secrets, and paths outside the selected workspace
  are not mounted.
- Runtime inspect/probe and resume reconciliation verify the exact mode, bind
  source, mask source/destination/read-only state, user, image, labels,
  privileges, network, rootfs, and resource limits. Mismatch fails closed.
- Session stop, crash, `/discard`, or TTL cleanup never deletes or claims to
  restore live repository changes. Review and undo use Git or the IDE.
- Legacy metadata without `workspace_mode` is treated as shadow and is never
  auto-migrated. Its independent snapshot, shadow Git baseline, deterministic
  exact-hash whole-set approval, conflict checks, durable apply journal, rollback,
  and `/discard` behavior remain available.
- A host heartbeat/disk watchdog and exact-label TTL cleanup support lifecycle
  safety for both modes.

### Meaning of target fields in V1

- `granted_scope.fs_read` and `granted_scope.fs_write` are policy/audit intent;
  kernel enforcement is workspace-wide except for the live paths masked at
  session creation.
- Live mode deliberately has no final whole-set approval gate or automatic file
  rollback. Successful tool mutations are already in the repository.
- Mask discovery protects denylisted, `.agentignore`, and known cache/build
  entries that exist when the session starts; it cannot pre-mount every future
  filename that arbitrary shell may create later. Mask sets persist with a
  session, so policy changes apply to newly created sessions.
- `sandbox_violation` is represented as `detected | not_detected | unknown`.
  Descriptor helpers can prove certain traversal violations, while arbitrary
  shell execution often yields only an observation rather than syscall intent.
- Per-action checkpoints remain future work. The legacy shadow baseline remains
  the checkpoint only for older shadow sessions.
- Network grants accept only denied/empty scope in V1. There is no egress proxy
  or package-install exception.

### Deferred target layers

Safety V2 covers deterministic action normalization, hard policy, and scoped
capability calculation. Safety V3 covers the SLM reviewer, persistent human
approval scopes, injection screening, controlled egress, stronger runtime
profiles, content-based secret detection, and richer live rollback/checkpoints.

Operational commands, exact image digests, verification commands, recovery, and
limitations are documented in `sandbox_v1_operations.md`.
