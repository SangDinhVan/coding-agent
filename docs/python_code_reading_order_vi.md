# Thứ tự đọc và cách hiểu toàn bộ code Python

Tài liệu này dành cho người muốn hiểu repository theo từng dòng nhưng chưa biết
nên bắt đầu từ đâu. Mục tiêu không phải là đọc hết file theo thứ tự bảng chữ cái.
Mục tiêu là xây đúng mental model, sau đó quay lại giải thích được từng dòng:

- dòng này nhận dữ liệu gì;
- tạo hoặc biến đổi dữ liệu nào;
- có thay đổi state hay tạo side effect không;
- lỗi có thể xuất hiện ở đâu;
- test nào chứng minh hành vi đó.

Phạm vi chính là `src/` và hai helper Python trong `sandbox-image/`. Thư mục
`tests/` được đọc sau source tương ứng để kiểm tra lại cách hiểu.

---

## 1. Đừng bắt đầu bằng `main.py`

`main.py` là nơi ghép gần như toàn bộ hệ thống. Nếu đọc nó đầu tiên, bạn sẽ gặp
`Agent`, `EventStore`, `SandboxSession`, `DockerBackend`, `RuntimeState` và
`ToolRegistry` trước khi biết từng đối tượng có trách nhiệm gì.

Thứ tự dễ hiểu hơn là:

```text
cấu hình và kiểu dữ liệu
        ↓
contract của tool và model
        ↓
journal và runtime state
        ↓
sandbox
        ↓
agent loop
        ↓
main.py
```

Bạn sẽ đọc `main.py` gần cuối. Lúc đó mỗi dòng import và mỗi object được khởi tạo
đều đã có ý nghĩa.

---

## 2. Ba lượt đọc cho mỗi file

Đừng cố hiểu mọi chi tiết ngay lượt đầu. Với mỗi file, đọc ba lượt.

### Lượt 1: Nhìn cấu trúc

Chỉ trả lời năm câu hỏi:

1. File import module nào?
2. File định nghĩa constant, enum, dataclass, class hay function nào?
3. API công khai của file là gì?
4. File này được module nào gọi?
5. File này có chạm filesystem, network, subprocess hoặc journal không?

Ở lượt này, chưa cần hiểu từng nhánh `if`.

### Lượt 2: Theo dữ liệu

Chọn một function hoặc method và lần theo:

```text
input → validate → transform → side effect → output/error
```

Ví dụ với `llm.complete()`:

```text
messages/tools/config
        ↓
tạo OpenAI client options
        ↓
tạo request payload
        ↓
Chat Completions API
        ↓
ChatCompletion hoặc stream chunks
```

### Lượt 3: Giải thích từng dòng

Với mỗi dòng, tự nói thành tiếng hoặc ghi chú theo mẫu:

```text
Cú pháp:       Đây là import / assignment / call / branch / loop gì?
Dữ liệu vào:   Giá trị đến từ đâu?
Dữ liệu ra:    Dòng này tạo giá trị nào?
State:         Có thay đổi object hoặc runtime state không?
Side effect:   Có ghi file, gọi API, chạy Docker hay append event không?
Failure:       Exception hoặc kết quả lỗi nào có thể xảy ra?
Contract:      Caller đang trông đợi điều gì từ dòng này?
```

Nếu chưa trả lời được một mục, dùng “Go to Definition” hoặc tìm caller bằng:

```bash
rg -n "ten_ham|TenClass" src tests
```

---

## 3. Các cú pháp Python cần nhận ra trong repository

Bạn không cần học toàn bộ Python trước. Chỉ cần nhận ra các mẫu xuất hiện nhiều
trong project này.

### 3.1. Import

```python
from openai import OpenAI
from runtime.models import ReplayPolicy
```

Dòng import cho biết dependency của file. Khi đọc một file, import nội bộ như
`runtime.models` quan trọng hơn import standard library như `json` hoặc `os`.

### 3.2. Type hint và union

```python
def get_context_window(model: str = None) -> int:
def build_user_content(text: str, image_paths: list[str] | None = None):
```

- `model: str`: caller nên truyền string.
- `-> int`: function dự kiến trả integer.
- `list[str] | None`: có thể là danh sách string hoặc `None`.
- Type hint giúp đọc contract nhưng không tự validate dữ liệu lúc runtime.

### 3.3. Dataclass

```python
@dataclass(frozen=True)
class ToolResult:
    raw: str
    compact: str
    success: bool
```

`@dataclass` tự tạo constructor và các method dữ liệu cơ bản. `frozen=True`
nghĩa là object không nên bị sửa sau khi tạo. Khi thấy `field(default_factory=...)`,
hãy hiểu mỗi instance sẽ có một object mặc định riêng.

### 3.4. Enum

```python
class ReplayPolicy(str, Enum):
    REPLAY_SAFE = "replay_safe"
```

Enum giới hạn state vào một tập giá trị hợp lệ. Hãy tìm mọi nơi enum được so
sánh để hiểu state machine.

### 3.5. Kế thừa và abstract method

```python
class BaseTool(ABC):
    @abstractmethod
    def execute(self, **kwargs):
        raise NotImplementedError
```

`BaseTool` đặt contract chung. Các tool con kế thừa và implement `execute()`.
Đọc base class trước, sau đó mới đọc `ReadTool`, `WriteTool`, `EditTool` và
`BashTool`.

### 3.6. `*args`, `**kwargs` và keyword-only argument

```python
def append_event(self, event_type, aggregate_type, aggregate_id, payload, *, turn_id=None):
```

Các tham số sau `*` phải được truyền bằng tên. `**kwargs` thu các keyword chưa
được khai báo vào dictionary. Khi thấy `kwargs.pop(...)`, code đang lấy một
option ra khỏi dictionary trước khi chuyển phần còn lại cho dependency khác.

### 3.7. Comprehension và generator

```python
missing = [field for field in required if field not in kwargs]
```

Đọc từ phải sang trái:

1. lặp qua `required`;
2. giữ field chưa có trong `kwargs`;
3. tạo list `missing`.

### 3.8. Context manager

```python
with EventStore(...) as store:
    ...
```

`with` bảo đảm phần cleanup trong `__exit__()` được chạy. Trong repository này,
context manager và lock thường đánh dấu ranh giới an toàn cho tài nguyên.

### 3.9. Exception

```python
try:
    ...
except ValueError as error:
    ...
```

Khi đọc `except`, đừng chỉ hỏi “bắt lỗi gì”. Hãy hỏi lỗi được chuyển thành:

- exception khác;
- `ToolResult(success=False)`;
- event thất bại;
- hay chỉ một log cảnh báo.

### 3.10. `Path` và đường dẫn tương đối

Project dùng `pathlib.Path` cho đường dẫn trên control plane và
`PurePosixPath` cho đường dẫn logic bên trong sandbox. Đừng mặc định mọi string
path đều được phép trỏ ra host filesystem.

---

## 4. Bản đồ thứ tự đọc

| Bước | File hoặc nhóm file | Mục tiêu |
|---:|---|---|
| 0 | `pyproject.toml`, `.env.example`, `compose.yaml` | Biết chương trình cần gì và chạy ở đâu |
| 1 | `core/config.py`, `core/paths.py` | Hiểu config và vị trí dữ liệu trên disk |
| 2 | `runtime/machine.py`, `agent/state.py` | Làm quen class nhỏ và state đơn giản |
| 3 | `runtime/models.py`, `sandbox/models.py` | Học toàn bộ từ vựng dữ liệu |
| 4 | `tools/base.py`, các tool và registry | Hiểu contract model gọi tool |
| 5 | `model/llm.py`, `context/compactor.py`, `memory/manager.py` | Hiểu biên model và context |
| 6 | `memory/event_store.py`, `runtime/reducer.py` | Hiểu journal và cách dựng runtime state |
| 7 | `runtime/executor.py` | Hiểu vòng đời một tool call |
| 8 | `sandbox-image/*.py`, `sandbox/*.py` | Hiểu nơi side effect thật sự xảy ra |
| 9 | `agent/loop.py` | Ghép model, state, plan và tools |
| 10 | `main.py` | Ghép toàn bộ ứng dụng và CLI |
| 11 | `tests/` | Kiểm chứng mental model bằng hành vi |

Không chuyển sang bước kế tiếp nếu chưa trả lời được các câu hỏi kiểm tra ở cuối
mỗi bước.

---

## 5. Bước 0 — Biết chương trình được chạy như thế nào

Đọc theo thứ tự:

1. [`pyproject.toml`](../pyproject.toml)
2. [`.env.example`](../.env.example)
3. [`compose.yaml`](../compose.yaml)
4. [`Dockerfile`](../Dockerfile)
5. [`docker-entrypoint.sh`](../docker-entrypoint.sh)
6. [`sandbox-image/Dockerfile`](../sandbox-image/Dockerfile)

Đây không phải Python, nhưng chúng trả lời các câu hỏi Python quan trọng:

- package source nằm dưới `src/`;
- entry point `coding-agent` gọi `main:main`;
- `API_KEY`, `MODEL`, `BASE_URL` đến từ environment;
- control plane có Docker CLI;
- model-generated commands chạy trong child sandbox, không chạy trực tiếp trên
  host.

### Điểm dừng kiểm tra

Bạn phải giải thích được:

- Lệnh nào bắt đầu chương trình?
- Tại sao Python import được package dưới `src/`?
- Có bao nhiêu container tham gia?
- `.env` được đọc ở đâu nhưng không nên commit vì sao?

---

## 6. Bước 1 — Config và đường dẫn

### 6.1. [`src/core/config.py`](../src/core/config.py)

Đây là file Python nên đọc đầu tiên vì chỉ có vài dòng.

Đọc từng dòng theo cách sau:

```python
import os
```

Import standard library để đọc environment variables.

```python
from dotenv import load_dotenv
load_dotenv()
```

Import function từ dependency, sau đó nạp các biến trong `.env` vào environment
của process hiện tại.

```python
API_KEY = os.environ["API_KEY"]
MODEL = os.environ["MODEL"]
BASE_URL = os.environ["BASE_URL"]
```

Dấu `[]` là điểm đáng chú ý: nếu biến không tồn tại, import file sẽ ném
`KeyError`. Đây là fail-fast configuration, không phải giá trị optional.

```python
CONTEXT_WINDOW = 256000
```

Constant nội bộ dùng để ước lượng thời điểm context cần compact.

### 6.2. [`src/core/paths.py`](../src/core/paths.py)

Đọc theo thứ tự bên trong file:

1. `ControlPaths` fields;
2. `default_root()`;
3. `create()`;
4. cách `workspace_hash` được tạo;
5. các path con như metadata, journal và workspace.

Khi đọc từng assignment trong `create()`, hãy ghi ra cây thư mục mà nó tạo.

### Điểm dừng kiểm tra

- Import `core.config` có side effect gì?
- `ControlPaths` chứa path hay chứa nội dung file?
- Tại sao source workspace cần được resolve và hash?

---

## 7. Bước 2 — Hai ví dụ state nhỏ

### 7.1. [`src/runtime/machine.py`](../src/runtime/machine.py)

File này giúp học generics và state transition mà chưa phải đọc runtime thật.

Đọc theo thứ tự:

1. `TypeVar`;
2. `InvalidTransition`;
3. dataclass `Transition`;
4. `StateMachine.__init__()`;
5. `validate()`;
6. `dispatch()`.

Mental model:

```text
state hiện tại + event + context
              ↓
      tìm transition hợp lệ
              ↓
         state tiếp theo
```

### 7.2. [`src/agent/state.py`](../src/agent/state.py)

Đọc `WorkspaceState.snapshot()` trước, rồi `render()`. Chú ý khác biệt:

- `snapshot()` trả dữ liệu có cấu trúc;
- `render()` chuyển state thành text để đưa vào prompt.

### Điểm dừng kiểm tra

- `validate()` khác `dispatch()` ở đâu?
- Predicate trong `Transition` dùng để làm gì?
- Tại sao prompt nhận text render thay vì object Python?

---

## 8. Bước 3 — Học từ vựng dữ liệu

### 8.1. [`src/runtime/models.py`](../src/runtime/models.py)

Không đọc liền 238 dòng. Chia thành bốn nhóm.

#### Nhóm A — Enum trạng thái

Đọc từ `TurnStatus` đến `ToolTransition`. Với mỗi enum, ghi một bảng:

```text
Tên enum | Các giá trị | Object nào sử dụng
```

Đặc biệt phân biệt:

- `TurnStatus`: trạng thái của một lượt hội thoại;
- `PlanStepStatus`: trạng thái một bước kế hoạch;
- `ToolExecutionStatus`: trạng thái một lần thực thi tool;
- `ReplayPolicy`: cách xử lý khi process chết giữa side effect.

#### Nhóm B — Event và lỗi

Đọc `RuntimeEvent`, `ErrorRecord`, `ApprovalMetadata`. Đây là dữ liệu được ghi
hoặc dựng lại từ journal.

#### Nhóm C — State từng aggregate

Đọc `TurnState`, `PlanStep`, `Plan`, `ToolExecution`. Ghi rõ field nào là ID,
field nào là status, field nào là evidence hoặc error.

#### Nhóm D — Toàn bộ runtime

Cuối cùng đọc `RuntimeState`. Đây là object reducer xây từ chuỗi event.

### 8.2. [`src/sandbox/models.py`](../src/sandbox/models.py)

Đọc theo thứ tự:

1. `SandboxStatus` và `WorkspaceMode`;
2. `SandboxPath.__new__()`;
3. `ResourceLimits.__post_init__()`;
4. `ExecRequest.__post_init__()`;
5. `SandboxResult.success`;
6. các dataclass manifest và changeset.

`SandboxPath` và `ExecRequest` là trust-boundary validation. Với từng `raise
ValueError`, hãy tự nghĩ một input xấu mà nhánh đó chặn.

### Điểm dừng kiểm tra

- `turn_id`, `tool_call_id` và `execution_id` có giống nhau không?
- State nào là persisted fact, state nào được suy ra?
- Tại sao `ResourceLimits` validate ngay trong `__post_init__()`?

---

## 9. Bước 4 — Contract của tool

### 9.1. [`src/tools/base.py`](../src/tools/base.py)

Đọc `ToolResult` trước. Đây là contract output chung:

- `raw`: dữ liệu đầy đủ để lưu journal;
- `compact`: dữ liệu ngắn để gửi lại model;
- `success`: kết quả logic;
- `exit_code` và `metadata`: thông tin bổ sung.

Sau đó đọc `BaseTool` theo thứ tự:

1. class attributes;
2. `schema()`;
3. `validate()`;
4. recovery methods;
5. `run()`;
6. abstract `execute()`.

Phân biệt rõ:

```text
run()     = validate chung rồi gọi execute()
execute() = logic riêng của từng tool
```

### 9.2. [`src/tools/filesystem.py`](../src/tools/filesystem.py)

Đọc `_SandboxFileTool` trước, rồi lần lượt:

1. `ReadTool`;
2. `WriteTool`;
3. `EditTool`.

Với mỗi tool, đọc theo bốn phần:

```text
replay_policy → JSON schema → recovery metadata → execute
```

Chú ý `WriteTool` biết hash trước và hash dự kiến sau khi ghi. Đó là dữ liệu
giúp recovery phân biệt side effect đã xảy ra hay chưa.

### 9.3. [`src/tools/terminal.py`](../src/tools/terminal.py)

Theo dõi `command` từ input đến `ExecRequest`, sau đó đến `sandbox.exec()` và
cuối cùng thành `ToolResult`.

### 9.4. [`src/tools/plan.py`](../src/tools/plan.py)

Đây là tool đặc biệt: model thấy nó như tool bình thường, nhưng side effect là
thay đổi runtime plan trong agent chứ không chạy trong Docker sandbox.

### 9.5. [`src/tools/registry.py`](../src/tools/registry.py)

File này ghép tool instances vào hai view:

- list để xuất schemas cho model;
- dictionary để lookup theo tên khi execute.

### 9.6. [`src/tools/verbs.py`](../src/tools/verbs.py)

Đọc file này sau cùng. Nó mô tả một hướng structured shell cũ và không nằm trên
production path hiện tại. Đừng dùng nó để suy luận cách `BashTool` đang chạy.

### Điểm dừng kiểm tra

- Model nhìn thấy phần nào của một tool?
- Tại sao `raw` và `compact` phải tách nhau?
- Tool nào replay-safe, reconcilable hoặc manual?
- Khi không có sandbox, filesystem và shell tool xử lý thế nào?

---

## 10. Bước 5 — Model, context và memory

### 10.1. [`src/model/llm.py`](../src/model/llm.py)

Đọc theo thứ tự:

1. `get_context_window()`;
2. `complete()`;
3. `complete_text()`;
4. `content_to_text()`;
5. hai function đếm token.

Trong `complete()`, tách thành ba khối:

```text
client_options
request
OpenAI API call
```

Chú ý:

- timeout và retry thuộc client options;
- `model`, `messages`, `stream` và `tools` thuộc request;
- `tools` chỉ được thêm khi khác `None`;
- `stream=False` trả response hoàn chỉnh;
- `stream=True` trả iterable chunks mà agent đọc dần.

### 10.2. [`src/context/compactor.py`](../src/context/compactor.py)

Đọc `should_compact()` trước để biết khi nào compaction xảy ra. Sau đó đọc
`compact()` theo flow:

```text
tách system messages
        ↓
nhóm assistant tool call với tool results
        ↓
giữ các nhóm gần nhất
        ↓
tóm tắt phần cũ
        ↓
ghép context mới
```

Đọc `_interaction_groups()` thật chậm. Đây là nơi invariant “không tách tool
call khỏi tool result” được bảo vệ.

### 10.3. [`src/memory/manager.py`](../src/memory/manager.py)

Phân biệt:

- context compaction chỉ phục vụ một model request;
- `PROJECT.md` là memory dài hạn giữa nhiều session.

Đọc `read()` trước, rồi `update()`. Trong `update()`, theo dõi text response từ
model được parse thành JSON diff và áp dụng vào file như thế nào.

### Điểm dừng kiểm tra

- `complete()` và `complete_text()` khác nhau ở contract nào?
- Tại sao không gửi base64 image vào token counter?
- Compacted summary có được ghi vào journal không?
- `PROJECT.md` khác message history ở đâu?

---

## 11. Bước 6 — Journal và reducer

### 11.1. [`src/memory/event_store.py`](../src/memory/event_store.py)

Đây là file quan trọng. Đọc theo thứ tự, không nhất thiết từ trên xuống:

1. error classes;
2. `_redact()`;
3. các helper tạo/list/resolve chat;
4. `EventStore.__init__()` và lock;
5. `append_event()`;
6. `append()`;
7. `_iter_events()` và validation;
8. `to_messages()`.

Khi đọc `append_event()`, đánh dấu thứ tự side effect:

```text
tạo event envelope
        ↓
redact payload
        ↓
serialize JSON
        ↓
append + flush + fsync
```

`to_messages()` không trả toàn bộ runtime state. Nó chỉ project journal thành
message format mà model hiểu.

### 11.2. [`src/runtime/reducer.py`](../src/runtime/reducer.py)

Đọc theo thứ tự:

1. helper `_error()` và `_steps()`;
2. `reduce_event()` từng nhóm event;
3. `replay()`;
4. `completion_blockers()`;
5. `pending_runtime_actions()`;
6. `runtime_prompt_projection()`.

Với mỗi branch trong `reduce_event()`, ghi bảng:

```text
event_type | state cũ cần có | field được cập nhật | lỗi nếu invariant sai
```

Đây là nơi cần đọc từng dòng cẩn thận nhất. Reducer không tạo side effect bên
ngoài; nó biến `RuntimeState + event` thành `RuntimeState` mới.

### Điểm dừng kiểm tra

- Journal hay `RuntimeState` là nguồn sự thật bền vững?
- `append()` khác `append_event()` thế nào?
- Tại sao reducer phải fail closed khi event sequence sai?
- Message projection khác runtime prompt projection thế nào?

---

## 12. Bước 7 — Tool executor

Đọc [`src/runtime/executor.py`](../src/runtime/executor.py) sau khi đã hiểu tool,
journal và reducer.

### Thứ tự đọc bên trong file

1. `_walk_values()` và `_error()`;
2. constructor của `ToolExecutor`;
3. `request_batch()`;
4. `execute()`;
5. `resolve_approval()`;
6. `resolve_recovery()`;
7. `_fail()`.

### Cách đọc `request_batch()`

Theo từng tool call và ghi ra:

```text
tool_call_id từ model
        ↓
execution_id do runtime tạo
        ↓
recovery metadata
        ↓
ToolRequested event
```

### Cách đọc `execute()`

Chia method thành các checkpoint:

```text
replay state
   ↓
kiểm tra status
   ↓
lookup tool và validate arguments
   ↓
policy: deny / ask / allow
   ↓
persist ToolStarted
   ↓
thực thi tool
   ↓
persist completed hoặc failed
```

Mỗi khi thấy `self.store.append_event(...)`, dừng lại và hỏi: “Nếu process chết
ngay sau dòng này thì lúc resume hệ thống biết gì?”

### Điểm dừng kiểm tra

- Vì sao phải persist toàn bộ request batch trước side effect đầu tiên?
- Policy `ASK` tạo pending action bằng cách nào?
- Khi tool ném exception, result và event được tạo ra sao?
- Replay policy ảnh hưởng recovery thế nào?

---

## 13. Bước 8 — Sandbox

Đây là subsystem lớn. Đọc từ dữ liệu ở trong container ra control plane.

### 13.1. Helper trong child container

Đọc trước:

1. [`sandbox-image/sandbox_fs.py`](../sandbox-image/sandbox_fs.py)
2. [`sandbox-image/sandbox_exec.py`](../sandbox-image/sandbox_exec.py)

#### `sandbox_fs.py`

Đọc theo thứ tự:

1. `_parts()` validate relative path;
2. `_parent_fd()` đi từng path component bằng file descriptor;
3. `_read()`;
4. `_atomic_write()`;
5. `handle()` dispatch operation;
6. `main()` đọc JSON stdin và in JSON stdout.

Chú ý các syscall và flag như `dir_fd`, `O_NOFOLLOW`, `fsync` và atomic
`replace`. Đây là lớp chống symlink race và partial write.

#### `sandbox_exec.py`

Đọc `_cwd()` rồi `execute()`. Theo dõi:

- cwd được validate thế nào;
- subprocess được tạo với environment nào;
- stdout/stderr bị giới hạn ra sao;
- timeout và signal được xử lý thế nào;
- kết quả được serialize thành JSON thế nào.

### 13.2. Docker boundary

Đọc [`src/sandbox/docker.py`](../src/sandbox/docker.py) theo nhóm:

1. isolation classification và error types;
2. `_run()`;
3. `DockerBackend.preflight()`;
4. `create()`;
5. `start()`, `stop()`, `destroy()`;
6. `inspect()` và `verify_runtime()`;
7. `_helper()`;
8. `exec()` và `fs_call()`.

Mỗi list `argv` trong file này cuối cùng trở thành một lệnh Docker CLI. Đọc list
theo từng argument thay vì nhìn như một chuỗi dài.

### 13.3. Workspace

Đọc [`src/sandbox/workspace.py`](../src/sandbox/workspace.py):

1. pattern matching helpers;
2. manifest helpers;
3. `prepare_workspace()` cho shadow mode;
4. `prepare_live_workspace()` cho live mode.

Ghi rõ file nào được copy, ignore, mask hoặc từ chối.

### 13.4. Changeset

Đọc [`src/sandbox/changes.py`](../src/sandbox/changes.py) theo hai luồng riêng:

```text
build/seal: scan → compare → preview → hash → persist
apply: verify → conflict check → backup → copy/delete → journal → rollback
```

Đây chủ yếu là shadow-workspace compatibility path. Đừng nhầm với live mode
mặc định, nơi thay đổi đã xuất hiện trực tiếp trong workspace.

### 13.5. Session lifecycle

Cuối cùng đọc [`src/sandbox/session.py`](../src/sandbox/session.py):

1. constructor và persisted metadata;
2. `create()`;
3. `start()`;
4. watchdog;
5. `stop()` và `resume()`;
6. `prepare_changes()`;
7. disk budget;
8. `destroy()`;
9. delegation `exec()`, `fs_call()`, `fingerprint()`;
10. cleanup session hết hạn.

### Điểm dừng kiểm tra

- Code nào chạy trên control plane, code nào chạy trong child container?
- JSON-over-stdin đi qua những function nào?
- Tại sao filesystem helper không chỉ dùng `Path.resolve()`?
- `DockerBackend` và `SandboxSession` khác trách nhiệm thế nào?
- Live mode và shadow mode khác nhau ở đâu?

---

## 14. Bước 9 — Agent loop

Chỉ bắt đầu [`src/agent/loop.py`](../src/agent/loop.py) sau các bước trên. Đây là
file orchestration lớn nhất.

### 14.1. Đọc các helper trước

1. `encode_image()`;
2. `build_user_content()`;
3. `StreamedToolCall`;
4. `SYSTEM_PROMPT_TEMPLATE`.

### 14.2. Đọc constructor

Trong `Agent.__init__()`, lập bảng:

```text
field | object được truyền vào hay tự tạo | trách nhiệm
```

Đặc biệt theo dõi:

- `event_store`;
- `runtime_state`;
- `memory_manager`;
- `compactor`;
- `tool_registry`;
- `tool_executor`;
- `plan_tool`;
- `sandbox`.

### 14.3. Đọc các method build input

Theo thứ tự:

1. `_refresh()`;
2. `_event()`;
3. `_build_system_message()`;
4. `_build_context()`;
5. `_consume_stream()`.

Trong `_consume_stream()`, theo dõi riêng hai dòng dữ liệu:

```text
text delta       → full_text
tool-call delta  → calls[index] → StreamedToolCall hoàn chỉnh
```

### 14.4. Đọc entry point của một turn

Đọc `run_turn()` và ghi lại thứ tự event. Sau đó đọc `_drive_turn()` theo từng
iteration:

```text
advance iteration
        ↓
build context
        ↓
call model stream
        ↓
consume text/tool calls
        ↓
tool branch hoặc final-answer branch
        ↓
completion blockers
```

Không đọc `_drive_turn()` như một khối 100 dòng. Dùng các lệnh `continue`,
`return` và `raise` làm ranh giới các nhánh.

### 14.5. Đọc plan path

Sau khi hiểu loop chính, đọc `_apply_plan_action()`. Chia theo ba action:

- `create`;
- `set_step_status`;
- `revise`.

Với mỗi action, liệt kê event được append và điều kiện từ chối.

### 14.6. Đọc resume, approval và recovery

Đọc:

1. `pending_runtime_actions()`;
2. `resolve_approval()`;
3. `resolve_recovery()`;
4. `_drain_pending_executions()`;
5. `resume_active_turn()`.

### 14.7. Đọc memory update cuối cùng

Đọc `_render_recent_for_memory()` rồi `_update_memory_bg()`. Chú ý memory update
chạy background và failure của nó không làm turn chính thất bại.

### Điểm dừng kiểm tra

- User message được ghi vào journal trước hay sau model call?
- Tool-call fragments được ghép bằng key nào?
- Khi có tool call, assistant message nào được persist?
- Khi không có tool call, điều gì có thể chặn final answer?
- Resume tránh duplicate user message và side effect bằng cách nào?

---

## 15. Bước 10 — `main.py`

Bây giờ đọc [`src/main.py`](../src/main.py) từ trên xuống sẽ dễ hơn.

### Thứ tự đề xuất

1. `build_parser()`;
2. `select_chat_path()`;
3. `handle_pending_runtime_actions()`;
4. `_render_changes()`;
5. `_default_agent_factory()`;
6. `run_repl()`;
7. `main()`.

### Cách đọc `_default_agent_factory()`

Vẽ chuỗi object construction:

```text
ControlPaths
    ↓
DockerBackend
    ↓
SandboxSession
    ↓
ToolRegistry
    ↓
Agent
```

Đánh dấu nhánh session mới và nhánh resume session cũ.

### Cách đọc `run_repl()`

Tách command nội bộ khỏi prompt gửi model:

- `exit`, `quit`;
- `/changes`;
- `/apply`;
- `/discard`;
- input thông thường;
- `/img` được parse thành image paths và text.

### Cách đọc `main()`

Theo dõi resource lifetime:

```text
parse args → select chat → create agent → run REPL → close agent
```

### Điểm dừng kiểm tra

- CLI argument nào quyết định workspace và sandbox image?
- Session mới và session resume khác nhau ở đâu?
- Pending approval/recovery được xử lý trước prompt mới vì sao?
- `Agent.close()` được bảo đảm chạy bằng cấu trúc nào?

---

## 16. Bước 11 — Đọc tests như executable documentation

Không cần đọc tests theo bảng chữ cái. Đọc ngay sau subsystem tương ứng.

| Sau khi đọc source | Đọc test |
|---|---|
| `model/llm.py` | `tests/test_model_llm.py` |
| `context/compactor.py` | `tests/test_compactor.py` |
| `runtime/machine.py` | `tests/test_machine.py` |
| `runtime/reducer.py` | `tests/test_reducer.py`, `tests/test_plan_lifecycle.py` |
| `memory/event_store.py` | `tests/test_event_store.py` |
| `runtime/executor.py` | `tests/test_tool_lifecycle.py`, `tests/test_recovery.py` |
| tool classes | `tests/test_sandbox_tools.py` |
| sandbox models | `tests/test_sandbox_models.py` |
| workspace | `tests/test_workspace_snapshot.py` |
| changeset | `tests/test_changeset.py`, `tests/test_changeset_apply.py` |
| Docker backend | `tests/test_docker_backend.py` |
| sandbox session | `tests/test_sandbox_session.py` |
| child helpers | `tests/test_sandbox_helpers.py` |
| `agent/loop.py` | `tests/test_agent_loop.py` |
| `main.py` | `tests/test_main.py`, `tests/test_compose_launcher.py` |
| toàn bộ sandbox | `tests/test_sandbox_integration.py` |

### Cách đọc một test

Đọc theo thứ tự:

1. tên test mô tả behavior nào;
2. Arrange: state và fake objects được tạo ra sao;
3. Act: method thật nào được gọi;
4. Assert: observable result nào được kiểm tra;
5. production bug nào sẽ làm test thất bại.

Đọc [`tests/fakes.py`](../tests/fakes.py) trước các test agent để hiểu fake stream
chunks và fake tool.

Chạy toàn bộ tests:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Chạy một file khi đang học subsystem:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_model_llm
```

---

## 17. Một luồng end-to-end để lần bằng debugger

Dùng prompt đơn giản khiến model gọi `read`. Đặt breakpoint theo thứ tự:

1. `main.run_repl()`;
2. `Agent.run_turn()`;
3. `EventStore.append()` khi ghi user message;
4. `Agent._drive_turn()`;
5. `llm.complete()`;
6. `Agent._consume_stream()`;
7. `ToolExecutor.request_batch()`;
8. `ToolExecutor.execute()`;
9. `ReadTool.execute()`;
10. `SandboxSession.fs_call()`;
11. `DockerBackend.fs_call()`;
12. `sandbox_fs.handle()` trong child container;
13. quay lại `ToolExecutor.execute()` để persist result;
14. `reduce_event()` khi agent refresh state;
15. iteration tiếp theo của `_drive_turn()`;
16. final-answer branch và `TurnCompleted`.

Ở mỗi breakpoint, chỉ theo dõi các biến sau:

```text
turn_id
tool_call.id
execution_id
messages
runtime_state
event_type
ToolResult.raw / compact / success
```

Nếu theo dõi quá nhiều biến cùng lúc, bạn sẽ mất luồng chính.

---

## 18. Checklist “tôi đã hiểu file này chưa?”

Trước khi đánh dấu một file là đã đọc, hãy tự trả lời:

- [ ] Tôi mô tả trách nhiệm file bằng một câu được không?
- [ ] Tôi biết caller chính của file không?
- [ ] Tôi biết input và output công khai không?
- [ ] Tôi biết state nào được đọc và state nào bị thay đổi không?
- [ ] Tôi biết side effect nằm ở dòng hoặc method nào không?
- [ ] Tôi biết failure được biểu diễn bằng exception, event hay result không?
- [ ] Tôi biết test nào bảo vệ behavior quan trọng không?
- [ ] Tôi có thể giải thích vì sao file này không nên gộp vào file khác không?

Nếu trả lời “không” cho ba mục trở lên, quay lại lượt đọc thứ hai thay vì cố nhớ
từng dòng.

---

## 19. Lộ trình học nhanh trong 3 ngày

### Ngày 1 — Ngôn ngữ và data model

- `core/config.py`, `core/paths.py`;
- `runtime/machine.py`, `agent/state.py`;
- `runtime/models.py`, `sandbox/models.py`;
- `tools/base.py`.

Kết quả cần đạt: tự vẽ được các state và ID chính.

### Ngày 2 — Journal, tool và sandbox

- tool implementations và registry;
- `memory/event_store.py`;
- `runtime/reducer.py`;
- `runtime/executor.py`;
- sandbox helpers, backend và session.

Kết quả cần đạt: lần được một tool call từ JSON đến side effect và journal.

### Ngày 3 — Agent và CLI

- `model/llm.py`, compactor và memory;
- `agent/loop.py`;
- `main.py`;
- tests theo subsystem;
- lần một flow bằng debugger.

Kết quả cần đạt: giải thích được một turn hoàn chỉnh và một turn có recovery.

---

## 20. Lộ trình đọc sâu trong 7 ngày

| Ngày | Nội dung | Sản phẩm tự viết |
|---:|---|---|
| 1 | Cú pháp Python và config/path | Cây thư mục control state |
| 2 | Runtime và sandbox models | Bảng enum, ID và state |
| 3 | Tool contracts và model boundary | Sơ đồ request/response |
| 4 | EventStore và reducer | Timeline event của một turn |
| 5 | Executor, approval, recovery | Bảng crash point và cách resume |
| 6 | Docker, workspace, changeset, session | Sơ đồ trust boundary |
| 7 | Agent loop, main, tests | Trace end-to-end hoàn chỉnh |

Không cần viết bài dài. Mỗi sản phẩm chỉ cần vừa một trang giấy hoặc một file
Markdown ngắn.

---

## 21. Tài liệu đọc sâu sau guide này

Sau khi hoàn thành thứ tự trên, dùng các tài liệu có sẵn để đào sâu:

- [`source_code_flow_guide_vi.md`](source_code_flow_guide_vi.md): toàn bộ kiến
  trúc và call flow;
- [`runtime_state_management_walkthrough_vi.md`](runtime_state_management_walkthrough_vi.md):
  journal, reducer, plan, approval và recovery;
- [`coding_agent_safety_architecture.md`](coding_agent_safety_architecture.md):
  security boundaries;
- [`sandbox_v1_operations.md`](sandbox_v1_operations.md): vận hành sandbox;
- [`agent_lifecycle_flows.md`](agent_lifecycle_flows.md): sơ đồ lifecycle;
- [`run_guide.md`](run_guide.md): cách build và chạy ứng dụng.

Guide hiện tại trả lời “đọc theo thứ tự nào và đọc ra sao”. Các tài liệu trên
trả lời sâu hơn “hệ thống hoạt động như thế nào”.

---

## 22. Công thức cuối cùng

Khi bị lạc trong code, quay lại công thức này:

```text
Ai gọi function này?
        ↓
Input đến từ đâu?
        ↓
Function validate điều gì?
        ↓
Nó thay đổi state hay tạo side effect nào?
        ↓
Nó trả gì hoặc ghi event gì?
        ↓
Caller tiếp theo xử lý kết quả ra sao?
        ↓
Test nào chứng minh toàn bộ contract đó?
```

Hiểu từng dòng không có nghĩa là thuộc từng dòng. Bạn thực sự hiểu khi có thể
dự đoán điều gì sẽ xảy ra nếu thay đổi hoặc xóa dòng đó.
