# Postmortem — DR Drill Lab 23

Diễn tập ngày 09/10/2026, localhost, Ubuntu WSL bare mode / fs. Xem xét điều kiện hệ thống khiến outage ảnh hưởng tới inference, không quy lỗi cá nhân. Thời gian bảng là Asia/Bangkok (UTC+7).

## 1. Timeline

| Thời gian ISO | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T11:26:29.894+07:00 | Outage bắt đầu | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T11:26:30.307+07:00 | Gửi request lỗi đầu tiên | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T11:26:44.622+07:00 | Health checker alert A UNHEALTHY | `reports/health-events.jsonl:2` |
| 2026-10-09T11:26:46.716+07:00 | Operator mở incident; --auto xác nhận cho drill | `reports/runbook-run.jsonl:2` |
| 2026-10-09T11:26:46.850+07:00 | Snapshot restore hoàn tất | `reports/failover-events.jsonl:2` |
| 2026-10-09T11:26:53.339+07:00 | B ready | `reports/failover-events.jsonl:4` |
| 2026-10-09T11:26:53.347+07:00 | DNS cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-10-09T11:26:58.688+07:00 | Resolved: request đầu tiên OK từ B qua edge | `reports/drill-2-withdr.jsonl:40` |

Thông báo incident trễ 16.823s so với outage; runbook ghi cả hai timestamp, không coi operator biết outage ngay tại mốc kill. Diễn tập dùng --auto; vận hành bình thường cần nhập y/N.

## 2. RTO/RPO và gap analysis

- RTO mục tiêu 300s; thực đo 28.8s; gap (thực đo − mục tiêu) = -271.2s, còn dư 271.2s. Evidence: `reports/drill-2-withdr.jsonl:40`, `chaos/chaos-events.jsonl:3`.
- RPO mục tiêu 300s; thực đo 4.00s / 2 document; gap = -296.00s, còn dư 296.00s. Mục tiêu theo giây đạt nhưng B vẫn thiếu dữ liệu. Evidence: `reports/failover-events.jsonl:2`.
- Thành phần lớn nhất: ngân sách health-check 15s, chiếm 52.1% RTO. Phát hiện thực tế 14.728s; warm-up/readiness 6.481s; TTL/request sampling 5.341s. Phân rã cộng đủ 28.8s ở `reports/rto-evidence.md`.
- Baseline: 16/32 request lỗi, NO_RECOVERY trong 40s; tự phục hồi chưa tồn tại. Evidence: `reports/drill-1-nodr.jsonl:17` đến `reports/drill-1-nodr.jsonl:32`.
- Golden signals: 10 request thật tới B, p95 63.37ms, error rate=0%; đây là kiểm tra nhanh, không đủ để suy ra SLA dài hạn. Evidence: `reports/runbook-run.jsonl:6`.

## 3. Root cause — 5 whys

1. Vì sao user nhận lỗi? Edge còn định tuyến A trong khi A bị SIGSTOP, upstream timeout. Evidence: `reports/drill-2-withdr.jsonl:26`.
2. Vì sao edge không chuyển ngay? Cache TTL 5s và file active_region chưa được thay; baseline không có quy trình đổi tuyến.
3. Vì sao không thể chuyển ngay sang B? Standby ban đầu pool warm, count=0, weights=false; process sống nhưng chưa inference được. Evidence: `reports/failover-events.jsonl:1`.
4. Vì sao có thể phục hồi? Replication đã có snapshot vectors + weights + embedding version; failover restore rồi scale và chờ ready trước cutover. Evidence: `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5`.
5. Vì sao vẫn có gap dữ liệu/thời gian? Replica bất đồng bộ mỗi 30s, GPU warm-up và threshold/TTL có độ trễ chủ ý; chúng chưa được thay bằng replica liên tục và hot standby.

Nếu outage thật làm mất filesystem A, phép đo RPO hiện tại không đọc được primary DB và snapshot sau outage không còn lấy được từ A. Lab SIGSTOP chỉ dừng serving: ingest và replication là process riêng, vẫn đọc/ghi filesystem A; snapshot dùng ở lần này xảy ra sau kill (`reports/replication.jsonl:2`). Vì vậy 4.00s / 2 document là RPO tại restore của mô phỏng này, không phải cam kết RPO cho mất cả region. Triển khai thật cần watermark ingest ngoài primary và snapshot ở miền lỗi độc lập. Restore lỗi/target chưa ready phải abort để không đưa traffic tới standby chưa phục vụ được.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến / cách xác minh |
|---|---|---|---|---|
| 1 | Drill interval 1s, giữ threshold 3 và operator confirm; kiểm tra false positives | On-call SRE | 10/10/2026 | ngân sách detection giảm 15s → 3s, tiết kiệm lý thuyết 12s; đo lại từ log |
| 2 | Thử replication mỗi 5s và watermark ở vùng lỗi độc lập | Data engineer | 12/10/2026 | cửa sổ replication lý thuyết giảm 30s → 5s; đo lại RPO và docs_lost |
| 3 | Drill A mất dữ liệu; xét hot standby full và quy trình đối soát failback | Platform SRE + incident commander | 13/10/2026 | kiểm chứng restore không cần A; hot standby có thể bỏ phần warm-up khoảng 6.481s, đổi lại chi phí compute |

## 5. Câu hỏi bắt buộc và reflection

1. interval×threshold = 5s×3 = 15s, chiếm 52.1% RTO. Theo ngân sách lab, để RTO≤300s thì interval phải nhỏ hơn (300s − thời gian restore/warm-up/TTL/điều phối)/3; không chọn 100s vì sẽ hết ngân sách trước phục hồi. Cấu hình thực tế 5s để dư thời gian cho các bước khác. Pha poll khiến thời điểm quan sát lệch nhẹ quanh ngân sách này.
2. Hạ interval từ 5s xuống 1s giảm ngân sách detection 12s, RTO lý thuyết khoảng 16.8s nếu phần khác giữ nguyên; đây chưa phải số đo. Tần suất probe tăng khoảng 5 lần, ba lỗi liên tiếp trong cửa sổ ngắn dễ phản ánh nhiễu hơn. Với timeout 2s, poll tuần tự có thể dài hơn interval 1s nên không bảo đảm tiết kiệm đủ 12s. Giữ threshold, xác nhận và kiểm tra false positives trước áp dụng.
3. Nếu outage 6 giờ và A mất dữ liệu vĩnh viễn, 2 document thiếu tại restore đại diện các ticket/tri thức không có trên B; cần replay từ nguồn ingest độc lập hoặc đối soát với khách hàng. Đây là số tại thời điểm restore, không tự suy rộng ra toàn bộ dữ liệu phát sinh trong 6 giờ.
4. Trước triển khai DR, không component nào tự phát hiện và đổi tuyến: edge chỉ trả lỗi upstream. B chưa có vectors/weights nên đổi pointer ngay sẽ trả region_not_ready. Health checker chạy trong process độc lập, không import serving; khi serving chết nó vẫn phát alert.
5. Cách giảm RTO ít tăng flapping: giữ B full để bớt warm-up, hoặc giảm TTL, đổi lại chi phí compute/refresh. Để chứng minh mục tiêu 5 phút, mở `reports/drill-2-withdr.jsonl` cùng chaos/health/failover logs và chạy công cụ đo; không lấy thời gian runbook kết thúc thay RTO của user.
