# Postmortem — DR Drill 2, 2026-10-10

This was a planned Region A outage injection, not a production incident. The
drill measures the recovery system; it does not establish the cause of any
real-world Region A failure. The review is blameless and focuses on system
behavior and process timing.

## 1. Timeline

| ISO time (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-10 07:59:26 | Region A outage injected; Region B was alive | `chaos/chaos-events.jsonl:16` |
| 2026-10-10 07:59:26 | First user request failed (`ConnectTimeout`), `+0.7s` by request timestamp | `reports/drill-2-withdr.jsonl:43` |
| 2026-10-10 07:59:43 | Health checker marked A `UNHEALTHY`, after 3 consecutive failures (`+17.3s`) | `reports/health-events.jsonl:7` |
| 2026-10-10 07:59:50 | Runbook announced the incident and started its clock (`+24.6s`) | `reports/runbook-run.jsonl:23` |
| 2026-10-10 07:59:50 | Snapshot restore completed; measured RPO `18.0s`, `9` docs lost (`+24.6s`) | `reports/failover-events.jsonl:33` |
| 2026-10-10 07:59:57 | B became ready and edge cutover completed (`+30.9s`) | `reports/failover-events.jsonl:35-36` |
| 2026-10-10 08:00:01 | First successful request served by B; service recovered (`+35.4s`) | `reports/drill-2-withdr.jsonl:60` |

## 2. RTO/RPO measured vs. target — gap analysis

- **RTO target:** `300s`; measured: `35.4s`; headroom: `264.6s` (**PASS**).
- **RPO target:** `300s`; measured: `18.0s`; `9` documents lost; headroom: `282.0s`.
- **Longest measured interval:** outage to health detection, `17.3s`. The
  configured detection floor is `5s × 3 = 15s`. A further `7.3s` elapsed from
  the health event to the runbook incident notification. B's warm pool then
  needed `6.2s` to become ready. The first successful traffic sample followed
  cutover by `4.5s` (edge propagation plus request sampling, not pure DNS TTL).
- **Customer impact in the capture:** 17 failed requests after the kill; the
  first failed request was at `+0.7s`. After recovery, the runbook's 10 golden
  requests all succeeded, with `0%` errors and `36.5ms` p95.

The RTO headroom is not a reason to ignore the delays: the on-call handoff and
warm-up account for most of the time after detection. The RPO is consistent
with a 30-second replication cadence and a live ingest rate of 0.5 documents
per second; the measured snapshot lag at restore was 18 seconds.

## 3. Root cause analysis (5 whys)

1. **Why did users receive errors?** Region A stopped serving requests after
   the planned outage injection; the edge continued routing to A until the
   cutover. The first failed request is recorded in
   `reports/drill-2-withdr.jsonl:43`.
2. **Why did the edge not move immediately?** The design requires three
   consecutive failed readiness probes to avoid flapping; the detector
   recorded that transition at `reports/health-events.jsonl:7`.
3. **Why did recovery take another 18.1 seconds after detection?** The
   runbook's incident notification was logged 7.3 seconds after detection;
   then the standby GPU pool required 6.2 seconds to become ready, followed
   by 4.5 seconds until the first successful traffic sample.
4. **Why did B require warm-up and have stale data?** The standby pool was
   intentionally kept `warm` rather than `full`, and snapshots were
   replicated every 30 seconds while A continued ingesting. This is the
   configured cost/RPO trade-off, not an operator error.
5. **Why are these trade-offs operationally visible?** The runbook is
   semi-automated and the lab has no alert-to-operator paging integration;
   compute warm-up and snapshot cadence are also explicit configuration
   choices. Those controls need owners and tested targets before production.

The drill does not identify why a real Region A might fail; it injects the
failure intentionally. Its actionable finding is that detection, operator
notification, warm-up, and edge observation all contribute to the measured
RTO.

## 4. Action items

| # | Action | Owner | Deadline | Expected RTO/RPO improvement |
|---|---|---|---|---|
| 1 | Connect the `UNHEALTHY` alert to the on-call notification and runbook confirmation flow; measure alert-to-start time in a repeat drill. | SRE / on-call tooling | 2026-10-17 | Target the observed 7.3s handoff gap to `<=2s` (about 5s RTO reduction); retain operator confirmation. |
| 2 | Evaluate reducing probe interval from 5s to 1s while retaining a 3-failure threshold; run transient-loss tests before adoption. | Platform SRE | 2026-10-24 | Theoretical detection floor falls from 15s to 3s (up to 12s); probe load rises 5x and false-positive/flapping risk must be measured. |
| 3 | Evaluate snapshot replication every 10s and validate RPO under concurrent ingest. | Data Platform | 2026-10-24 | Target RPO `<=10s`; reduces the configured maximum stale window by up to 20s versus a 30s cadence, subject to measured copy lag. |
| 4 | Measure warm-pool options and their cost; define an approved standby readiness SLO. | ML Infrastructure | 2026-10-31 | Current warm-up is 6.2s; any improvement depends on the selected capacity/cost trade-off. |

## 5. Required questions

1. `interval × threshold = 5s × 3 = 15s`, the theoretical detection floor.
   This is `42.4%` of the measured `35.4s` RTO. Actual detection was `17.3s`.
2. At a 1-second interval and the same threshold, the theoretical floor is
   `3s`, up to `12s` lower. It would require 5x as many probes and must be
   validated against transient failures to avoid flapping.
3. At restore time, `docs_lost=9` means nine ingested customer documents were
   absent from the Region B snapshot and would need replay or reconciliation
   if the primary copy were permanently lost. Region A was restarted after
   this drill, so the measurement does not claim those originals were
   permanently lost.
