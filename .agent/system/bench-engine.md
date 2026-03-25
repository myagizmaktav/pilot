# Bench Engine Architecture

Custom execution engine for Terminal Bench 2.0. Replaces Claude Code CLI with direct Anthropic API calls.

## Why Custom Engine

Claude Code is a black box — no control over:
- Thinking budget per turn (extended thinking runs unbounded)
- Tool dispatch (can't validate/correct tool calls)
- Context window (CC manages its own compaction)
- Retry behavior (opaque error handling)

ForgeCode (81.8%) and every top Terminal Bench agent uses direct API calls.

## Architecture

```
Harbor Orchestrator (Python)
  │
  ├── agent.py (PilotAgent) — Harbor interface
  │   ├── setup() — install deps, upload engine, bootstrap env
  │   └── create_run_agent_commands() — returns ExecInput for engine.py
  │
  └── engine.py (in container) — execution loop
      ├── Anthropic Messages API (tool_use + extended thinking)
      ├── Tools: bash, read_file, write_file, edit_file
      ├── Progressive thinking budget
      ├── Loop detection
      ├── Auto-test verification
      ├── Context window pruning
      └── Prompt caching (cache_control)
```

### Data Flow

```
1. Harbor downloads task + creates Modal sandbox
2. agent.py setup():
   - Runs install-pilot-agent.sh.j2 (Node, uv, numpy, git)
   - Uploads engine.py to /opt/pilot-agent/
   - pip installs anthropic SDK
   - Uploads pilot.db (learning patterns) to /root/.pilot/data/
   - Runs env bootstrap → /app/.pilot-env-context.txt
   - Uploads test files to /tests/
3. Harbor calls create_run_agent_commands(instruction)
   → returns: python3 /opt/pilot-agent/engine.py --task "..." --project /app
4. engine.py runs:
   - Builds system prompt (task + env context + learned patterns)
   - Enters turn loop (max 60 turns, 90min timeout)
   - Calls Anthropic API with progressive thinking
   - Executes tool calls (bash, read, write, edit)
   - Auto-checks tests every 8 turns
   - Stops when tests pass or timeout
5. Writes /logs/agent/pilot-result.json
6. Harbor runs verifier (pytest) → reward 0.0 or 1.0
```

## Engine Configuration

### Constants (engine.py top-level)

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_TURNS` | 60 | Max API call iterations |
| `MAIN_TIMEOUT` | 5400 | 90 min total budget (seconds) |
| `BASH_TIMEOUT` | 120 | Per-command timeout (seconds) |
| `THINKING_HIGH_TURNS` | 8 | Turns with high thinking budget |
| `THINKING_HIGH_BUDGET` | 16000 | Thinking tokens for planning phase |
| `THINKING_LOW_BUDGET` | 4000 | Thinking tokens for execution phase |
| `MAX_OUTPUT_TOKENS` | 16000 | Max output tokens per API call |
| `TEST_CHECK_INTERVAL` | 8 | Auto-run tests every N turns |
| `MAX_REPEATED_COMMANDS` | 3 | Identical commands before loop warning |
| `CONTEXT_PRUNE_THRESHOLD` | 150000 | Estimated tokens before pruning |
| `CONTEXT_KEEP_TURNS` | 12 | Turns to keep when pruning |

### Tuning Knobs

**Thinking budget** — The most impactful parameter. Higher = better planning but slower and more expensive. ForgeCode uses high thinking early, low later. Current split: 16K for turns 0-7, 4K for turns 8+.

**Test interval** — How often to auto-inject test results. Lower = more feedback but wastes turns. Current: every 8 turns.

**Bash timeout** — Per-command max. Too low kills pip install/builds. Too high wastes time on stuck commands. Current: 120s (max 600s via tool param).

**Context pruning** — When to drop old messages. Too aggressive = loses important context. Too late = API errors. Current: prune at 150K estimated tokens, keep last 12 turns.

## Tools

### bash
Execute shell commands with timeout. Output capped at 50KB (first/last 25KB on truncation). Returns exit code on non-zero.

### read_file
Read file with line numbers. Supports offset/limit for partial reads. Rejects files >500KB (suggests `head` instead).

### write_file
Write or overwrite file. Creates parent directories. For new files or complete rewrites.

### edit_file
String replacement — find `old_string` (must appear exactly once), replace with `new_string`. More precise than write_file for small changes. Returns error if old_string not found or ambiguous.

## Progressive Thinking

```
Turn 0-7:  thinking_budget = 16000 tokens
           → Deep planning, approach selection, test analysis

Turn 8+:   thinking_budget = 4000 tokens
           → Quick execution decisions, error fixing
```

This mirrors ForgeCode's technique that contributed +12 points to their score. The idea: spend thinking budget on understanding the problem (first 8 turns), then switch to rapid execution.

## Loop Detection

`LoopDetector` tracks:
1. **Command history** — if the last 3 bash commands are identical → inject warning
2. **File edit counts** — if same file edited 5+ times → inject warning

Warning format: `⚠️ LOOP DETECTED: You ran 'X' 3 times. STOP. Try a completely different approach.`

Injected as a user message before the next API call.

## Context Window Management

Rough token estimation: `len(text) // 4`.

When estimated context > 150K tokens:
1. Keep first message (initial "Begin" instruction)
2. Keep last 12 turns (24 messages — user+assistant pairs)
3. Replace everything in between with: `[Context pruned: N earlier messages removed]`

This prevents API errors on long tasks while preserving recent context.

## Prompt Caching

System prompt uses `cache_control: {"type": "ephemeral"}`:
```python
system = [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]
```

This tells the API to cache the system prompt across turns. Since the system prompt is identical every turn (~2KB), this saves ~90% on input tokens for subsequent calls.

## API Retry

`api_call_with_retry()` handles:
- **429 Rate Limit** — exponential backoff: 10s, 30s, 60s
- **529 Overloaded** — same backoff
- **Other errors** — fail immediately

Max 3 retries before propagating the error.

## Result JSON

Written to `/logs/agent/pilot-result.json` (or `--result-json` path):

```json
{
  "Success": true,
  "TokensInput": 45000,
  "TokensOutput": 12000,
  "EstimatedCostUSD": 1.58,
  "Turns": 15,
  "ElapsedSeconds": 342.5
}
```

Cost calculated from Opus 4.6 pricing: $15/1M input, $75/1M output.

Harbor's `populate_context_post_run()` in agent.py reads this file for metrics.

## Learning DB Integration

Engine loads patterns from `/root/.pilot/data/pilot.db` (SQLite):
- Queries `cross_patterns` table where `confidence >= 0.7`
- Formats as `[DO]` / `[AVOID]` bullet list
- Injects into system prompt under `## Learned Patterns`

Currently 17 patterns (11 recommended, 6 anti-patterns) covering:
- Test-first workflow, brute-force approach, approach switching
- Task-specific: torch pre-installed, compression stdlib, crypto textbook, build Makefile, git reflog
- Anti-patterns: analysis paralysis, format mismatch, concurrent heavy processes, retrying failures

Managed by `pilot-bench/pilot_agent/scripts/seed-learning-db.py`.

## Env Bootstrap

Before engine runs, `agent.py setup()` executes env discovery in the container:
- `ls /app/` — workspace files
- `head -50 /tests/test_outputs.py` — test file preview
- Python package checks (torch, scipy, pandas, sklearn)
- `free -m`, `nproc` — system resources

Output saved to `/app/.pilot-env-context.txt`, injected into system prompt as `## Pre-discovered Environment`.

## Files

| File | Purpose |
|------|---------|
| `pilot-bench/pilot_agent/engine.py` | Execution engine (direct API) |
| `pilot-bench/pilot_agent/agent.py` | Harbor agent shim |
| `pilot-bench/pilot_agent/templates/install-pilot-agent.sh.j2` | Container bootstrap |
| `pilot-bench/pilot_agent/data/pilot.db` | Learning DB (SQLite) |
| `pilot-bench/pilot_agent/scripts/seed-learning-db.py` | Pattern seeder |

## Running

### Validation (3 tasks)
```bash
source .env && cd pilot-bench && harbor run \
  --job-name pilot-engine-vN -o jobs -d terminal-bench@2.0 \
  --agent-import-path "pilot_agent:PilotAgent" \
  -m "anthropic/claude-opus-4-6" -e modal -n 3 -k 1 \
  --agent-timeout-multiplier 9.0 --agent-setup-timeout-multiplier 5.0 \
  --ae "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  --task-name "break-filter-js-from-html" \
  --task-name "chess-best-move" \
  --task-name "gcode-to-text"
```

### Full Run (k=5 for leaderboard)
```bash
source .env && cd pilot-bench && harbor run \
  --job-name pilot-engine-full-k5 -o jobs -d terminal-bench@2.0 \
  --agent-import-path "pilot_agent:PilotAgent" \
  -m "anthropic/claude-opus-4-6" -e modal -n 3 -k 5 \
  --agent-timeout-multiplier 9.0 --agent-setup-timeout-multiplier 5.0 \
  --ae "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
```

### Check Results
```python
import json
r = json.load(open("pilot-bench/jobs/<job-name>/result.json"))
s = r["stats"]["evals"]["pilot-real__claude-opus-4-6__terminal-bench"]
print(f"Score: {s['metrics'][0]['mean']:.1%}")
```

### Debug Failures
```bash
# Agent stderr (engine errors)
cat pilot-bench/jobs/<job>/<task>/agent/command-0/stderr.txt

# Agent stdout (engine logs)
cat pilot-bench/jobs/<job>/<task>/agent/command-0/stdout.txt

# Verifier output (test results)
cat pilot-bench/jobs/<job>/<task>/verifier/command-0/stdout.txt
```

## Version History

| Version | Date | Changes | Score |
|---------|------|---------|-------|
| v1 (CC) | 2026-03-08 | First bench run with Claude Code | 55.6% (18 tasks) |
| v24 (CC) | 2026-03-22 | Best CC run, Haiku classifier, k=1 | 65.9% |
| v32 (CC) | 2026-03-25 | CC + env bootstrap + quality gates, k=5 | 49.2% @58 |
| v4 (engine) | 2026-03-25 | Custom engine, direct API | Validating... |
