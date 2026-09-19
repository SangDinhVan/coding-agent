# Chạy Coding Agent với Docker

> [!IMPORTANT]
> Sandbox chấp nhận Linux Docker rootless (`ROOTLESS`) và Docker Desktop trên
> Windows/macOS (`VM_ISOLATED`). Rootful Docker chạy trực tiếp trên Linux
> (`ROOTFUL_BARE`) vẫn bị chặn fail-closed. `VM_ISOLATED` không tương đương
> rootless thật, nhưng được chấp nhận trong phạm vi đồ án.

## 1. Chọn Docker daemon

### Linux rootless

```bash
dockerd-rootless-setuptool.sh install
docker context use rootless
docker info --format '{{json .SecurityOptions}}'
```

Kết quả phải chứa `rootless`. Compose tự dùng socket trong
`$XDG_RUNTIME_DIR` trên Linux. Có thể chỉ định `DOCKER_SOCKET` nếu socket nằm
ở vị trí khác:

```bash
docker compose run --build --rm coding-agent
```

### Windows/macOS

Bật Docker Desktop, chọn **Linux containers**, rồi chạy từ PowerShell, CMD hoặc
Terminal tại thư mục repository:

```text
docker compose run --build --rm coding-agent
```

Compose dùng Unix socket `/var/run/docker.sock` bên trong Linux VM của Docker
Desktop; không mount trực tiếp Windows named pipe `\\.\pipe\docker_engine` vào
Linux control-plane container.

## 2. Cấu hình API key

```bash
cp .env.example .env
```

Mở `.env` và thay `API_KEY=your_api_key_here` bằng API key thật.

## 3. Kiểm tra daemon

```bash
docker info --format 'os={{.OperatingSystem}} version={{.ServerVersion}} security={{json .SecurityOptions}} cgroup={{.CgroupVersion}}'
```

`OperatingSystem=Docker Desktop` được nhận là `VM_ISOLATED`; daemon rootless có
`rootless` trong `SecurityOptions`. Cả hai vẫn phải báo cgroups v2.
