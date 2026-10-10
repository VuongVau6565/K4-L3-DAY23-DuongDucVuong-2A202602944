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
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append and print one timestamped runbook event."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as events_file:
        events_file.write(json.dumps(record) + "\n")
    print("RUNBOOK", json.dumps(record))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit affirmative response unless running in auto mode."""
    if auto:
        return True
    try:
        answer = input(f"{msg} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the incident runbook and record each completed decision."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be region 'a' or 'b'")
    if primary == target:
        raise ValueError("primary and target must be different regions")

    from dr import health_checker

    result = {"ok": False, "primary": primary, "target": target, "steps": []}
    run_started = time.time()

    def record(n, name, **data):
        step(n, name, **data)
        result["steps"].append(name)

    probe_results = {region: [] for region in (primary, target)}
    for _ in range(3):
        for region in (primary, target):
            ready, reason = health_checker.probe(region, 2.0)
            probe_results[region].append({"ready": ready, "reason": reason})
        time.sleep(0.1)

    outage_confirmed = all(
        not probe["ready"] for probe in probe_results[primary]
    )
    target_ready_count = sum(
        probe["ready"] for probe in probe_results[target]
    )
    record(
        1,
        "xac_nhan_outage",
        primary=primary,
        target=target,
        outage_confirmed=outage_confirmed,
        probes=probe_results,
    )
    if not outage_confirmed:
        result["reason"] = f"outage of region {primary} was not confirmed"
        result["elapsed_s"] = round(time.time() - run_started, 3)
        record(7, "post_incident", **{
            "ok": False,
            "reason": result["reason"],
            "elapsed_s": result["elapsed_s"],
        })
        return result

    incident_started = time.time()
    outage_ts = None
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_log.exists():
        with chaos_log.open(encoding="utf-8") as events_file:
            for line in events_file:
                event = json.loads(line)
                if event.get("action") == "kill" and event.get("region") == primary:
                    outage_ts = event.get("ts")

    record(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        incident_ts=incident_started,
        target_ready_probes=target_ready_count,
    )

    if not confirm(
        auto,
        f"Region {primary} failed 3 consecutive readiness probes. "
        f"Proceed with failover to region {target}?",
    ):
        result["reason"] = "operator declined failover"
        for n, name in (
            (3, "scale_gpu_pool"),
            (4, "verify_state_replica"),
            (5, "dns_cutover"),
            (6, "verify_golden_signals"),
        ):
            record(n, name, skipped=True, reason=result["reason"])
        result["elapsed_s"] = round(time.time() - incident_started, 3)
        record(
            7,
            "post_incident",
            ok=False,
            reason=result["reason"],
            elapsed_s=result["elapsed_s"],
        )
        return result

    failover_result = fo.failover(target, backend, wait=60.0)
    result["failover"] = failover_result
    failover_ok = bool(failover_result.get("ok"))
    record(
        3,
        "scale_gpu_pool",
        ok=failover_ok,
        failover_result=failover_result,
    )

    replica_state = None
    replica_ok = False
    replica_error = None
    if failover_ok:
        try:
            replica_state = fo.state_of(target)
            if not isinstance(replica_state, dict):
                replica_error = "region state endpoint returned a non-object response"
            else:
                replica_ok = bool(
                    replica_state.get("region") == target
                    and replica_state.get("weights")
                    and replica_state.get("count", 0) > 0
                )
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            replica_error = str(exc)
    record(
        4,
        "verify_state_replica",
        ok=replica_ok,
        failover_result={
            key: failover_result.get(key)
            for key in ("rpo_seconds", "docs_lost", "embed_model_version")
        },
        state=replica_state,
        reason=replica_error,
    )

    active_file = pathlib.Path("edge/active_region")
    active_region = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else None
    cutover_ok = failover_ok and active_region == target
    record(
        5,
        "dns_cutover",
        ok=cutover_ok,
        expected_region=target,
        active_region=active_region,
    )

    latencies_ms = []
    error_count = 0
    request_results = []
    for request_number in range(10):
        started = time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"runbook golden signal {request_number}"},
                timeout=3.0,
            )
            latency_ms = (time.monotonic() - started) * 1000
            latencies_ms.append(latency_ms)
            request_ok = response.status_code == 200
            if not request_ok:
                error_count += 1
            request_results.append({
                "status": response.status_code,
                "ok": request_ok,
                "latency_ms": round(latency_ms, 1),
            })
        except httpx.RequestError as exc:
            error_count += 1
            request_results.append({
                "status": None,
                "ok": False,
                "error": type(exc).__name__,
                "reason": str(exc),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            })

    sorted_latencies = sorted(latencies_ms)
    if sorted_latencies:
        p95_position = (len(sorted_latencies) - 1) * 0.95
        lower = math.floor(p95_position)
        upper = math.ceil(p95_position)
        p95_ms = sorted_latencies[lower] + (
            sorted_latencies[upper] - sorted_latencies[lower]
        ) * (p95_position - lower)
    else:
        p95_ms = None
    error_rate = error_count / 10
    golden_signals_ok = error_count == 0 and len(latencies_ms) == 10
    record(
        6,
        "verify_golden_signals",
        ok=golden_signals_ok,
        requests=10,
        successes=10 - error_count,
        error_count=error_count,
        error_rate=error_rate,
        p95_latency_ms=round(p95_ms, 1) if p95_ms is not None else None,
        results=request_results,
    )

    result["ok"] = failover_ok and replica_ok and cutover_ok and golden_signals_ok
    result["elapsed_s"] = round(time.time() - incident_started, 3)
    result["golden_signals"] = {
        "requests": 10,
        "error_count": error_count,
        "error_rate": error_rate,
        "p95_latency_ms": round(p95_ms, 1) if p95_ms is not None else None,
    }
    record(
        7,
        "post_incident",
        ok=result["ok"],
        elapsed_s=result["elapsed_s"],
        rto_command=(
            "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
            "--target-rto 300"
        ),
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
