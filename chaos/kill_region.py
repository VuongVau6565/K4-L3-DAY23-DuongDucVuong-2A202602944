"""Chaos script: giết 1 region. [Đã tối ưu tương thích hoàn toàn với Windows]"""
import argparse
import json
import os
import pathlib
import subprocess
import time

import httpx

EVENTS = pathlib.Path("chaos/chaos-events.jsonl")
PID_DIR = pathlib.Path("run")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def event(**kw):
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with EVENTS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("CHAOS", json.dumps(rec))
    return rec


def is_ready(region: str, timeout=1.5) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=timeout).status_code == 200
    except Exception:
        return False


def is_alive(region: str, timeout=1.5) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


def pid_of(region: str) -> int | None:
    f = PID_DIR / f"region-{region}.pid"
    if f.exists():
        try:
            pid = int(f.read_text(encoding='utf-8').strip())
            if os.name == 'nt':
                # Kiểm tra tiến trình trên Windows có đang tồn tại không
                res = subprocess.run(f"tasklist /FI \"PID eq {pid}\"", shell=True, capture_output=True, text=True)
                if str(pid) in res.stdout:
                    return pid
            else:
                os.kill(pid, 0)
                return pid
        except Exception:
            pass

    # Fallback tự động dò qua port nếu file PID không khớp
    port = 8001 if region == 'a' else 8002
    try:
        res = subprocess.run(f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True)
        for line in res.stdout.strip().split('\n'):
            if 'LISTENING' in line:
                parts = line.strip().split()
                return int(parts[-1])
    except Exception:
        pass
    return None


def kill(region: str, mode: str, backend: str, force_both: bool, mock: bool):
    other = "b" if region == "a" else "a"
    other_alive = is_alive(other)
    if not other_alive and not force_both:
        event(action="refused", region=region, mode=mode,
              reason=f"region-{other} khong phan hoi /healthz -> giet region-{region} nua "
                     f"la double outage, RTO do duoc se vo nghia")
        raise SystemExit(
            f"CHAN LAI: region-{other} dang khong sống. Chạy `restore --region {other}` trước.\n"
            f"(Muốn ép: --i-really-want-both, nhưng drill sẽ bị đánh dấu INVALID.)")

    ev = event(action="kill", region=region, mode=mode, backend=backend, mock=mock,
               other_region=other, other_alive=other_alive, forced_both=force_both,
               note="t_outage_start — moc 0 cua RTO clock")
    
    if backend == "bare":
        pid = pid_of(region)
        if pid is None:
            raise SystemExit(f"khong tim thay PID cua region-{region} trong {PID_DIR}")
        
        if os.name == 'nt':
            # Trên Windows, dùng taskkill để ép dừng tiến trình (tương đương stop/netblock)
            subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
        else:
            import signal
            os.kill(pid, signal.SIGSTOP if mode == "netblock" else signal.SIGKILL)
    else:
        svc = f"serving-{region}"
        if mode == "stop":
            subprocess.run(["docker", "compose", "stop", svc], check=True)
        else:
            subprocess.run(["docker", "exec", "--privileged", svc, "iptables", "-A", "INPUT",
                            "-p", "tcp", "--dport", "8000", "-j", "DROP"], check=True)
    return ev


def restore(region: str, backend: str):
    if backend == "bare":
        return event(action="restore", region=region, method="need_manual_start",
                   note="Tren Windows, process da bi dung, hay chay lai lenh uvicorn cua region do")
    subprocess.run(["docker", "compose", "start", f"serving-{region}"], check=False)
    subprocess.run(["docker", "exec", "--privileged", f"serving-{region}", "iptables", "-F",
                    "INPUT"], check=False)
    return event(action="restore", region=region, method="docker_start+iptables_flush")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", nargs="?", default="kill", choices=["kill", "restore", "status"])
    p.add_argument("--region", default="a", choices=["a", "b"])
    p.add_argument("--mode", default="netblock", choices=["stop", "netblock"])
    p.add_argument("--backend", default=None, choices=["bare", "docker"])
    p.add_argument("--mock", action="store_true",
                   help="pin tham so thoi gian -> cham diem reproducible; ham y --backend bare")
    p.add_argument("--i-really-want-both", action="store_true")
    a = p.parse_args()
    backend = a.backend or ("bare" if a.mock else "docker")
    if a.cmd == "status":
        print(json.dumps({r: {"alive": is_alive(r), "ready": is_ready(r)} for r in "ab"}, indent=2))
    elif a.cmd == "restore":
        restore(a.region, backend)
    else:
        kill(a.region, a.mode, backend, a.i_really_want_both, a.mock)