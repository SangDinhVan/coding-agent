# Chạy Coding Agent bằng Docker Compose

Chạy các lệnh dưới đây tại thư mục gốc repository (nơi có `compose.yaml`).
Ứng dụng cần Docker Compose, Docker daemon đang chạy và cgroups v2. Sandbox
chấp nhận Docker Engine **rootless** trên Linux hoặc Docker Desktop chạy
**Linux containers** trên Windows/macOS. Docker Engine rootful chạy trực tiếp
trên Linux bị ứng dụng từ chối. Docker Desktop được chấp nhận cho đồ án nhưng
không tương đương bảo đảm an toàn của rootless Docker.

## 1. Chuẩn bị Docker

### Linux: Docker Engine rootless

Nếu chưa cài rootless Docker, làm theo [hướng dẫn chính thức của Docker](https://docs.docker.com/engine/security/rootless/).
Lệnh `dockerd-rootless-setuptool.sh install` là bước **cài đặt một lần**, không
cần chạy mỗi khi mở ứng dụng. Trong terminal dùng để chạy Compose, kiểm tra:

```bash
docker context use rootless
docker info --format 'security={{json .SecurityOptions}} cgroup={{.CgroupVersion}}'
test -S "$XDG_RUNTIME_DIR/docker.sock"
```

`security` phải có `rootless`, `cgroup` phải là `2`, và lệnh `test` phải thành
công. Compose tự mount `$XDG_RUNTIME_DIR/docker.sock` vào container. Nếu socket
rootless ở vị trí khác, đặt `DOCKER_SOCKET` khi chạy (xem mục xử lý lỗi).

### Windows/macOS: Docker Desktop

Khởi động Docker Desktop và dùng chế độ **Linux containers**. Trên Windows,
chạy các lệnh từ PowerShell tại thư mục repository; trên macOS dùng Terminal.
Kiểm tra daemon:

```text
docker info --format 'os={{.OperatingSystem}} cgroup={{.CgroupVersion}}'
```

`os` phải nhận diện Docker Desktop và `cgroup` phải là `2`. Khi không có
`XDG_RUNTIME_DIR`, Compose mount `/var/run/docker.sock` vào Linux container;
không thay bằng Windows named pipe `\\.\pipe\docker_engine`.

Nếu chạy trong **WSL** thay vì PowerShell, hãy đảm bảo Docker Desktop đã bật
WSL integration cho distro đó. Nếu WSL có `XDG_RUNTIME_DIR` riêng, đặt
`DOCKER_SOCKET=/var/run/docker.sock` để Compose không chọn nhầm socket WSL.

## 2. Cấu hình môi trường

Tạo `.env` từ file mẫu:

```bash
# Linux, macOS hoặc WSL (chỉ tạo khi chưa có .env)
if [ ! -f .env ]; then cp .env.example .env; fi
```

```powershell
# Windows PowerShell (chỉ tạo khi chưa có .env)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Mở `.env` và điền giá trị thật cho cả `API_KEY`, `MODEL` và `BASE_URL` theo
nhà cung cấp API bạn dùng. Không commit `.env` hoặc đưa API key vào lệnh chạy.
Nếu `.env` đã tồn tại, chỉ chỉnh file đó, không cần sao chép lại.

## 3. Chạy ứng dụng

Lệnh giống nhau trên Linux, PowerShell và macOS:

```text
docker compose run --build --rm coding-agent
```

Để tiếp tục phiên gần nhất:

```text
docker compose run --build --rm coding-agent resume --last
```

Gõ `exit` trong ứng dụng để thoát. Mỗi lần chạy, Compose build image điều
khiển nếu cần; entrypoint bên trong container tiếp tục build image sandbox.

## 4. Khi gặp lỗi Docker socket

Kiểm tra container có nói chuyện được với daemon, **không khởi động agent**:

```text
docker compose run --build --rm --no-deps --entrypoint docker coding-agent info
```

Nếu báo `permission denied ... /var/run/docker.sock`:

- Linux rootless: kiểm tra `docker context show` là `rootless` và socket trong
  `$XDG_RUNTIME_DIR` tồn tại. Có thể chỉ định rõ socket:

  ```bash
  DOCKER_SOCKET="$XDG_RUNTIME_DIR/docker.sock" docker compose run --build --rm coding-agent
  ```

- Windows chạy trong WSL: nếu có `XDG_RUNTIME_DIR` riêng, dùng:

  ```bash
  DOCKER_SOCKET=/var/run/docker.sock docker compose run --build --rm coding-agent
  ```

- Windows/macOS Docker Desktop: kiểm tra đang dùng Linux containers. Nếu tổ
  chức bật [Enhanced Container Isolation](https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/),
  chính sách đó có thể chặn mount Docker socket; cần quản trị viên cấp ngoại
  lệ phù hợp.

Cảnh báo `legacy builder is deprecated` không phải lỗi quyền socket và không
phải nguyên nhân khiến lệnh trên dừng.

Nếu trên Windows báo `exec /usr/local/bin/coding-agent-entrypoint: no such file
or directory` ngay sau khi tạo container, hãy cập nhật repository rồi chạy lại
`docker compose run --build --rm coding-agent`. Lỗi này có thể do script được
checkout với dòng CRLF; Dockerfile hiện chuẩn hóa LF khi build image.
