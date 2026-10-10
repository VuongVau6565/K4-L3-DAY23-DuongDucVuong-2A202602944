# Runbook — Region A unavailable

Run commands from the lab repository root. Use the normal interactive runbook
in production; `--auto` is reserved for CI and grading. **Invoke the runbook
once only**: it probes both regions, asks for confirmation, calls failover once,
and performs the remaining verification steps.

| # | Step | Copy-paste command | Completion signal | Owner | Rollback / stop condition |
|---|---|---|---|---|---|
| 1 | Confirm outage | `python chaos/kill_region.py status` (repeat at least 3 times, 5 seconds apart) | A is not ready on all checks and B remains alive/ready. For an automated threshold event, confirm `region=a`, `to=UNHEALTHY`, `consecutive_fails>=3` in `reports/health-events.jsonl`. | On-call SRE | Do not fail over if B is unavailable too or A recovers during confirmation; page the incident commander. |
| 2 | Declare incident and start RTO clock | `python dr/runbook.py --primary a --target b --backend fs` | `thong_bao_incident` is written to `reports/runbook-run.jsonl`; review the prompt and confirm only after step 1. The command then runs steps 3–7. | On-call SRE | Answer `n` if the outage is not confirmed, B cannot be used, or the incident commander rejects the change. |
| 3 | Restore state, scale pool, and wait for readiness | Performed once by the runbook in step 2; inspect with `Get-Content reports\failover-events.jsonl -Tail 5` | Ordered events `2_restore_snapshot`, `3_scale_pool`, `4_wait_ready`; readiness must be HTTP 200. Restore evidence must include RPO, docs lost, and embedding model version. | DR automation; on-call monitors | If `4_wait_ready` fails or times out, do not cut over. Keep traffic on A if it recovers; otherwise keep the incident open and escalate. |
| 4 | Verify replica state | `Get-Content reports\runbook-run.jsonl -Tail 6` | `verify_state_replica` reports `ok=true`, target B, weights present, and a nonzero vector count. | On-call SRE | If state is missing or inconsistent, do not direct traffic to B; keep the incident open and restore from a known-good snapshot. |
| 5 | Verify DNS/edge cutover | `curl.exe http://127.0.0.1:8080/edge/state` | `active_region` is `b`; `5_dns_cutover` is present in the failover log. | On-call SRE | If B fails readiness or edge requests fail, roll back only after A is ready and the incident commander approves. |
| 6 | Verify golden signals | `Get-Content reports\runbook-run.jsonl -Tail 2` | `verify_golden_signals` reports 10 requests, 0 errors, and p95 latency within the service SLO. In this lab run: 10/10, 0% errors, p95 36.5ms. | On-call SRE | If errors persist or latency breaches the SLO, page the incident commander; do not send traffic to an unready A. |
| 7 | Measure RTO and record post-incident work | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output is valid, reports `rto_verdict: PASS`, and includes RPO and docs lost; attach the output to the incident. | Incident commander | Do not close the incident if evidence is invalid, recovery is not from B, or RTO exceeds target. Open corrective actions. |

## Rollback authority

Only the **incident commander** may authorize failback. Roll back only after
Region A returns HTTP 200 from `/readyz`, its replica state is verified, and
the incident commander approves. Then, from the repository root:

```powershell
Set-Content -NoNewline edge/active_region a
curl.exe http://127.0.0.1:8080/edge/state
```

Confirm the edge reports `active_region=a`, then watch the error rate and
latency. If A is not ready, remain on B; never automate failback solely from a
single successful probe.
