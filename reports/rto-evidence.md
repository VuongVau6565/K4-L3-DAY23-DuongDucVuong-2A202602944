# RTO/RPO Evidence — Lab 23

All measurements below come from the JSONL evidence produced by the two drills.
Drill 2 was measured with `tools/measure_rto.py` using the load-generator,
chaos, health-check, and failover logs.

## 1. Drill 1 — no DR (baseline)

| Metric | Measured value | How measured | Evidence |
|---|---:|---|---|
| `t_outage` | `2026-10-10T07:01:38` | Region A kill | `chaos/chaos-events.jsonl:10` |
| First failed request | `+0.8s` | First `ok:false` after outage | `reports/drill-1-nodr.jsonl:22` |
| Recovery request | None | All requests through the end of the capture fail | `reports/drill-1-nodr.jsonl:36` |
| RTO verdict | `NO_RECOVERY` | No successful request from the surviving region | `reports/drill-1-nodr.jsonl:36` |

## 2. Drill 2 — DR enabled

| Milestone | Seconds from `t_outage` | Measurement | Evidence |
|---|---:|---|---|
| `t_outage` (zero) | `0.0s` | `action:kill`, Region A | `chaos/chaos-events.jsonl:16` |
| User's first failed request | `+0.7s` | First `ok:false` after the kill | `reports/drill-2-withdr.jsonl:43` |
| Health check detected outage | `+17.3s` | Region A changed to `UNHEALTHY` after 3 consecutive failures | `reports/health-events.jsonl:7` |
| Snapshot restore completed | `+24.6s` | `2_restore_snapshot`; RPO `18.0s`, 9 docs lost | `reports/failover-events.jsonl:33` |
| Region B ready | `+30.9s` | `/readyz` returned HTTP 200 | `reports/failover-events.jsonl:35` |
| DNS cutover | `+30.9s` | `5_dns_cutover` to Region B | `reports/failover-events.jsonl:36` |
| First successful request from B | `+35.4s` | First `ok:true` after the failure, served by B | `reports/drill-2-withdr.jsonl:60` |

| Metric | Measured | Target | Verdict |
|---|---:|---:|---|
| RTO — inference API | `35.4s` | `300s` (5 minutes) | **PASS** |
| RPO — vector DB | `18.0s` / `9` docs lost | `300s` (5 minutes) | **PASS** |

Runbook golden-signal check after cutover: 10/10 requests succeeded, error rate
`0%`, p95 latency `36.5ms` (`reports/runbook-run.jsonl:27`).

## 3. RTO breakdown

The measured RTO is outage-to-first-success (`35.4s`), not merely time-to-cutover.
The detector's theoretical floor is `interval_s × threshold = 5s × 3 = 15s`;
the observed detector transition was at `+17.3s`.

| Component | Observed time | Evidence / calculation | Potential reduction |
|---|---:|---|---|
| Health-check detection | `17.3s` (15.0s theoretical floor) | `5s × 3` in `reports/health-events.jsonl:7`; observed `UNHEALTHY` at `+17.3s` | Reduce interval only after evaluating added probe load and false-positive/flapping risk |
| Detection-to-runbook start | `7.3s` | Health event at `+17.3s`; incident notification at `+24.6s`, `reports/runbook-run.jsonl:23` | Page/trigger the on-call workflow directly from the health alert |
| Snapshot restore + scale command | `<0.1s` | `2_restore_snapshot` at `+24.6s`, `3_scale_pool` at `+24.6s`, `reports/failover-events.jsonl:33-34` | Already negligible in this run |
| GPU pool warm-up | `6.2s` | `3_scale_pool` to `/readyz` HTTP 200, `reports/failover-events.jsonl:34-35` | Keep capacity warm at additional resource cost, or optimize startup |
| Cutover-to-first-success observation | `4.5s` | Cutover at `+30.9s`; first successful traffic sample at `+35.4s`, `reports/failover-events.jsonl:36` and `reports/drill-2-withdr.jsonl:60` | Reduce edge cache/TTL and account for load-generator sampling cadence |

The observed intervals sum to approximately `35.4s` (rounding applies). The
last component includes edge propagation and the next successful traffic sample;
it is not a direct measurement of DNS TTL alone.
