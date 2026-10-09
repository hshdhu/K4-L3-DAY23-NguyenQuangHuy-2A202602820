"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước runbook có timestamp vào JSONL và stdout."""
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Yêu cầu xác nhận, trừ chế độ auto dành cho drill/CI."""
    return auto or input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Xác nhận outage, thực hiện failover một lần và kiểm tra phục hồi."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("Primary and target must be distinct regions a/b")
    started = time.monotonic()
    for attempt in range(3):
        ready, reason = hc.probe(primary, 2)
        try:
            alive = httpx.get(f"{URL[target]}/healthz", timeout=2).status_code == 200
        except httpx.HTTPError:
            alive = False
        if ready or not alive:
            step(1, "xac_nhan_outage", ok=False, reason=reason, target_alive=alive)
            return {"ok": False, "reason": "Outage unconfirmed or target unavailable"}
        if attempt < 2:
            time.sleep(5)
    step(1, "xac_nhan_outage", ok=True, primary=primary, target=target,
         consecutive_fails=3, interval_s=5)
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    kills = [json.loads(line) for line in chaos.read_text().splitlines() if line.strip()] if chaos.exists() else []
    outage = next((event["ts"] for event in reversed(kills)
                   if event.get("action") == "kill" and event.get("region") == primary), None)
    step(2, "thong_bao_incident", t_outage=outage, primary=primary, target=target)
    if not confirm(auto, f"Failover {primary} -> {target}?"):
        return {"ok": False, "reason": "Operator declined"}
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", **result)
    if not result.get("ok"):
        return result
    step(4, "verify_state_replica", count=result["state"]["count"],
         weights=result["state"]["weights"], pool_state=result["state"]["pool_state"],
         embed_model_version=result["embed_model_version"],
         rpo_seconds=result["rpo_seconds"], docs_lost=result["docs_lost"])
    step(5, "dns_cutover", ok=result["ok"], target=target)
    latencies, errors = [], 0
    with httpx.Client(timeout=3) as client:
        for _ in range(10):
            request_start = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer")
                errors += response.status_code != 200 or response.json().get("region") != target
            except (httpx.HTTPError, ValueError):
                errors += 1
            latencies.append((time.monotonic() - request_start) * 1000)
    p95 = sorted(latencies)[9]
    golden_ok = errors == 0 and p95 < 1000
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=round(p95, 2),
         error_rate=errors / 10, ok=golden_ok)
    step(7, "post_incident", elapsed_s=round(time.monotonic() - started, 2),
         ok=golden_ok, measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {**result, "ok": golden_ok, "p95_latency_ms": p95, "error_rate": errors / 10}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
