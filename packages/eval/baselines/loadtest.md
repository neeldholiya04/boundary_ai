# Load test

Stub planner with 800 ms simulated LLM latency per call (two calls per request), 60 s per level, closed loop (each user sends its next request when the answer arrives), postgres database. Machine: macOS-27.0.1-arm64-arm-64bit, 8 CPUs, 8.6 GB RAM.

| Profile | Users | Requests | Req/s | p50 ms | p95 ms | p99 ms | Failed | Guard errors |
|---|---|---|---|---|---|---|---|---|
| `filters_off` | 1 | 28 | 0.48 | 2100 | 2100 | 2200 | 0 (0%) | 0 |
| `filters_off` | 2 | 54 | 0.94 | 2100 | 2200 | 2500 | 0 (0%) | 0 |
| `filters_off` | 4 | 104 | 1.76 | 2200 | 2800 | 2800 | 0 (0%) | 0 |
| `async` | 1 | 8 | 0.14 | 7000 | 9100 | 9100 | 0 (0%) | 0 |
| `async` | 2 | 19 | 0.27 | 6000 | 12000 | 12000 | 0 (0%) | 7 |
| `async` | 4 | 20 | 0.32 | 13000 | 15000 | 15000 | 2 (10%) | 3 |
| `blocking` | 1 | 7 | 0.13 | 7100 | 13000 | 13000 | 0 (0%) | 0 |
| `blocking` | 2 | 16 | 0.26 | 7400 | 12000 | 12000 | 0 (0%) | 3 |
| `blocking` | 4 | 22 | 0.33 | 12000 | 14000 | 19000 | 1 (5%) | 1 |

## Guard overhead per request (vs `filters_off`, same concurrency)

| Users | `async` p50 / p99 | `blocking` p50 / p99 | blocking − async p50 / p99 |
|---|---|---|---|
| 1 | +4900 / +6900 ms | +5000 / +10800 ms | +100 / +3900 ms |
| 2 | +3900 / +9500 ms | +5300 / +9500 ms | +1400 / +0 ms |
| 4 | +10800 / +12200 ms | +9800 / +16200 ms | -1000 / +4000 ms |

## Where the time goes: `async` (per check, all levels)

| Stage | Policy | Execution | Checks | p50 ms | p90 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|---|
| final_output | `groundedness` | async | 50 | 2482 | 5100 | 6962 | 0 |
| final_output | `toxicity` | blocking | 49 | 1289 | 2557 | 4100 | 0 |
| final_output | `pii` | blocking | 49 | 26 | 74 | 174 | 0 |
| final_output | `secrets` | blocking | 49 | 0 | 0 | 2 | 0 |
| tool_args | `pii_egress` | blocking | 49 | 18 | 210 | 382 | 0 |
| tool_args | `secrets` | blocking | 49 | 0 | 0 | 1 | 0 |
| tool_output | `tool_output_injection_protectai` | blocking | 49 | 2177 | 5791 | 8666 | 8 |
| tool_output | `pii` | blocking | 49 | 188 | 317 | 518 | 0 |
| tool_output | `secrets` | blocking | 49 | 0 | 1 | 1 | 0 |
| tool_output | `tool_output_injection_heuristic` | blocking | 49 | 0 | 0 | 0 | 0 |
| user_input | `user_injection_promptguard` | blocking | 51 | 1570 | 2530 | 4284 | 2 |
| user_input | `topic` | blocking | 51 | 238 | 356 | 620 | 0 |
| user_input | `pii` | blocking | 51 | 22 | 63 | 84 | 0 |
| user_input | `jailbreak_patterns` | blocking | 51 | 0 | 0 | 0 | 0 |

## Where the time goes: `blocking` (per check, all levels)

| Stage | Policy | Execution | Checks | p50 ms | p90 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|---|
| final_output | `groundedness` | blocking | 47 | 2273 | 3516 | 8693 | 0 |
| final_output | `toxicity` | blocking | 47 | 1544 | 2337 | 3513 | 0 |
| final_output | `pii` | blocking | 47 | 29 | 51 | 131 | 0 |
| final_output | `secrets` | blocking | 47 | 0 | 0 | 1 | 0 |
| tool_args | `pii_egress` | blocking | 47 | 14 | 44 | 88 | 0 |
| tool_args | `secrets` | blocking | 47 | 0 | 0 | 0 | 0 |
| tool_output | `tool_output_injection_protectai` | blocking | 47 | 2234 | 3783 | 5770 | 3 |
| tool_output | `pii` | blocking | 47 | 174 | 289 | 493 | 0 |
| tool_output | `secrets` | blocking | 47 | 0 | 1 | 1 | 0 |
| tool_output | `tool_output_injection_heuristic` | blocking | 47 | 0 | 0 | 1 | 0 |
| user_input | `user_injection_promptguard` | blocking | 48 | 1537 | 2272 | 3814 | 0 |
| user_input | `topic` | blocking | 48 | 265 | 486 | 1342 | 1 |
| user_input | `pii` | blocking | 48 | 18 | 70 | 116 | 0 |
| user_input | `jailbreak_patterns` | blocking | 48 | 0 | 0 | 1 | 0 |

Failures by reason: `async`: run blocked ×2; `blocking`: run blocked ×1
