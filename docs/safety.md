# Safety architecture cho coding agent

Trạng thái: **Thiết kế mục tiêu để review, chưa triển khai.**
Cập nhật: 2026-10-04. Phạm vi hiện tại là tài liệu; kế hoạch code sẽ lập sau.

## 1. Agent được làm gì, khi nào hỏi, quyền được enforce ra sao?

Giữ sự tách biệt giữa Main LLM đề xuất, policy quyết định và sandbox cưỡng chế
quyền. Reviewer giúp suy luận theo ngữ cảnh; ranh giới bảo mật thực sự nằm ở OS
sandbox và các adapter thực thi do control plane quản lý.

Thiết kế approval được điều chỉnh thành `ask_all`, `ask_on_escalation` và
`auto_review`; đề xuất mặc định `ask_on_escalation` để giảm approval fatigue.
Headless và backend profile là các lựa chọn độc lập, chịu trần host policy.
Code hiện chưa có các mode này: `ASK` đang là policy decision, không phải mode.
Reviewer tạm dùng core LLM qua request riêng; sau này có thể thay bằng SLM.

Bảo vệ project gốc, dirty/untracked work, credential và dịch vụ bên ngoài ngay
cả khi model làm theo injection hoặc repo/dependency độc hại. Parse được một
lệnh không chứng minh effects của nó an toàn.

**Đề xuất mặc định SHADOW:** nhập dữ liệu được chọn vào workspace riêng, chạy
code offline và xuất diff đã niêm phong để user duyệt. LIVE cập nhật IDE ngay
nhưng có thể phá dữ liệu gốc/kích hoạt watcher trước review; không có cùng bảo
đảm. Lựa chọn này cần chốt trước implementation plan.

### 1.1 Quyền nền của một session

Phần này là **hợp đồng mục tiêu**, không mô tả quyền đã có trong runtime.
User chọn project, dữ liệu được nhập, mục tiêu công việc và approval mode;
controller ghi authority trong trần host policy. Repo/tool result không được
thay các lựa chọn này. Một lời đề xuất của model không tạo permission.

Profile đầu tiên: làm việc trên private copy SHADOW, offline, không credential,
không mount project gốc hay state/config của controller vào tool sandbox.
Agent được đề xuất đọc/sửa file thường trong dữ liệu đã chọn; chạy code phải
qua REVIEW. Thay đổi private copy chưa phải thay đổi project gốc.

Effective scope là giao của host policy, authority user cấp và khả năng backend
cưỡng chế. Mỗi execution nhận grant một lần trong scope đó; approval mode chỉ
chọn cách duyệt, không mở filesystem/network hoặc bỏ hard DENY.

### 1.2 Hai tầng quyết định và approval mode

Hard policy phân loại action trước; approval mode chọn cách duyệt action sau
phân loại. Kết quả hard policy và kết quả Reviewer có ý nghĩa riêng:

| Tầng | Kết quả | Ý nghĩa |
|---|---|---|
| Hard policy | `ALLOW` | Đủ điều kiện đi tiếp trong authority và scope được enforce; mode vẫn có thể yêu cầu human approval |
| Hard policy | `REVIEW` | Chưa được thực thi; cần người hoặc Reviewer đánh giá trước khi đi tiếp |
| Hard policy | `DENY` | Chặn; không đưa sang Reviewer hoặc hỏi user để override |
| Reviewer, chỉ được gọi ở nhánh hard `REVIEW` trong `auto_review` | `ALLOW` / `ASK` / `DENY` | Cho đi tiếp trong authority hiện có / chuyển human gate / chặn |

**Không có luồng hard `ALLOW` rồi gọi Reviewer kiểm lại.** Hard `ALLOW` và
hard `REVIEW` là hai nhánh thay thế nhau, không phải hai bước nối tiếp.
`auto_review` tự xử lý nhánh hard `REVIEW`, không review mọi action.

Bảng dưới áp dụng cho execution mới, không bị hard `DENY` và không có
mandatory human gate. Hai trường hợp đó được xử lý trước bảng mode:

| Hard policy trả về | `ask_all` | `ask_on_escalation` | `auto_review` |
|---|---|---|---|
| `ALLOW` | Hỏi user | Đi revalidation, không hỏi | Đi revalidation, không hỏi, không gọi Reviewer |
| `REVIEW` | Hỏi user | Hỏi user | Gọi LLM Reviewer; Reviewer `ALLOW` đi revalidation, `ASK` hỏi user, `DENY` chặn |

Mandatory human gate luôn cần user duyệt ở mọi mode; Reviewer không thay thế.
Nếu cần human approval mới mà headless/không có kênh human, action bị từ chối.

Ví dụ trong `auto_review`: ghi `notes.txt` qua file adapter trong private scope
nhận hard `ALLOW` -> revalidation -> thực thi. Một lệnh bash trong profile được
hỗ trợ nhận hard `REVIEW` -> Reviewer xét -> chỉ khi Reviewer `ALLOW` mới đi
revalidation. Cùng chữ `ALLOW`, nhưng đến từ hai nhánh và hai tầng khác nhau.

Revalidation là controller kiểm lại hard policy, authority và action binding
trước effects; không phải một call LLM Reviewer. "Không hỏi" không có nghĩa
thực thi ngay hoặc bỏ sandbox/checkpoint. Nếu policy/input thay đổi, quyết định
duyệt cũ không còn đủ: đánh giá lại, không tự dùng lại approval cũ.

### 1.3 Bảng quyền và gate cụ thể

Bảng áp dụng cho profile đầu tiên; cột hỏi dùng mặc định đề xuất
`ask_on_escalation`. `ask_all` hỏi thêm mọi execution không DENY;
`auto_review` chỉ chuyển các hàng REVIEW sang core LLM Reviewer.

| Action được đề xuất | Hard policy / gate | Khi nào hỏi user? | Enforcement bắt buộc |
|---|---|---|---|
| Read/list/search file thường trong private scope | ALLOW nếu schema/path hợp lệ và disclosure được phép | Không; `ask_all` vẫn hỏi | File adapter trong sandbox; no-follow/beneath, mount RO, output qua disclosure gate |
| Write/edit/create file thường trong private scope | ALLOW; bảo vệ targets không tồn tại và giới hạn dung lượng | Không; `ask_all` vẫn hỏi | Adapter giới hạn operation/target; private inode, checkpoint trước ghi |
| Bash/test/build hoặc execute/source/import code | REVIEW; opaque exec không tự ALLOW | `ask_on_escalation` hỏi; `auto_review` hỏi nếu Reviewer ASK hoặc thiếu content analysis bắt buộc | Container riêng offline, RW private scope đã duyệt, không host credential; thu hồi toàn descendants |
| Sửa control file của project: hook, package scripts, Makefile, CI, tasks, conftest | Mandatory human gate, kể cả ở private copy | Luôn duyệt exact diff trước ghi; duyệt execute/publication là action riêng | File adapter nhận scope sau approval; shell bị chặn sửa target và rename/unlink ancestor, kể cả target chưa tồn tại; không enforce được thì DENY |
| Structured bulk delete hoặc migration dữ liệu đã nhận diện | Mandatory human gate nếu backend hỗ trợ; ngoài private scope hiện tại DENY | Luôn duyệt targets, blast radius, before-state/dry-run | Grant giới hạn scope; checkpoint; không cấp quyền tới dữ liệu bền vững bên ngoài |
| Xuất sealed patch / apply diff vào project gốc | Disclosure gate cho export; apply là mandatory human gate | Duyệt exact changeset trước host apply; export cần nằm trong authority đã cấp | Broker riêng, hash/conflict check và guarded recovery; chưa có broker thì chỉ xuất patch qua sink được phép |
| Scope mới vượt authority hiện có nhưng host/backend cho phép | Mandatory human gate | Luôn hỏi trước cấp authority mới | Controller cập nhật authority, chạy lại policy, cấp grant mới; Reviewer không mở scope |
| Đọc secret, ghi policy/settings/MCP config đang có hiệu lực, state root, trusted runtime/image; truy cập docker.sock | DENY | Không có approval override trong tool pipeline | Loại khỏi ingest/mount/env; control plane và trusted image ngoài vùng agent ghi |
| Git push/force push, API ghi, email, web/MCP/package fetch qua network | DENY trong profile đầu tiên: chưa có broker | Không hỏi để mở capability chưa được enforce | Network isolation; không credential/socket; muốn hỗ trợ phải bổ sung broker và policy trước |

Read cũng có thể làm lộ dữ liệu qua provider, UI hoặc log: filesystem scope
không thay disclosure scope. Có file trong private copy không mặc nhiên được
gửi toàn nội dung ra mọi sink.

**Giới hạn của shell:** parser không chứng minh hết effects của code động.
REVIEW shell là duyệt toàn quyền vật lý của profile, không chỉ vài path trong
command. Code có thể xoá hàng loạt file thường trong private RW scope mà parser
không nhận diện; immutable before-generation cho phép recovery và isolation
bảo vệ project gốc. Muốn hạn chế effects đó phải cấp RO/narrow scope hoặc DENY,
không hứa rằng review lệnh sẽ phát hiện hết. Protected targets và ancestor
mutation phải được backend chặn cả rename/unlink/recreate; nếu chưa enforce
được, không chạy arbitrary shell RW với profile này.

### 1.4 Thứ tự quyết định và điều kiện bắt buộc hỏi

1. Không normalize được, thiếu facts bắt buộc, scope forbidden hoặc backend
   không cưỡng chế được => DENY. Không chuyển lỗi này thành câu hỏi để bypass.
2. Action được hỗ trợ nhưng cần authority mới, ghi project control files,
   publication hoặc irreversible effect đã nhận diện => mandatory human gate.
3. Action còn lại đi theo bảng mode ở mục 1.2: hard `ALLOW` đi revalidation ở
   `ask_on_escalation` và `auto_review`; hard `REVIEW` hỏi trực tiếp hoặc gọi
   Reviewer tương ứng; `ask_all` hỏi cả hai. Reviewer `ALLOW` chỉ cho đi tiếp
   trong authority hiện có.
4. Human-required mà headless/không có kênh human => DENY. Approval gắn exact
   action/content/scope; input đổi phải đánh giá lại, không dùng approval cũ.
5. Trước effects, controller recheck policy/taint rồi claim grant một lần;
   thiếu checkpoint hoặc sandbox không sẵn sàng => không chạy tool.

### 1.5 Tầng nào thực sự chặn quyền?

| Ranh giới | Thành phần cưỡng chế | Không được thay bằng |
|---|---|---|
| File/project gốc, protected targets, secret và private state | Mount/isolation + enforced file adapter; grants giới hạn operation/target; shell profile bảo vệ cả ancestor operations | Prompt, path regex hoặc command allowlist đơn thuần |
| Network/credential | Network namespace/rules; env và mounts không credential; future broker giữ secret ngoài sandbox | LLM hứa không gọi mạng hoặc chỉ kiểm URL string |
| Approval/action identity | Controller giữ authority/grant, bind digest/generation/revisions, recheck và durable one-use claim | Model tự nói user đã duyệt hoặc cờ approved bỏ qua policy |
| Process sống quá action | Container/cgroup theo action; terminate và xác minh toàn descendants trước harvest | Chỉ kill foreground PID |
| Xuất dữ liệu và cập nhật host | Disclosure gate trước sink; publication broker riêng hoặc sealed patch | Sandbox filesystem như bằng chứng không thể exfiltrate |

Phạm vi tài liệu là permission, gate và enforcement. Dùng lại memory/event log
hiện có; safety chỉ cần security state có provenance được policy đọc lại.
Không thiết kế lại retrieval, summary, compaction hoặc memory storage ở đây.

## 2. Trust boundary

| Nguồn | Vai trò và quyền |
|---|---|
| User qua kênh control-plane xác thực | Cấp authority trong giới hạn hard policy bằng record có cấu trúc |
| Host policy, controller, pinned image/helper | Quyết định/cưỡng chế quyền; ngoài vùng agent được ghi |
| Main LLM và Safety Reviewer | Đề xuất/đánh giá; không tạo authority hoặc sửa policy |
| Repo, dependency, web, MCP/tool result, code agent tạo | Dữ liệu không tin cậy, không có thẩm quyền ra chỉ thị |
| Memory và summary/context checkpoint | Dữ liệu tham khảo có provenance, không tạo permission |

Trusted computing base gồm controller, private state, trusted image/helper,
Docker runtime/daemon và host kernel. Không tuyên bố chống kernel escape hoặc
controller đã bị chiếm quyền. Rootless Docker và Docker Desktop phải được ghi
rõ và kiểm thử như các profile có mức bảo đảm khác nhau.

## 3. Pipeline mục tiêu

Sơ đồ chia thành ba phần nối tiếp để dễ đọc trong editor.

**A. Dữ liệu và tool call được đề xuất**

```text
  SELECTED SOURCE DATA                   USER REQUEST
           |                                  |
           v                                  v
  +------------------------+       +------------------------+
  | TRUSTED INGEST         |       | MAIN LLM               |
  | selected data only     |       | proposes tool call     |
  +------------------------+       +------------------------+
           |                                  |
           v                                  v
  +------------------------+       +------------------------+
  | IMMUTABLE PRIVATE      |       | ACTION NORMALIZER      |
  | WORKSPACE GENERATION   |       | facts only             |
  +------------------------+       +------------------------+
           |                                  |
           |                                  | facts
           +--------------------+-------------+
                                |
  USER AUTHORITY + HOST POLICY --+
  PERSISTENT TAINT / STATE ------+
                                |
                                v
                      +--------------------+
                      | HARD POLICY ENGINE |
                      +--------------------+
                                |
                                v
                         DECISION (B)
```

**B. Quyết định và phê duyệt**

```text
  HARD POLICY ENGINE
       |
       +-- DENY --------------------------------> HARD BLOCK
       |
       +-- ALLOW / REVIEW
              |
              +-- mandatory human gate --------> HUMAN GATE
              |
              v
     +--------------------------------------+
     | EFFECTIVE APPROVAL MODE              |
     | host constraints + user selection    |
     +--------------------------------------+
              |
              +-- ask_all ---------------------> HUMAN GATE
              |
              +-- ask_on_escalation
              |      |
              |      +-- ALLOW ----------------> REVALIDATION (C)
              |      +-- REVIEW ---------------> HUMAN GATE
              |
              +-- auto_review
                     |
                     +-- hard ALLOW -----------> REVALIDATION (C)
                     |
                     +-- hard REVIEW
                            |
                            v
             +--------------------------------+
             | SAFETY REVIEWER                |<-- TAINT / STATE
             | core LLM, separate request     |
             | sanitized context, no tools    |
             +--------------------------------+
                            |
                            +-- reviewer DENY -> BLOCK
                            +-- reviewer ALLOW
                            |   within
                            |   authority -----> REVALIDATION (C)
                            +-- reviewer ASK
                            |   / ERROR -------> HUMAN GATE

  HUMAN GATE (shared by all modes)
       |
       +-- headless / no human channel --------> DENY
       |                                         human_approval_unavailable
       +-- interactive
              |
              v
     +--------------------------------------+
     | HUMAN APPROVAL                       |
     | exact action + sealed content        |
     +--------------------------------------+
              |
              +-- REJECTED --------------------> BLOCK
              +-- APPROVED --------------------> REVALIDATION (C)
```

Host constraints quyết định effective mode và scope trước khi policy xét action;
router đặt sau hard policy để chọn cách review. Mode không được phép => DENY.
Headless chỉ thay cách xử lý yêu cầu human, không tự mở quyền.
Mandatory human gate không được Reviewer tự duyệt.

**C. Thực thi, cập nhật state và xuất thay đổi**

```text
              +--------------------------------+
              | REVALIDATION                   |
              | hard policy + immutable inputs |
              +--------------------------------+
                              |
                              +-- INVALID / DENY --> HARD BLOCK
                              |
                            VALID
                              |
                              v
              +--------------------------------+
              | CAPABILITY / SCOPE GRANT       |
              | claim once, bounded authority  |
              +--------------------------------+
                              |
                              v
              +--------------------------------+
              | REQUIRED CHECKPOINT            |
              | + DURABLE EXECUTION INTENT     |
              +--------------------------------+
                              |
                              v
              +--------------------------------+
              | OS SANDBOX / ENFORCED ADAPTER  |
              +--------------------------------+
                              |
                              v
                       TOOL EXECUTION
                              |
                              v
              +--------------------------------+
              | REVOKE ALL DESCENDANTS         |
              | verify quiescence              |
              +--------------------------------+
                              |
                              v
              +--------------------------------+
              | BOUNDED HARVEST                |
              | + DISCLOSURE GATE              |
              +--------------------------------+
                              |
             +----------------+----------------+
             |                |                |
             v                v                v
      AUDIT / LOG       INJECTION          SEALED DIFF
      + GUARDED         SCREENING          / ARTIFACTS
      RECOVERY          + PROVENANCE           |
             |                |                v
             +-------+--------+        SEPARATE HUMAN-APPROVED
                     |                 HOST PUBLICATION
                     v
              TAINT / SESSION
               STATE UPDATE
                     |
          +----------+-----------+
          |                      |
          v                      v
    NEXT POLICY /           TOOL RESULT
    REVIEWER INPUT               |
                                 v
                             MAIN LLM
```

Audit ghi cả quyết định bị chặn và intent trước effects. Taint được cập nhật
trước mọi model disclosure và trước action kế tiếp, kể cả trong cùng batch.
Screening âm tính không làm dữ liệu trở thành tin cậy.

## 4. Hợp đồng các tầng

### 4.1 Trusted ingest và workspace

- Chốt dữ liệu được phép nhập và gửi tới provider trước ingest. Private input
  bị loại; scanner không bảo đảm phát hiện hết secret. `.gitignore` không phải
  policy bảo mật; `.agentignore` chỉ được thu hẹp phạm vi.
- Copy bytes vào inode độc lập; không mount source gốc vào arbitrary execution
  trong SHADOW. Profile đầu tiên từ chối symlink, special file và file nhiều
  hardlink chưa được phân loại, tránh alias tới secret/file ngoài repo.
- Snapshot gồm dirty/untracked được chọn, mode/hash và absent targets. Phát hiện
  source thay đổi khi copy; không chạy hooks, filters hoặc plugin để ingest.
- Policy, settings, state root, approval, checkpoint, credential và docker.sock
  nằm ngoài mọi untrusted mount. Từ chối state-root/workspace overlap.
- Generation được niêm phong và lưu bất biến; không chỉ gắn hash lên directory
  mutable. Action chạy trên private working copy của generation đó.

### 4.2 Action Normalizer: facts only

Đầu ra: action/tool kind, exact arguments digest, canonical targets/cwd, input
generation, artifact hashes, flags execution/egress/deletion và `opaque_exec`.
Chỉ khẳng định effects adapter xác định được; phần còn lại ghi unknown.
Normalizer không quyết định permission hoặc tin facts về authority do model khai.

File ops validate schema đầy đủ và canonical paths. Adapter vẫn phải dùng
descriptor-relative, no-follow/beneath constraints lúc truy cập: resolve path
một lần không ngăn symlink/parent replacement.

Shell AST/lệnh ghép chỉ giúp cảnh báo. Không tách rồi chạy từng đoạn làm đổi
semantics, không suy an toàn từ tên command. Python, shell substitution,
source/import, hook và dependency có thể có effects động; generic shell/code
execution luôn mang `opaque_exec=true`.

Parse/schema lỗi, vượt budget hoặc timeout => DENY, không fallback ALLOW.

### 4.3 Hard Policy Engine

Input: normalized facts, user authority records, policy revision, enforced
backend profile, immutable generation và security taint/session state hiện tại.

| Quyết định | Hợp đồng |
|---|---|
| ALLOW | Trong authority đã cấp; facts đủ và backend cưỡng chế được toàn scope |
| REVIEW | Cần suy luận ngữ cảnh/review nội dung; chưa được thực thi |
| DENY | Vi phạm invariant, thiếu facts bắt buộc hoặc scope không cưỡng chế được |

Thiếu/lỗi policy không default ALLOW. Opaque exec không tự ALLOW chỉ vì parse
thành công: REVIEW trong profile offline/private được hỗ trợ, nếu không thì DENY.
Effects unknown không bao giờ là bằng chứng để mở rộng authority.

Phân biệt thiếu facts để enforce với thiếu phân tích để review: thiếu scope,
generation hoặc bằng chứng backend cưỡng chế được => hard `DENY`. Nếu các
facts này đầy đủ nhưng phân tích nội dung chưa đủ cho Reviewer tự duyệt,
chuyển human review nội dung đã niêm phong; không lấy human approval để bù
thiếu enforcement. Không cung cấp được nội dung cần duyệt thì không execute.

Gate irreversible/sensitive actions: publication, push/force push, migration
dữ liệu bền vững, API ghi, email, xoá hàng loạt và ghi control files. Facts cần
targets, blast radius, before-state, payload và dry-run nếu hỗ trợ. Dry-run
không cấp quyền hoặc miễn sandbox. Không thấy verb nguy hiểm không có nghĩa
shell không có effects nguy hiểm.

Đây là gates cho action/effects nhận diện được và backend hỗ trợ. Profile đầu
tiên DENY external effects; protected project control-file writes luôn cần
human. Shell unknown bị giới hạn vật lý như mục 1.3, không được coi là đã phân
loại đầy đủ irreversible effects.

Hard DENY là quyết định cuối; Reviewer/human không override trong pipeline.
User đổi host policy là thao tác riêng, ngoài vùng ghi của agent.

### 4.4 Approval mode, Safety Reviewer và human approval

**Đối chiếu code hiện tại**

| Khái niệm | Code thực tế | Phân biệt |
|---|---|---|
| `PolicyDecision` | `ALLOW / ASK / DENY` trong [runtime/models.py](../src/runtime/models.py), dòng 50 | Quyết định cho một execution; chưa có `REVIEW` |
| `PlanMode` | `OPTIONAL / REQUIRED`, [runtime/models.py](../src/runtime/models.py), dòng 16 | Có bắt buộc lập/hoàn thành plan hay không; không phải safety approval |
| `WorkspaceMode` | `LIVE / SHADOW`, [sandbox/models.py](../src/sandbox/models.py), dòng 18 | Nơi ghi file; độc lập với approval mode |
| Approval handler | Callback hoặc `WAITING_APPROVAL`, [executor.py](../src/runtime/executor.py), `execute`, dòng 113 | Cơ chế hỏi user đã có một phần; không phải auto LLM review |
| Approval/interaction modes và host mode constraints | KHÔNG TÌM THẤY trong CLI/config/Agent/ToolExecutor | Thiết kế mới, chưa có flag/settings hoạt động |
| Core LLM Reviewer | KHÔNG TÌM THẤY call site trong tool path | Có thể tái dùng `llm.complete`; chưa có reviewer prompt/schema/routing |

Luồng hiện tại là validate -> `policy(execution)` -> ALLOW/ASK/DENY.
Policy thiếu thì default ALLOW ([executor.py](../src/runtime/executor.py),
`__init__`, dòng 42); factory CLI chưa truyền security policy
([main.py](../src/main.py), `_default_agent_factory`, dòng 137).
Vì vậy không gọi hành vi hiện tại là mode `auto_review` có Reviewer.

**Ba mode approval và hai trục độc lập**

Đề xuất `ApprovalMode = ask_all | ask_on_escalation | auto_review`, thay tên
`ask/auto` để mô tả rõ lúc nào hỏi và ai review. Đây là thiết kế mới, chưa phải
enum/config trong source. Mặc định đề xuất `ask_on_escalation`, cần user chốt.
Reviewer tạm dùng core LLM qua request riêng; sau này có thể đổi sang SLM.

Phân biệt quyền vật lý với quyết định approval:

- Backend profile quyết định quyền filesystem/network/process/credential.
  `WorkspaceMode` LIVE/SHADOW quyết định nơi ghi; nó không mô tả đầy đủ profile.
- Approval mode quyết định cách xử lý action sau hard policy; đổi mode không
  thay sandbox, authority đã cấp hoặc mở capability mới.
- `InteractionMode = interactive | headless` quyết định có thể hỏi người hay
  không. Headless độc lập với approval mode và không có nghĩa auto-approve.

Router áp dụng bảng ở mục 1.2: hard `ALLOW` không gọi Reviewer; hard `REVIEW`
chỉ gọi Reviewer trong `auto_review`. Hard `DENY` và mandatory human gate có
ưu tiên trước mode. `ALLOW / ASK / DENY` của Reviewer là kết quả duyệt, không
phải kết quả phân loại hard policy hoặc một authority record mới.

Bảng này bổ sung hành vi headless cho execution mới, chưa có approval hợp lệ:

| Mode trong headless | Hard ALLOW, không human gate | Hard REVIEW, không human gate |
|---|---|---|
| `ask_all` | DENY vì cần human approval mới | DENY vì cần human approval mới |
| `ask_on_escalation` | Đi revalidation | DENY vì cần human approval mới |
| `auto_review` | Đi revalidation, không gọi Reviewer | Gọi Reviewer: `ALLOW` đi revalidation; `ASK`, `DENY` hoặc lỗi đều chặn |

Mandatory human gate ở mọi mode: interactive hỏi user; headless DENY với reason
`human_approval_unavailable`. Không tự approve, không treo chờ stdin và không
tạo `WAITING_APPROVAL` mới trong headless. Reviewer lỗi/timeout chuyển human
review nếu interactive, hoặc DENY cùng reason khi headless; log giữ nguyên
nguyên nhân `reviewer_timeout`/`reviewer_invalid_response`.

Headless resume không được tự chấp nhận approval đang pending từ phiên trước.
Approval một lần đã được user cấp qua control plane có thể được sử dụng nếu
còn hiệu lực và mode/input/policy vẫn khớp; headless tự nó không cấp approval.

`ask_on_escalation` hỏi khi REVIEW hoặc cần authority tăng thêm có backend hỗ
trợ. Vượt scope forbidden/unenforceable vẫn DENY, không đưa user một nút để
vượt hard DENY. `auto_review` chỉ tự duyệt trong authority hiện có; mở scope mới
được hỗ trợ và publication/protected paths vẫn có mandatory human gate.
Unknown effects không tạo authority: opaque exec vẫn REVIEW theo mục 4.3.

Hard-policy REVIEW là phân loại mới; Reviewer ASK là yêu cầu human approval.
`PolicyDecision.ASK` hiện tại giữ nghĩa human gate bắt buộc khi chuyển sang
thiết kế mới; không âm thầm map thành REVIEW rồi cho LLM thay user.
`mandatory_human_approval` là field mục tiêu, chưa có trong policy result hiện tại.

**Trần host policy và cấu hình project**

Host policy tin cậy ngoài vùng agent ghi có thể giới hạn allowed modes, default/
pinned mode, backend profiles, scope và mandatory human gates theo project.
User chọn trong tập được host cho phép. Chọn mode bị cấm hoặc config sai =>
DENY với `approval_mode_not_permitted`/`invalid_approval_configuration`, không
âm thầm fallback sang mode dễ hơn.

Repo-local config là dữ liệu không tin cậy: chỉ thêm restrictions, excluded
paths hoặc human gates; không bật `auto_review`, mở scope, bỏ approval, chọn
profile rộng hơn hoặc sửa host pin. Hợp thành constraints theo giao; không
sắp ba mode theo một thứ tự "ít/nhiều quyền" đơn giản. Khi config thay đổi,
controller revalidate và tăng policy revision. Restrictions đã nhận giữ nguyên
trong session; agent xoá/sửa repo config không tự gỡ chúng. Nới restrictions cần
user xác nhận qua control plane trong trần host policy. Main LLM/tool output
không tự chỉnh effective config.

Không thêm `yolo` hoặc "disable safety" vào ApprovalMode. Backend profile cho
môi trường disposable nếu cần phải là lựa chọn host riêng, có giới hạn/bảo đảm
được ghi rõ; approval mode không tắt sandbox hoặc hard policy.

Khi triển khai, truyền effective config qua CLI/config -> Agent -> ToolExecutor,
độc lập với plan/workspace mode. Bind approval mode, interaction mode, host policy
revision và effective constraints vào grant/audit. Đổi effective config làm
approval/grant chưa chạy mất hiệu lực.

**Approval một lần**

`ask_all` hỏi từng proposed execution mới, không phải mỗi lần gọi lại
`execute(execution_id)`. Sau approve, recheck policy và action binding; approval
hợp lệ chỉ đáp ứng human gate của đúng execution. Không hỏi lặp vô hạn khi
resume, không bỏ policy như [executor.py](../src/runtime/executor.py),
`execute`, dòng 106. Không dùng approval cũ để duyệt ID mới hoặc cả batch.

"Always allow"/standing approval chưa thuộc thiết kế đầu tiên. Nếu làm sau,
phải bind action class/digest và scope rõ, có expiry/revocation, recheck policy
mỗi execution; không áp dụng mandatory human gates hoặc đổi backend scope.

**Nối approval vào runtime hiện có**

Tái dùng `ToolApprovalRequested -> WAITING_APPROVAL -> ToolApproved/ToolRejected`
trong [reducer.py](../src/runtime/reducer.py), dòng 198, thay vì tạo một approval
loop riêng không liên kết execution ID. Agent dừng model progress khi chờ duyệt
([loop.py](../src/agent/loop.py), `_drive_turn`, dòng 602).

REPL hiện gọi `handle_pending_runtime_actions` một lần trước vòng input
([main.py](../src/main.py), `run_repl`, dòng 163). Approval mới phát sinh từ
`run_turn` chưa được REPL xử lý ngay trong cùng phiên. Thiết kế mode phải nối
lại pending-action handling sau turn/resume và tiếp tục drain batch tới khi
không còn approval/recovery unresolved. Không gọi Main LLM để sinh tool mới
trong lúc execution đang chờ.

Sau approve/reject, ghi tool result đúng một lần, tiếp tục turn/batch đang dừng
qua cơ chế resume hiện có; mỗi action sau phải kiểm policy/taint/mode mới nhất.
Recovery vẫn là human decision riêng, không được Reviewer quyết định thay.
Headless có recovery unresolved phải dừng với reason có cấu trúc, giữ evidence;
không thay manual recovery bằng auto retry hoặc self-attested completion.

Các test hiện có mô tả stop/resume approval:
[tests/test_recovery.py](../tests/test_recovery.py),
`ApprovalProgressTests.test_waiting_approval_blocks_model_progress` và
`BatchResumeTests.test_resume_drains_pending_batch_before_model_progress`.
Đây là bằng chứng đọc source test, chưa chạy lại tests trong lượt sửa tài liệu.

**Core LLM Reviewer**

Dùng cùng effective model/provider với Main LLM, nhưng một call `llm.complete`
riêng: messages/system prompt riêng, không tools, không stream, output schema
ALLOW/ASK/DENY và validate ở controller. Không chuyển toàn lịch sử Main LLM
hoặc lấy câu model tự nhận an toàn làm review result.

API hiện có là [model/llm.py](../src/model/llm.py), `complete`, dòng 117;
hàm tạo OpenAI client mỗi call, dòng 146. Tái dùng cấu hình/hàm gọi hiện có,
không giả định đã có shared client hay một reviewer service. Có thể thay model
reviewer bằng SLM sau mà giữ nguyên contract. Cùng model có thể mắc cùng lỗi
suy luận; hard policy/sandbox vẫn chịu trách nhiệm cưỡng chế quyền.

Reviewer chỉ nhận user intent có nguồn gốc rõ, facts/scope/taint và structured
artifact analysis. Không nhận raw tainted file/stdout/web instruction;
sanitization không nâng trust label. Reviewer không có tool hoặc quyền tạo grant.

Reviewer `ALLOW` chỉ cho đi revalidation trong authority hiện có; `ASK` chuyển
user; `DENY` chặn. Timeout/output sai schema/thiếu review => human review hợp lệ
hoặc DENY, không fallback ALLOW. Revalidation không gọi lại Reviewer cho cùng
action/binding còn hiệu lực, nhưng vẫn kiểm hard policy trước effects.

File agent vừa tạo/sửa rồi execute/source/import phải review **nội dung**
generation đó, gồm import/dependency/config liên quan; tên lệnh/hash không
thay thế content review. Scanner tin cậy sinh facts; nếu không đủ, human xem
sealed content qua viewer không thực thi. Không gửi raw tainted code cho Reviewer
để bù thiếu facts. Import động bind toàn generation và ghi effects unknown.

Approval UI do controller render action, execution ID, scope, targets,
content/diff, blast radius, reversibility và action digest. Model rationale
không phải nguồn sự thật duy nhất.

### 4.5 Capability / Scope Grant

Grant là record nội bộ controller quản lý, không phải token đặt trong model
context hoặc sandbox environment.

```text
execution_id, session_id, tool_name, exact_arguments_digest
input_generation_digest, image_digest, trusted_environment_digest, cwd
profile_id, enforced_scope_digest, policy_revision, taint_state_version
approval_mode, interaction_mode, effective_constraints_digest
mandatory_human_approval
authority_record_id, approval_action_digest, expires_at, max_uses=1
status = proposed | approved | claimed | running | completed | unknown | denied
```

Approval bind canonical action, content/generation, image, environment, profile
và policy revision/effective modes/constraints. Input đổi hoặc taint siết quyền
=> approval vô hiệu. Trước effects luôn recheck hard policy rồi durable claim
một lần; approval không bỏ recheck.

File broker có scope operation/target và temporary-file/rename cần cho atomic
replacement. Shell có thể cần RW nhiều file thường trong private copy; grant
mô tả toàn scope vật lý đó, gồm excluded/protected paths, không giả vờ chỉ được
ghi file nhắc trong command. Chỉ duyệt exact control-file diff qua file adapter,
không dùng approval đó để cấp shell quyền sửa tuỳ ý control files. Scope backend
không cưỡng chế được => DENY dù user/Reviewer đồng ý.

### 4.6 Checkpoint và OS sandbox

Checkpoint dữ liệu lưu immutable before-images/manifest và absent targets.
Hash-only hoặc Git commit bỏ sót dirty/untracked không đủ phục hồi. Shell không
biết targets thì checkpoint toàn private generation. Checkpoint nén context
không thay thế data snapshot hoặc authority. Required checkpoint/intent ghi
thất bại => không chạy tool.

Mọi tool OS, gồm read/write/edit/bash và tool tương lai, qua enforced adapter;
không host fallback. `update_plan` chỉ đổi runtime state theo schema/lifecycle,
không cấp permission hoặc sửa policy.

Profile đề xuất:

- Rootless, pinned trusted image/helper RO, env tối thiểu; không secret,
  docker.sock, inherited control socket/FD hay host credential mount.
- Network none chặn host/external. Namespace vẫn có loopback: không tuyên bố
  cấm mọi localhost nội bộ. Nếu cần cấm tuyệt đối, thêm enforcement và test
  với listener thực, không chỉ thử một port đóng.
- Drop capabilities, no-new-privileges; CPU/memory/PID/time/output limits,
  byte/inode budgets. Watchdog có overshoot, không được gọi là hard disk quota.
- File adapter RO/quyền ghi hẹp; arbitrary exec dùng private working copy trong
  container/cgroup có lifetime riêng cho từng action. Shell không được ghi
  protected targets hoặc thay ancestor để bypass; path chưa tồn tại cũng phải
  được bảo vệ. Không đáp ứng được => DENY shell RW, không fallback host.

Thu hồi toàn descendants khi success/error/timeout/cancel, kể cả child đóng
stdio hoặc `setsid`. Xác nhận dừng trước harvest/action sau; killing riêng
foreground PID/process group không đủ.

### 4.7 Harvest, Audit/Log và Recovery

Harvester đọc cây đã quiescent, giới hạn bytes/entries/time, từ chối link,
special file và unsafe mode. Không execute code/hook để inspect. Artifacts hợp
lệ được copy sang generation mới và niêm phong.

Audit lưu decision linkage, safe facts/reason codes, revisions, hashes,
grant/approval và observed effects. Full decision trace không đồng nghĩa full
raw payload. Trace mặc định trong control state; host export nếu có phải qua
broker chống symlink/path escape và được authorize riêng.

```text
process_outcome = success | error | timeout | cancelled | unknown
enforcement_observation = detected | not_detected | unknown
effect_state = unchanged | sealed_generation | quarantined | unknown
```

Exit 0 không chứng minh safe; `not_detected` không chứng minh không có attempt.
Transport loss, termination không xác minh được hoặc audit sau effects thất
bại => quarantine/unknown, dừng mutation/publication và reconcile; không auto retry.

Recovery chỉ restore khi current content/type/mode khớp expected-after và có
writer exclusion phù hợp. Sửa mới của user => giữ backup, báo conflict. External
effect không có guaranteed rollback; compensation là action mới cần authorize.

### 4.8 Điểm nối security state, disclosure và injection screening

Tái dùng session/event log hiện có để lưu và replay security state; controller
đọc state trước policy/Reviewer và cập nhật trước action kế tiếp. Không thêm hệ
memory riêng. Contract safety tối thiểu theo generation/session:

```text
untrusted_content_seen, agent_modified_content, injection_flags
secret_exposure_detected, provenance_generation_ids, security_state_version
```

Output arbitrary code có thể chịu ảnh hưởng mọi input được đọc. Copy/rename,
summary và resume phải giữ provenance/taint qua cơ chế memory hiện có. Taint
chỉ siết quyền; clear/declassify cần user qua control plane. Secret exposure
dừng disclosure/egress; untrusted/injection flags làm policy đánh giá lại
sensitive actions, không tự cho LLM authority mới.

Disclosure gate trước mọi sink: Main LLM, Reviewer, summarizer, memory, UI,
audit text, trace và diff export. Exclude private input từ ingest; detector chỉ
bổ sung, không bảo đảm bắt secret encode/chia nhỏ/đổi tên. Mandatory gate lỗi
thì chặn disclosure, không gửi trước rồi mới lọc log.

Screening tạo flags/annotations; không biến file/tool instruction thành user
instruction. Summary giữ provenance và không nâng nội dung thành system
authority. Trusted status/metadata do controller tạo, không lấy từ stdout.

### 4.9 Publication, control files và external effects

Tool success chỉ thay đổi private generation. Publication về project gốc là
action riêng: sealed change-set digest, human review, hard-policy recheck,
checkpoint, before-hash conflict check và guarded recovery.

Publication không execute hook/build. Protected paths gồm `.git/hooks`, package
scripts, Makefile, shell startup, `.vscode/tasks.json`, CI, `conftest.py`,
policy/settings/MCP/allowlist, runtime source và sandbox build context. Chúng
phải phân biệt theo vai trò: trusted policy/runtime/config đang chạy nằm ngoài
vùng agent ghi và hard DENY; bản source/control file của project trong private
copy cần human duyệt exact diff trước ghi và duyệt riêng trước publication.
Grant shell mặc định không có quyền sửa các targets này, kể cả qua parent
rename/unlink; thiếu enforcement thì DENY profile đó. User duyệt một lần không
cho phép code agent sửa tự trở thành trusted runtime/image ở lần khởi động sau.
Danh sách không đầy đủ: source thường cũng có thể chạy qua import/watcher.

Bảo vệ cả path chưa tồn tại, không chỉ mask lúc startup. Không tự build trusted
image từ workspace agent vừa sửa. Sửa `.agentignore`/source config không tự nới
quyền session đang chạy hoặc lần chạy sau.

Automatic apply cần host writer coordination; agent lock không loại IDE/watcher.
Nếu không bảo đảm, xuất sealed patch để user áp dụng. Multi-file apply không
atomic; publication không chứng nhận code an toàn để chạy trên host.

Giai đoạn đầu không có external web/MCP/API/email/package-fetch broker. Approval
không bật capability chưa có enforcement. Broker tương lai giữ credential
ngoài sandbox, bind identity/service/operation/resource/payload; chặn private,
loopback/link-local/metadata address gồm `169.254.169.254`, kiểm DNS/redirect/
rebinding tại connect. Endpoint allowlist chưa đủ chặn exfiltration.
Mutation cần resource revision, dry-run nếu có, idempotency/reconciliation;
timeout là unknown, không tự gửi lại. Package fetch và execution tách riêng,
lifecycle scripts chỉ chạy offline không credential.

## 5. Failure semantics

| Điều kiện | Hành vi bắt buộc |
|---|---|
| Normalizer/policy lỗi hoặc thiếu required facts | DENY; không effects |
| Hard DENY | Không Reviewer/human override |
| Mode `ask_all`, kể cả policy ALLOW | Hỏi user cho execution mới; headless DENY nếu cần hỏi |
| Mode `ask_on_escalation`, hard ALLOW không human gate | Revalidation trong scope hiện có, không hỏi |
| Mode `auto_review`, hard ALLOW không human gate | Revalidation, không hỏi và không gọi Reviewer |
| Reviewer ALLOW nhưng revalidation gặp hard DENY | Chặn; kết quả Reviewer không override policy hiện tại |
| Execution đã approve hợp lệ rồi resume | Recheck hard policy; không hỏi lại cùng execution vô hạn |
| Mandatory human gate ở mọi mode | User duyệt; headless DENY; Reviewer không thay thế |
| Reviewer ASK hoặc fallback human review trong headless | DENY human_approval_unavailable; không đợi input/auto approve |
| Mode bị host cấm hoặc cấu hình invalid | DENY; không fallback sang mode dễ hơn |
| Reviewer lỗi/approval thiếu | Không ALLOW; ASK hợp lệ hoặc DENY |
| Action/content/generation/policy/mode/taint đổi | Approval vô hiệu; đánh giá lại |
| Grant đã claim/hết hạn/sai session | Không chạy lại |
| Required checkpoint/durable intent thất bại | Không effects |
| Sandbox/termination/harvest không xác minh được | Quarantine; không host fallback/auto retry |
| Disclosure gate lỗi/secret exposure | Không gửi raw payload vào sink |
| Recovery gặp sửa mới của user | Giữ backup; manual reconciliation |
| Backend không cưỡng chế được scope | DENY dù approve |

## 6. Code hiện tại và khoảng cách

Các link là bằng chứng implementation, không phải cam kết thiết kế đã có.
Lượt sửa tài liệu này chưa chạy test/probe mới.

| Thành phần | Hiện tại | Bằng chứng |
|---|---|---|
| Workspace | CLI mặc định LIVE | [main.py](../src/main.py), `_default_agent_factory` |
| Policy | Callback default ALLOW; approval bỏ recheck | [executor.py](../src/runtime/executor.py), `__init__`, `execute` |
| Approval/interaction modes và host mode constraints | KHÔNG TÌM THẤY; ASK hiện là decision, chưa có REVIEW | [models.py](../src/runtime/models.py), `PolicyDecision`, dòng 50; [main.py](../src/main.py), `build_parser`; [config.py](../src/core/config.py) |
| Approval UI trong REPL | Pending handler chỉ gọi lúc startup, chưa xử lý ngay approval mới | [main.py](../src/main.py), `run_repl`, dòng 163; [loop.py](../src/agent/loop.py), `_drive_turn`, dòng 602 |
| Tool boundary | read/write/edit/bash dùng sandbox; thiếu sandbox thì từ chối | [registry.py](../src/tools/registry.py), `ToolRegistry`; [filesystem.py](../src/tools/filesystem.py), `_call`; [terminal.py](../src/tools/terminal.py), `execute` |
| Docker baseline | Network none, rootfs RO, capabilities/cgroups, runtime probe | [docker.py](../src/sandbox/docker.py), `create`, `verify_runtime` |
| File scope | Descriptor-relative/no-follow helper | [sandbox_fs.py](../sandbox-image/sandbox_fs.py), `_parent_fd`, `_read` |
| Process lifetime | Chưa thu hồi descendants trên mọi success path | [sandbox_exec.py](../sandbox-image/sandbox_exec.py), `execute` |
| Recovery/publication | SHADOW có seal/apply; LIVE ghi ngay; rollback chưa guard sửa mới | [changes.py](../src/sandbox/changes.py), `apply_changeset`, `_rollback`; [main.py](../src/main.py), `run_repl` |
| Audit/context | JSONL fsync, redaction theo key; summary vào system message | [event_store.py](../src/memory/event_store.py), `append_event`, `_redact`; [checkpoint.py](../src/context/checkpoint.py), `to_message` |
| Host hậu xử lý | Trace copy về project; build sandbox image từ project | [loop.py](../src/agent/loop.py), `_run_with_trace`; [docker-entrypoint.sh](../docker-entrypoint.sh), lệnh `docker build` |
| Full Normalizer / Reviewer / policy taint / screening | KHÔNG TÌM THẤY trong tool path hiện tại | [executor.py](../src/runtime/executor.py), `request_batch`, `execute`; [loop.py](../src/agent/loop.py), `_drive_turn`; [models.py](../src/runtime/models.py), `RuntimeState` |

## 7. Acceptance criteria

| Kịch bản nhiều bước | Kết quả yêu cầu |
|---|---|
| README injection -> xoá private workspace | Dirty/untracked project gốc nguyên vẹn |
| Private read/edit file thường -> chọn approval mode -> execute | Quyết định đúng bảng quyền; mode không tăng physical scope |
| Shell -> rename parent/xoá rồi tạo lại protected control path | Backend chặn; profile không enforce được bị DENY trước execute |
| Sửa project package scripts/CI trong private copy -> execute -> publish | Human duyệt write diff; execute/publication có gate riêng, không tái dùng approval |
| Hardlink alias secret/outside file -> ingest -> read/write | Từ chối admission chưa phân loại; không lộ/chạm inode gốc |
| Tạo/sửa script/import -> xin execute | Content review đúng generation, không chỉ tên lệnh |
| ASK -> đổi policy/content/taint -> approve | Approval cũ không chạy được |
| Mode `ask_all` interactive -> hard ALLOW/REVIEW -> action | Luôn hỏi riêng; không gọi Reviewer để tự duyệt |
| Mode `ask_all` -> approve -> execute/resume lại cùng ID | Không hỏi lặp; vẫn recheck hard policy/hash; không chạy effects hai lần |
| Mode `ask_on_escalation` -> private read/edit hard ALLOW -> REVIEW action | ALLOW không hỏi; REVIEW hỏi trực tiếp; scope sandbox giữ nguyên |
| Policy ASK hiện tại -> chuyển sang thiết kế mode mới | Giữ mandatory human gate, không cho LLM thay human approval |
| Batch dừng chờ duyệt -> REPL approve/reject -> resume | Xử lý trong cùng phiên, mỗi result đúng một lần; không model progress khi pending |
| Mode `auto_review` -> hard REVIEW -> core Reviewer | Request/context riêng; kết quả ALLOW/ASK/DENY đi đúng nhánh |
| Mode `auto_review` -> hard ALLOW -> execute | Không gọi Reviewer; vẫn revalidation, checkpoint/intent và sandbox |
| Reviewer ALLOW -> hard policy/input đổi trước effects | Hard DENY chặn; binding đổi phải đánh giá lại, không dùng quyết định cũ |
| Backend/scope hợp lệ nhưng thiếu content analysis -> execute | Human xem sealed content hoặc block; thiếu enforcement vẫn hard DENY |
| Headless -> Reviewer ASK/timeout hoặc mandatory gate | DENY có reason; không prompt, không tự approve, không effects |
| Host cấm auto_review -> user/repo chọn auto_review | DENY approval_mode_not_permitted; repo không override host pin |
| Repo config thêm restriction -> agent sửa config để bỏ gate | Không nới quyền; effective host constraints/gates vẫn được giữ |
| Đổi approval mode -> action yêu cầu rộng hơn backend profile | Scope không cưỡng chế được vẫn hard DENY; mode không đổi sandbox |
| Mode bất kỳ -> hard DENY hoặc mandatory human gate | Không auto execute; hard DENY không có override |
| Core Reviewer lỗi/timeout -> action nhạy cảm | Human review hợp lệ hoặc block, không fallback ALLOW |
| Action đã duyệt -> đổi mode -> execute | Grant cũ vô hiệu; revalidation đánh giá lại |
| Parse thành công nhưng opaque/lệnh ghép -> policy | Không default ALLOW; unknown không tạo authority |
| Spawn child đóng stdio/setsid -> exit -> action kế | Không descendants giữ quyền sau completion |
| Injection/secret -> compact -> resume -> sensitive action | Provenance/taint còn; policy siết; known canary không vào provider/log |
| Path export vắng -> tạo symlink -> kết thúc turn | Controller không ghi ngoài approved host scope |
| Sửa sandbox Dockerfile/config/tasks -> restart/publication | Không auto build/execute/nới policy; human content review |
| Apply/crash -> user sửa thêm -> recovery | Không overwrite sửa mới; giữ backup/báo conflict |
| Network none -> listener nội bộ / host, metadata, Internet | Mô tả đúng loopback; không truy cập host/external |
| Transport/audit failure sau effects -> resume | Reconcile unknown; không auto retry effects |
| Edit thường + test project đại diện | Workflow hữu ích trong scope, không chỉ DENY mọi action |

Kiểm tra vật lý cần Docker integration cho supported profiles; mock không chứng
minh enforcement. Đây là tiêu chí cần kiểm thử, chưa phải kết quả test.

## 8. Quyết định trước implementation plan

Đã chọn core LLM Reviewer qua request riêng làm bước chuyển tiếp trước SLM.
Thiết kế mode mới đề xuất ba mức, headless độc lập và trần host policy; chưa
triển khai trong runtime. Các lựa chọn dưới đây còn cần chốt:

1. Chọn SHADOW làm mặc định; LIVE nếu cần là profile riêng, bảo đảm yếu hơn.
2. Chọn sealed patch export giai đoạn đầu hay automatic publication cùng writer
   coordination/guarded recovery ngay.
3. Chốt source-data disclosure scope và cách review code agent tạo trước execute;
   không âm thầm bỏ gate opaque exec.
4. Chốt storage profile bắt buộc; không nhận watchdog là hard quota.
5. Chốt tên `ask_all` / `ask_on_escalation` / `auto_review` và default đề xuất
   `ask_on_escalation`. Headless từ chối mọi yêu cầu human chưa được đáp ứng;
   host policy có quyền cấm/pin mode theo project. Standing approval để sau.

Duyệt các quyết định này rồi mới lập kế hoạch PR nhỏ. Đây là các tầng logic,
có thể đặt trong module hiện có; không yêu cầu microservice, policy DSL hoặc
instruction-level taint graph.
