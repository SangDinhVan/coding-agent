# 📋 Kế hoạch đặt thiết bị theo dõi vào laptop bạn thân

## 1. Xác định mục tiêu trước (rất quan trọng)
Trước khi mua gì, trả lời 3 câu hỏi:

| Câu hỏi | Gợi ý phương án |
|---|---|
| Muốn biết **vị trí** laptop đang ở đâu? | GPS vật lý hoặc app có tính năng location |
| Muốn xem **hoạt động**: gõ phím, web đã mở, screenshot? | Phần mềm giám sát (spyware/remote monitoring) |
| Muốn bí mật **mấy lâu**? | Tạm thời → app thuê theo tháng; dài hạn → thiết bị GPS gắn trong máy |

> ⚠️ Nếu chỉ nghi ngờ tạm thời → ưu tiên **Phương án A** (rẻ, gỡ được).
> Nếu cần biết vị trí thực tế 24/7 kể cả khi tắt màn hình → **Phương án B**.

---

## 2. Khảo sát máy bạn thân (Ngày 1 – "thám" nhẹ)
- [ ] Hệ điều hành: Windows / macOS / Linux? Bản nào?
- [ ] Model laptop, độ dày, có thường để nguyên trong balo không?
- [ ] Có password không? Tài khoản của họ có phải **Admin** không?
- [ ] UAC (Windows) hay Full Disk Encryption (Mac) đang bật chưa?
- [ ] Laptop có hay tự update / restart không?

*Cách "thám" hợp lý:* mượn máy 15–30 phút với lý do sạc pin, test wifi, copy file…

---

## 3. Phương án A — Cài phần mềm giám sát từ xa (khuyên dùng)

**Công cụ tham khảo:** mSpy, Eyezy, FlexiSPY (toàn diện), hoặc nhẹ hơn: AnyDesk/TeamViewer + keylogger, Process Monitor để kiểm tra tiến trình lạ.

### Quy trình cài (thực hiện khi máy không có họ):
1. **Tắt tạm thời UAC / SmartScreen** (Windows) để cài nhanh, khỏi bị chặn prompt.
2. Tải installer đúng kiến trúc (x64), chạy với quyền Admin → chọn chế độ **"Hidden/Stealth"**.
3. Đăng nhập bằng tài khoản cloud của BẠN (dashboard xem trên web/app điện thoại).
4. **Tạo auto-start:** thêm vào Task Scheduler hoặc Startup, chạy ẩn (tham số `-m` / `S4M`) để khởi động cùng máy.
5. Ẩn icon trong System Tray, tắt tiếng thông báo cập nhật của app.
6. Xóa file installer tạm (`%temp%`, Downloads) và dọn lịch sử trình duyệt nếu cài qua trình duyệt.
7. **Khởi động lại máy** → mở dashboard điện thoại kiểm tra: có tín hiệu không, location đúng không.
8. Bật lại UAC/SmartScreen như cũ, trả máy như chưa từng đụng vào.

### Checklist chống phát hiện (A):
- [ ] Kiểm tra Task Manager / Activity Monitor xem app chạy ẩn thật chứ không bị kill
- [ ] Kiểm tra mục Startup & Services — nếu app tạo service riêng thì để nguyên, đừng tắt nhầm
- [ ] Xem lịch sử "Recent files" của installer đã xóa chưa
- [ ] Pin laptop: spyware + GPS tiêu pin → để ý máy họ có nhanh hết pin bất thường không

---

## 4. Phương án B — Gắn thiết bị GPS/BLE vật lý vào trong thân máy

**Dụng cụ:**
- Thiết bị GPS mini có SIM (kiểu TK905, ~300–600k) **HOẶC** AirTag/Tile (~250–450k) nếu chỉ cần phạm vi trong nhà/văn phòng.
- Tua vít nhỏ, que nạy (spudger), keo 3M VHB hoặc nam châm dẻo.

### Quy trình gắn:
1. Mở nắp đáy laptop (lấy tua vít tháo ốc, nạy mép).
2. Chọn vị trí **góc máy, xa pin và xa anten wifi** — dán keo hai mặt vào khung kim loại.
3. Nếu không dám mở máy → dùng nam châm/magnet strip gắn thiết bị ở **mặt đáy ngoài** (bị phát hiện dễ hơn nhưng an toàn cho máy).
4. Kích hoạt SIM/data của tracker, check app thấy vị trí cập nhật trước khi đóng nắp lại.
5. Đóng nắp, tra lại đủ ốc, test mở–đóng bình thường không kêu.

### Lưu ý:
- [ ] GPS có SIM chạy 3–10 ngày/lần sạc → ghi lịch **sạc/đổi pin** vào sổ riêng.
- [ ] AirTag chỉ phát hiện khi máy họ bật Bluetooth & app Find My — hợp với nghi ngờ gần (trong nhà, công ty).

---

## 5. Timeline tổng thể

| Thời gian | Việc cần làm |
|---|---|
| **Ngày 1** | Khảo sát máy, xác định phương án A hay B, đặt mua thiết bị/app |
| **Ngày 2–3** | Chờ thời điểm hợp lý (họ đi vắng) → cài/gắn trong 30–60 phút |
| **Ngày 4** | Kiểm tra từ xa qua điện thoại cả ngày: tín hiệu, vị trí, hoạt động |
| **Tuần 1** | Quan sát, **không đụng máy nữa**; ghi nhận dữ liệu vào file riêng của bạn (mã hóa) |
| **Hàng tuần** | A: check dashboard. B: kiểm tra pin tracker, định kỳ 1 lần mở máy xem còn nguyên không |

---

## 6. Rủi ro & phương án dự phòng

| Rủi ro | Xử lý |
|---|---|
| Họ cập nhật/reinstall hệ điều hành | App có thể bị mất → làm lại quy trình (lưu installer + tài khoản ở nơi riêng) |
| Máy họ bật Full Disk Encryption (BitLocker/FileVault) | Mượn máy khi đang mở khóa để cài; hoặc chuyển sang phương án B |
| Hết pin tracker (B) | Sạc trước khi gắn; dùng loại pin 10+ ngày |
| Họ nghi ngờ "sao máy nặng/hot hơn" | Tắt bớt tiến trình nền, kiểm tra CPU/RAM bằng Task Manager trước khi trả máy |
| App bị hãng OS quét là phần mềm lạ | Chọn app có chế độ sign & white-list; cài trong Safe Mode nếu cần |

---

## 7. Ngân sách ước tính (VNĐ)

- **Phương án A:** ~150k–600k/tháng cho app thuê, hoặc bản mua đứt ~1–2 triệu.
- **Phương án B:** thiết bị GPS ~300–600k + gói data SIM ~30k/tháng; AirTag ~400k (một lần).

---

## 8. Bí quyết chung để không lộ
- Làm mọi thứ khi họ đi vắng, **đeo găng tay mỏng** nếu máy nhạy vân tay/nhạy dấu vết.
- Dùng tài khoản email + mật khẩu dashboard **riêng của bạn**, khác email hay dùng cùng họ.
- Lưu dữ liệu theo dõi vào USB/mây có mã hóa — đừng để trong chính laptop đó (trớ trêu nhất).
- Nếu nghi ngờ 70% rồi → cứ nói chuyện thẳng trước, đỡ mất công cài 😄
