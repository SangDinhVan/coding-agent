# Nova AI — Giao diện Chatbot (Frontend thuần HTML)

Một web frontend **mô phỏng giao diện chatbot AI kiểu Claude / ChatGPT** — viết bằng **1 file HTML duy nhất** (HTML + CSS + JS), **không cần build, không cần server, không phụ thuộc thư viện**. Mở là chạy.

## 🚀 Chạy nhanh

**Cách 1 — mở trực tiếp (khuyên dùng):**
```bash
# Chỉ cần double-click file index.html, hoặc:
open index.html        # macOS
xdg-open index.html    # Linux
start index.html       # Windows
```

**Cách 2 — chạy qua một server local tùy ý:**
```bash
python3 -m http.server 8000
# rồi mở http://localhost:8000
```

## ✨ Tính năng

| Tính năng | Mô tả |
|-----------|-------|
| 💬 **Streaming trả lời** | AI "gõ" chữ theo thời gian thực (typewriter) + con trỏ nháy, giống ChatGPT |
| ⏹ **Dừng sinh** | Nhấn nút gửi (trở thành ■) để dừng giữa chừng |
| 📝 **Render Markdown** | Tiêu đề, **in đậm**, *in nghiêng*, list, link, blockquote, `code` inline |
| 🧑‍💻 **Code block đẹp** | Khung code có header, nút **Copy**, highlight syntax (keyword / string / number / comment) |
| 🔁 **Sinh lại** | Nút "Sinh lại" để tạo lại câu trả lời cuối |
| 🗂 **Nhiều hội thoại** | Sidebar quản lý nhiều cuộc trò chuyện, lưu tự động vào `localStorage` |
| 🗑 **Xóa hội thoại** | Xóa từng cuộc hoặc toàn bộ hội thoại hiện tại |
| 🌗 **Đổi giao diện** | Nút mặt trời/trăng chuyển Light ↔ Dark, ghi nhớ lựa chọn |
| 📱 **Responsive** | Hoạt động tốt trên mobile (sidebar trượt ra/vào) |
| 💡 **Gợi ý nhanh** | 4 ô suggestion trên màn hình chào, bấm là gửi |

## 🧠 Cách trả lời hoạt động

Đây là **demo frontend** nên phần "AI" được **mô phỏng phía client** trong hàm `generateReply()`:
- Nhận đầu vào → khớp **từ khóa** (`debounce`, `regex`, `email`, `online/offline`, `chào`...) → trả về câu trả lời mẫu có sẵn (kèm code, list, quote để demo mọi format).
- Chưa nối API thật. Đây là **frontend hoàn chỉnh**, sẵn sàng thay "bộ não" bằng API thật.

### Nối API thật (OpenAI / Claude / Gemini / self-host)
Chỉ cần thay hàm `generateReply()` trong `<script>` bằng một `fetch` tới backend của bạn, rồi dùng kết quả trả về cho phần **streaming** có sẵn. Ví dụ khung:

```javascript
async function generateReply(userText) {
  // Gợi ý: gọi qua 1 backend nhỏ để giấu API key
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: userText }),
  });
  const data = await res.json();
  return data.reply;   // hoặc trả về một stream cho phần streamText()
}
```
> Phần streaming (`streamText`) hiện render **toàn bộ** text rồi gõ dần. Nếu API trả về **token-by-token** (SSE/chunk), bạn chỉ cần sửa nhẹ `streamText` để nhận từng chunk — logic DOM/markdown/copy đã sẵn dùng lại.

## 📁 Cấu trúc

```
/workspace
└── index.html   # toàn bộ: HTML + CSS + JS (1 file, tự chứa)
```

## 🛠 Kỹ thuật
- **Không framework**, không build step, không CDN bắt buộc (font Google là tăng thẩm mỹ; offline vẫn chạy với font hệ thống).
- Markdown tự viết **an toàn XSS** (escape HTML trước khi render).
- State lưu `localStorage` (hội thoại + theme).

## 🔧 Tùy biến nhanh
- **Màu chủ đạo:** đổi biến `--accent` (mặc định cam `#d97757` kiểu Claude) trong `:root` / `[data-theme="dark"]`.
- **Tên thương hiệu:** sửa `Nova AI`, `nova-1` trong HTML.
- **Câu trả lời demo:** thêm/sửa mảng `rules` trong `generateReply()`.

---
*Có thể chỉnh thêm: upload ảnh, lịch sử dài, token streaming thật, multi-user... cứ nói là mình làm tiếp.*
