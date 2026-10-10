"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append and print one timestamped failover event."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as events_file:
        events_file.write(json.dumps(record) + "\n")
    print("FAILOVER", json.dumps(record))
    return record


def state_of(region: str) -> dict:
    """Read the target region's serving state."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Run the ordered failover steps, cutting over only after readiness."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait < 0:
        raise ValueError("wait must not be negative")

    completed_steps = []
    result = {"ok": False, "steps": completed_steps}

    try:
        target_state = state_of(target)
        if not isinstance(target_state, dict) or target_state.get("region") != target:
            raise ValueError(
                f"region state endpoint returned an invalid identity; expected {target!r}"
            )
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        emit(step="1_verify_target", ok=False, target=target, reason=str(exc))
        result["reason"] = str(exc)
        return result

    emit(step="1_verify_target", ok=True, target=target, state=target_state)
    completed_steps.append("1_verify_target")

    metadata = snapshot.get(target, backend)
    source_region = "a" if target == "b" else "b"
    rpo_data = snapshot.rpo(
        pathlib.Path(f"state/region-{source_region}/vectors.sqlite"),
        pathlib.Path(f"state/region-{target}/vectors.sqlite"),
    )
    restore_data = {
        "rpo_seconds": rpo_data["rpo_seconds"],
        "docs_lost": rpo_data["docs_lost"],
        "embed_model_version": metadata["embed_model_version"],
    }
    result.update(restore_data)
    emit(step="2_restore_snapshot", ok=True, target=target, **restore_data)
    completed_steps.append("2_restore_snapshot")

    pool_state_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_state_file.parent.mkdir(parents=True, exist_ok=True)
    pool_state_file.write_text("full\n", encoding="utf-8")
    emit(step="3_scale_pool", ok=True, target=target, pool_state="full")
    completed_steps.append("3_scale_pool")

    deadline = time.monotonic() + wait
    ready = False
    ready_reason = "readiness check timed out"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=min(2.0, remaining),
            )
            if response.status_code == 200:
                ready = True
                ready_reason = "HTTP 200"
                break
            ready_reason = f"HTTP {response.status_code}"
        except httpx.RequestError as exc:
            ready_reason = str(exc)

        time.sleep(min(0.2, max(0, deadline - time.monotonic())))

    if not ready:
        emit(step="4_wait_ready", ok=False, target=target, reason=ready_reason)
        result["reason"] = ready_reason
        return result

    emit(step="4_wait_ready", ok=True, target=target, reason=ready_reason)
    completed_steps.append("4_wait_ready")

    active_region_file = pathlib.Path("edge/active_region")
    active_region_file.parent.mkdir(parents=True, exist_ok=True)
    active_region_file.write_text(f"{target}\n", encoding="utf-8")
    emit(step="5_dns_cutover", ok=True, target=target)
    completed_steps.append("5_dns_cutover")

    result["ok"] = True
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
