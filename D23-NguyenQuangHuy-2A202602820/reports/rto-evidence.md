# RTO/RPO Evidence — Lab 23

Diễn tập ngày 09/10/2026, Ubuntu WSL, bare mode, netblock --mock, snapshot backend fs. Warm-up mặc định 6s, edge TTL mặc định 5s. Thời gian ISO bên dưới dùng Asia/Bangkok (UTC+7); phép đo dùng timestamp epoch trong log.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| Outage bắt đầu | 2026-10-09T11:25:38.791+07:00 | chaos kill A; B alive; không ép double outage | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên | +0.087s; latency 2027.0ms | timestamp gửi request và latency thật | `reports/drill-1-nodr.jsonl:17` |
| Request lỗi | 16/32 | đếm ok=false sau kill | `reports/drill-1-nodr.jsonl:17` đến `reports/drill-1-nodr.jsonl:32` |
| Phục hồi sau lỗi | Không có trong cửa sổ đo 40s | không còn request ok=true sau lỗi đầu | `reports/drill-1-nodr.jsonl:17` đến `reports/drill-1-nodr.jsonl:32` |
| RTO | NO_RECOVERY | công cụ đo không tìm được timestamp phục hồi | `reports/drill-1-nodr.jsonl:32`, `chaos/chaos-events.jsonl:1` |

Kiểm tra: `python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300`. Cảnh báo thiếu health/cutover trong baseline là đúng vì chưa chạy DR. NO_RECOVERY chỉ chứng minh không phục hồi trong cửa sổ đo.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| Outage bắt đầu | 0s; 2026-10-09T11:26:29.894+07:00 | kill A bằng SIGSTOP; B alive=true; forced_both=false | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.413s | timestamp gửi request lỗi đầu; lỗi thực trả sau latency trong dòng log | `reports/drill-2-withdr.jsonl:26` |
| Health checker phát hiện | 14.728s | A UNHEALTHY sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| Operator mở incident / xác nhận tự động | 16.823s | ts thông báo trừ t_outage; drill dùng --auto | `reports/runbook-run.jsonl:2` |
| Snapshot restore xong | 16.956s | timestamp bước 2 | `reports/failover-events.jsonl:2` |
| Region B ready | 23.446s | /readyz=200; count=216; weights=true; pool=full | `reports/failover-events.jsonl:4` |
| DNS cutover | 23.453s | bước 5 sau bước 4 | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **28.8s**; timestamp thô 28.794s | request ok=true đầu sau lỗi, served_by=b | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu | Verdict / Evidence |
|---|---|---|---|
| RTO — Inference API | 28.8s | 300s | PASS; `reports/drill-2-withdr.jsonl:40` |
| RPO — Vector DB | 4.00s / 2 document | 300s | PASS về giây; `reports/failover-events.jsonl:2` |
| Golden signals trực tiếp tới B | 10 request; p95 63.37ms; error rate=0% | p95<1000ms; error rate=0 | PASS; `reports/runbook-run.jsonl:6` |

RPO = primary_latest_doc_ts (1791520006.3112447) − restored_latest_doc_ts (1791520002.3105762), làm tròn 4.00s. docs_lost là số document có ingested_at lớn hơn watermark restore, không phải tuổi snapshot. Phiên bản embedding được restore: embed-model=vi-e5-base@v3. Evidence: `reports/failover-events.jsonl:2`; snapshot nguồn: `reports/replication.jsonl:2`.

Drill 2 ghi 157 request, có 14 request lỗi sau outage; lần phục hồi do B phục vụ. Evidence: `reports/drill-2-withdr.jsonl:26` đến `reports/drill-2-withdr.jsonl:40` và `reports/drill-2-withdr.jsonl:157`.

## 3. Phân rã RTO

| Thành phần | Giây | Nguồn / cách tính | Cách giảm |
|---|---|---|---|
| Health-check detection floor theo cấu hình lab | 15.000s | interval 5s × threshold 3; `reports/health-events.jsonl:2` | giảm interval, giữ threshold và operator confirm |
| Snapshot restore thực tế | 0.092s | elapsed_s của restore + đo RPO; `reports/failover-events.jsonl:2` | replica gần nguồn, giảm kích thước snapshot |
| GPU pool warm-up + chờ readiness | 6.481s | waited_s; `reports/failover-events.jsonl:4` | giữ standby full, đổi lại tăng chi phí |
| DNS/LB TTL và request lấy mẫu | 5.341s | t_recovered − t_cutover; `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:40` | giảm TTL, đo chi phí refresh |
| Xác nhận/verify/ghi log và sai lệch pha poll, làm tròn | 1.886s | phần còn lại = RTO − bốn thành phần trên; `reports/runbook-run.jsonl:1`, `reports/failover-events.jsonl:1`, `reports/drill-2-withdr.jsonl:40` | tránh xác nhận lặp lại khi alert đã đủ threshold |
| **Tổng** | **28.800s** | tổng các dòng khớp RTO công cụ đo, làm tròn 0.1s | |

15s là ngân sách detection interval×threshold dùng trong lab; thời điểm phát hiện đo được 14.728s lệch nhẹ do pha polling và timeout. TTL cấu hình 5s không có nghĩa mọi request quan sát đúng 5s: loadgen tuần tự còn chịu timeout và lịch request. Phần điều phối được ghi riêng, không gán vào thời gian copy snapshot.

Tái kiểm tra: `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`. Kết quả valid=true, warnings=[], recovered_by_region=b, PASS. Trong cửa sổ drill, log failover có đúng năm bước verify → restore → scale → ready → cutover (dòng 1–5); các dòng ngoài cửa sổ này do unit test mock không được dùng làm evidence diễn tập.
