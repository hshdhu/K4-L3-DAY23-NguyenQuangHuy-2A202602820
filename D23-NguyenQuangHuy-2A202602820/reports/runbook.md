# Runbook — Region A down

Chạy từ thư mục gốc repo lab trên Linux/Ubuntu WSL, với dependencies trong `requirements.txt`. Trên máy này: `source .venv/bin/activate`. A là primary, B là standby; chỉ một on-call thực hiện runbook tại một thời điểm.

**Điều kiện trước khi chuyển vùng:** B còn sống, snapshot fs đã tồn tại, health checker và traffic đang ghi log. Kiểm tra snapshot: `python3 state/snapshot.py lag --backend fs`; nếu chưa có snapshot, dừng và báo Data engineer. Dùng snapshot từ primary đã được replication trước incident; không tự seed lại khi đang xử lý outage.

| # | Bước | Lệnh copy-paste | Biết xong khi | Ai | Điều kiện dừng / rollback |
|---|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status --backend bare` | A không trả lời, B alive; bước 1 của runbook kiểm tra A lỗi 3 lần liên tiếp, cách nhau 5s | On-call SRE | A vẫn ready hoặc B không alive: dừng, không chuyển tuyến |
| 2 | Mở incident và xác nhận | `python3 dr/runbook.py --primary a --target b --backend fs` | Log `thong_bao_incident` có ts và t_outage; nhập `y` để tiếp tục | On-call; incident commander phê duyệt | Chưa được duyệt: nhập `n`, giữ nguyên tuyến |
| 3 | Restore, scale pool và chờ ready | `cat reports/failover-events.jsonl` | Lệnh bước 2 đã thực hiện đúng một lần: verify → restore có RPO/docs_lost/model version → pool full → ready=true | Platform SRE | Restore lỗi hoặc quá 60s chưa ready: abort, không DNS cutover |
| 4 | Verify state replica | `curl -fsS http://127.0.0.1:8002/v1/state` | weights=true, count>0, pool_state=full; `verify_state_replica` ghi cùng state và embedding version | Data/ML engineer | State thiếu hoặc version sai: giữ incident mở, commander quyết định rollback theo điều kiện dưới |
| 5 | Xác nhận DNS/LB cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | Sau cache TTL 5s, active_region=b; log `5_dns_cutover` sau ready | Platform SRE | Tuyến không đổi hoặc B lỗi: kiểm tra pointer/cache, không đổi về A khi A chưa ready |
| 6 | Verify golden signals | `cat reports/runbook-run.jsonl`; `curl -fsS http://127.0.0.1:8080/v1/infer` | Bước 6 gửi 10 request thật tới B: error_rate=0, p95<1000ms, ok=true; request qua edge do B phục vụ | On-call SRE | Golden signals lỗi: báo commander, chỉ failback khi A đã phục hồi và đối soát dữ liệu |
| 7 | Đo RTO và postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Sau loadgen kết thúc: valid=true, warnings=[], PASS, recovered_by_region=b, RPO/docs_lost có giá trị; hoàn tất reports | Incident commander | Không PASS hoặc evidence không hợp lệ: chưa đóng incident, xử lý nguyên nhân và diễn tập lại |

Lệnh ở bước 2 tự thực hiện cả bảy bước; bước 3–6 trong bảng là lệnh đối chiếu, **không gọi lại failover**. `--auto` chỉ dùng cho drill/CI. Health checker phải chạy độc lập: `python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl`.

**Rollback/failback:** On-call đề xuất nếu B lỗi hoặc phục vụ không ổn định; **incident commander có quyền phê duyệt**. Nếu A còn lỗi hoặc chưa đồng bộ dữ liệu, không trả traffic về A. Không tự động chuyển qua lại.

1. Khôi phục A bị netblock: `python3 chaos/kill_region.py restore --region a --backend bare`. Nếu A đã SIGKILL, khởi động riêng process A trước.
2. Data engineer đối soát A/B, xử lý ghi mới ở B; kiểm tra `curl -fsS http://127.0.0.1:8001/readyz` ba lần cách nhau 5s. Chưa đủ điều kiện thì dừng.
3. Sau phê duyệt và đối soát: `python3 state/snapshot.py put --region b --backend fs`, rồi `python3 dr/failover.py --target a --backend fs --wait 60`. Nếu restore/ready lỗi: abort, giữ tuyến B. Xác nhận edge phục vụ A sau TTL và theo dõi error rate/latency.

**Kiểm tra bài nộp:** chạy `python -m pytest tests/ -v` từ repo lab. Thư mục nộp chứa phần làm bài và evidence; để chạy lại, đặt các file vào đúng đường dẫn của repo starter. Giữ nguyên log của diễn tập đã dùng trong reports.

Trên PowerShell/Python Windows của máy này, đặt `$env:PYTHONUTF8 = '1'` trước lệnh pytest để đọc đúng reports tiếng Việt. Nếu sandbox chặn thư mục tạm của pytest, chạy từ terminal PowerShell bình thường ngoài sandbox.

Nếu script Bash của checkout Windows có CRLF, chạy `bash -c "$(tr -d '\r' < scripts/up_bare.sh)" scripts/up_bare.sh`; tương tự cho down_bare. Kết thúc drill: restore A, rồi `bash scripts/down_bare.sh` để dừng services.
