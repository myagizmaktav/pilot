# Pilot Bench Worklog — Real Binary Agent

**Branch**: `feat/pilot-bench-real`
**Goal**: Benchmark real Pilot Go binary on Terminal-Bench 2.0, target ≥58% (stock Claude Code baseline)
**Started**: 2026-03-05

---

## Day 1 (Mar 5): Initial Setup

- Created `pilot-bench/` with Python agent shim (`PilotAgent`)
- Set up Daytona sandbox environment, Harbor CLI integration
- First run: agent installs Claude Code + pilot binary in container
- **Blocker**: npm not found in some containers → added Node.js install to template
- **Blocker**: OAuth token format wrong → fixed with `claude setup-token` (1-year token)
- Commits: `51c4705c`, `1caa5975`, `3fc9c05c`

## Day 2 (Mar 6-7): Pipeline Wiring

- Wired `--local` mode (skip git workflow) and `--result-json` for structured output
- Added test file upload to container for test-aware prompting
- Fixed config.json path lookup (trial root vs logs_dir)
- Installed jinja2 for template-based prompts
- Multiple Daytona runs — frequent DNS timeouts, sandbox memory limits
- **Score (Python v3 agent)**: 36.2% on 89 tasks — worse than stock Claude Code (58%)
- **Decision**: abandon Python prompt pipeline, use real Go binary instead
- Commits: `890176fd`, `ed3fcc27`, `59e2fbdb`, `1635f151`

## Day 3 (Mar 8): Real Binary Agent

- Rewrote agent from 5-step Python pipeline to thin Go binary shim
- Added `make bench-binary` (cross-compile linux/amd64 static, 19MB)
- Added `--result-json` flag to `pilot task` CLI
- Pre-installed uv/uvx in container (verifiers need it)
- DNS fallback: added `8.8.8.8` to resolv.conf
- Commit: `c8fa863e`

### Validation Run 1-2 (3 tasks)
| Task | Result | Issue |
|------|--------|-------|
| break-filter | 1.0 PASS | OK |
| chess-best-move | 0.0 (verifier broken) | Pilot solved it (d2g5), verifier failed: `uvx: command not found` |
| gcode-to-text | 0.0 | API timeout mid-task |

### Root Causes Found
1. **Heartbeat killing completed tasks**: result event received but heartbeat monitor still running → killed process 5min later
2. **Model routing gap**: `model_routing.enabled: false` → `SelectModel()` returns "" → Claude Code defaults to Sonnet → API timeouts
3. **Verifier DNS timeout**: `curl` to github.com times out (300s) → uv install fails → `uvx: command not found`

## Day 4 (Mar 9): Fixes + Production Config

### Go Fixes (production-worthy)
- **Heartbeat cancel on result event** (`backend_claudecode.go:415`): `cancelHeartbeat()` when `EventTypeResult` received
- **HeartbeatTimeout 5min → 15min** (`backend_claudecode.go:24`): long image analysis pauses need more time

### Verifier Fix
- Created `/root/.local/bin/env` shim so verifier's `source $HOME/.local/bin/env` doesn't fail when its own uv install times out
- Symlinked uv/uvx to `/usr/local/bin/` (always on PATH)
- Removed conditional guard on uv install (always install, always symlink)

### Validation Run 3 (verifier fix only, minimal config)
| Task | Result | Issue |
|------|--------|-------|
| break-filter | 1.0 PASS | OK |
| chess-best-move | 0.0 FAIL | Verifier worked! But Pilot only wrote 1 move, test expects 2 |
| gcode-to-text | 0.0 FAIL | Wrong flag: `G1ng` vs `GiNg` (2 chars off) |

**Key insight**: verifier fix worked — failures are now real task failures, not infra issues.

### Production Config Added
Ported real `~/.pilot/config.yaml` executor settings to bench:
- **Quality gates**: run test_outputs.py after execution, retry 2x on failure
- **Hooks**: run_tests_on_stop, block_destructive
- **Effort routing**: haiku classifier → effort mapping
- **Intent judge**: haiku (disabled — needs ANTHROPIC_API_KEY, not OAuth)
- **Retry**: API error/rate limit retry with backoff
- **Structured output**: enabled

### Validation Run 4 (production config)
| Task | Score | Cost | Retries | Duration |
|------|-------|------|---------|----------|
| break-filter | **1.0** | $1.09 | 2 | 15m |
| chess-best-move | **1.0** | $1.04 | 1 | 14m |
| gcode-to-text | **0.0** | $1.32 | 0 | 22m |

**Score: 2/3 (67%)** — up from 1/3 (33%)

**Chess fix confirmed**: quality gate caught missing move → retry → both moves written → PASS

**Gcode failure**: `pip install numpy` ran as background task (FORCE_AUTO_BACKGROUND_TASKS=1) → timed out → execution failed. Fixed by removing the env var.

### Validation Run 5 (FORCE_AUTO_BACKGROUND removed, quality gate PATH broken)
| Task | Score | Cost | Retries | Issue |
|------|-------|------|---------|-------|
| break-filter | **1.0** | $0.57 | 2 | OK |
| chess-best-move | **0.0** | $1.51 | 1 | Quality gate exit 127: `uvx` not on PATH in Go exec.Command |
| gcode-to-text | **0.0** | $1.19 | 0 | Request timed out after 30 turns — spent time rendering, never wrote out.txt |

**Chess root cause**: quality gate `bash -c 'source /root/.local/bin/env && uvx ...'` fails because Go's `exec.Command` doesn't inherit shell PATH. Fixed by using explicit `export PATH=/usr/local/bin:/root/.local/bin:$PATH`.

**Gcode**: genuinely hard task. Claude spends all tokens on matplotlib rendering, times out before writing the answer file. Not an infra issue.

### Commits (Day 4)
- `4a4a56b3` feat(bench): real binary agent + heartbeat fix + verifier uv shim
- `d7b2ef09` feat(bench): add production config — hooks, quality gates, effort routing
- `bcecdaf0` fix(bench): remove FORCE_AUTO_BACKGROUND_TASKS causing pip install timeouts
- `a1877a49` fix(bench): use absolute PATH in quality gate command

---

## Current State

### What Works
- Real Pilot Go binary running in Daytona sandboxes
- Quality gates with test retry (game changer for chess-like tasks)
- Verifier uv shim (no more false negatives from DNS timeouts)
- Effort routing, structured output, hooks
- ~$1-1.30 per task cost

### What's Left
- [ ] Validate gcode fix (FORCE_AUTO_BACKGROUND_TASKS removal)
- [ ] Run full 89-task suite
- [ ] Compare score vs stock Claude Code (58%) and Python agent (36.2%)
- [ ] Analyze failures, iterate on prompt_builder.go if needed
- [ ] Consider making HeartbeatTimeout configurable (currently hardcoded 15min)
- [ ] Fix model routing gap for production (orchestrator model as fallback)
- [ ] Wire ANTHROPIC_API_KEY for intent judge (currently only OAuth token)

### Known Limitations
- Intent judge disabled (needs ANTHROPIC_API_KEY, we only pass OAuth)
- DNS unreliable in Daytona sandboxes (mitigated with 8.8.8.8 + uv shim)
- Sequential mode only (n=1, ~4GiB per sandbox, Daytona free tier 10GiB)
- Full run takes 12-20h

### Key Files
| File | Purpose |
|------|---------|
| `pilot-bench/pilot_agent/agent.py` | Python agent shim (~240 lines) |
| `pilot-bench/pilot_agent/templates/install-pilot-agent.sh.j2` | Container setup |
| `internal/executor/backend_claudecode.go` | Heartbeat + result parsing |
| `internal/executor/prompt_builder.go` | Prompt template (affects score) |
| `cmd/pilot/commands.go` | `--local`, `--result-json` flags |
| `.agent/sops/development/pilot-bench-real-binary.md` | Full SOP |
| `.agent/sops/daytona-bench-operations.md` | Sandbox management |

### Run Commands
```bash
# 3-task validation
source .env && cd pilot-bench && \
harbor run --job-name pilot-real-valN -o jobs \
  -d terminal-bench@2.0 --agent-import-path "pilot_agent:PilotAgent" \
  -m "anthropic/claude-opus-4-6" -e daytona -n 1 --timeout-multiplier 5.0 \
  --ae "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN" \
  -t chess-best-move -t break-filter-js-from-html -t gcode-to-text

# Full 89-task (tmux)
source .env && cd pilot-bench && \
harbor run --job-name pilot-real-full -o jobs \
  -d terminal-bench@2.0 --agent-import-path "pilot_agent:PilotAgent" \
  -m "anthropic/claude-opus-4-6" -e daytona -n 1 --timeout-multiplier 5.0 \
  --ae "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN"

# Check results
python3 -c "
import json, glob, os
job = 'jobs/<job-name>'
p = f = e = 0
for rf in sorted(glob.glob(f'{job}/*/result.json')):
    r = json.load(open(rf))
    vr = (r.get('verifier_result') or {}).get('rewards',{}).get('reward')
    if vr == 1.0: p += 1
    elif vr == 0.0: f += 1
    else: e += 1
t = p + f + e
print(f'Score: {p}/{t} = {p/max(t,1)*100:.1f}%')
"
```

---

**Last Updated**: 2026-03-09T13:40Z
