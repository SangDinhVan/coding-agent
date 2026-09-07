## Project Files
- `output/hello_world.py`: script Python in Hello, World!; chạy bằng `python3 output/hello_world.py`.

## Project Files
- `output/fried-chicken.html`: website bán gà rán CrispyChicken hoàn chỉnh dạng single-file HTML, CSS/JS inline, responsive, giỏ hàng lưu localStorage key `cc_cart`, JS đã kiểm tra không lỗi cú pháp.
- `output/dino-game.html`: game khủng long runner single-file HTML, CSS/JS inline, localStorage lưu kỷ lục, responsive.

## Project Files
- `todo-app/`: fullstack Todo App đã DEPLOY đang chạy bằng Docker Compose.
  - Backend: `todo-app/backend/` — FastAPI + SQLAlchemy 2 + SQLite, REST API `/api/todos` (CRUD + filter `?completed=` + `DELETE /api/todos/clear-completed`), validation Pydantic (title 1–200 ký tự, trim, whitespace-only → 422). Chạy test: `cd todo-app/backend && .venv/bin/python -m pytest` (50 tests pass, cần `requirements-dev.txt`).
  - Frontend: `todo-app/frontend/` — React 18 + Vite 5, UI tiếng Việt (thêm/toggle/xóa/bộ lọc all-active-completed/xóa hàng loạt đã xong/error banner + retry), API client `src/api.js`. Chạy test: `cd todo-app/frontend && npx vitest run` (16 tests pass, cần `npm install`; esbuild phải được approve: `npm install-scripts approve esbuild`).
  - Deploy: `todo-app/docker-compose.yml` — 2 services: `backend` (port 8000, uvicorn, healthcheck, volume `todo-data` chứa `/data/todos.db`) + `frontend` (port 3000, multi-stage build node→nginx, nginx reverse-proxy `/api` → backend:8000). Lệnh: `cd todo-app && docker compose up -d --build`. URL chính: http://localhost:3000, Swagger: http://localhost:8000/docs. Verified end-to-end qua nginx: CRUD, filter, 404, 422, persistence volume OK.

## Status Update
- `todo-app/`: đã tắt bằng `docker compose down`; containers `todo-backend` và `todo-frontend` cùng network đã bị xóa. Volume `todo-app_todo-data` vẫn được giữ, dữ liệu `todos.db` còn an toàn. Khởi động lại: `cd todo-app && docker compose up -d --build` để dùng lại dữ liệu cũ.

## Project Files
- `output/tong_so_chan.cpp`: chương trình C++17 nhập mảng số nguyên, in các số chẵn và tổng các số chẵn (dùng `long long`). Build/test: `g++ -std=c++17 -o /tmp/tong_so_chan output/tong_so_chan.cpp`; test với input gồm dòng `10` và dòng `1 2 3 4 5 6 7 8 9 10` cho tổng `30`, hoặc dòng `3` và dòng `1 3 5` cho tổng `0`.

## Hard Constraints
- Thư mục làm việc thực tế: `/home/sang/working/TLCN/repo/coding-agent` (không phải `/home/dub1056/code/repos/plate-clone`). Nên kiểm tra `pwd` trước khi chạy lệnh build/run.

## Failed Approaches
- Đã dùng sai đường dẫn repo và cú pháp `cd <đường-dẫn> --output/tmp`, gây lỗi `cd: too many arguments` và `No such file or directory`.

## Project Files
- `output/OopAnimal.java`: ví dụ Java OOP thể hiện kế thừa (`Animal` -> `Dog`, `Cat`) với `super()`, override `makeSound()`, đa hình qua mảng `Animal`; đã biên dịch/chạy thành công. Lệnh kiểm tra: `cd /home/sang/working/TLCN/repo/coding-agent && docker run --rm -v $(pwd)/output:/src:ro -w /tmp eclipse-temurin:21-jdk-alpine sh -c "cp /src/OopAnimal.java /tmp/ && javac OopAnimal.java && java OopAnimal"` (không để lại file `.class` trong repo).

## Environment
- Host hiện tại là Arch Linux, chưa cài `java`/`javac`; `sudo` yêu cầu mật khẩu nên không cài Java bằng `pacman` trong ngữ cảnh này. Docker đang chạy và image `eclipse-temurin:21-jdk-alpine` đã được pull để biên dịch/chạy Java bằng container.

## Status Update
- Docker: đã xóa image `eclipse-temurin:21-jdk-alpine`; không còn image Java/JDK trong `docker images`. Không có container Java nào liên quan.
- Khi cần chạy Java lại (ví dụ `output/OopAnimal.java`), phải pull lại image trước: `docker pull eclipse-temurin:21-jdk-alpine`; lệnh `docker run` trước đó vẫn dùng được sau khi pull lại.
