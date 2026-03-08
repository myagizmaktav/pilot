# SOP: Daytona Bench Operations

## Prerequisites

| Item | Value |
|------|-------|
| Daytona API Key | `DAYTONA_API_KEY` env var (starts with `dtn_`) |
| Daytona API URL | `DAYTONA_BASE_URL=https://app.daytona.io/api` |
| Claude OAuth Token | `CLAUDE_CODE_OAUTH_TOKEN` (1-year token from `claude setup-token`) |
| Harbor CLI | `harbor` (installed via pip, `pip3 install harbor`) |
| Memory Limit | 10 GiB (Daytona free tier) |

## Quick Reference

### Start a Full Run

```bash
cd pilot-bench

DAYTONA_API_KEY="dtn_..." \
DAYTONA_BASE_URL="https://app.daytona.io/api" \
harbor run \
  --job-name <name> \
  -o jobs \
  -d terminal-bench@2.0 \
  --agent-import-path "pilot_agent:PilotAgent" \
  -m "anthropic/claude-opus-4-6" \
  -e daytona \
  -n 1 \
  --timeout-multiplier 5.0 \
  --ae "CLAUDE_CODE_OAUTH_TOKEN=<token>"
```

### Resume a Failed Run

```bash
DAYTONA_API_KEY="dtn_..." \
DAYTONA_BASE_URL="https://app.daytona.io/api" \
harbor jobs resume -p jobs/<job-name>
```

### Check Results

```bash
python3 -c "
import json, glob, os
job_dir = 'pilot-bench/jobs/<job-name>'
passed, failed, errors = 0, 0, 0
for rf in sorted(glob.glob(f'{job_dir}/*/result.json')):
    r = json.load(open(rf))
    reward = r.get('reward',{}).get('reward')
    name = os.path.basename(os.path.dirname(rf)).rsplit('__',1)[0]
    if reward == 1.0: passed += 1; print(f'  ✓ {name}')
    elif reward == 0.0: failed += 1; print(f'  ✗ {name}')
    else: errors += 1; print(f'  ? {name} ({reward})')
total = passed + failed + errors
print(f'\nScore: {passed}/{total} = {passed/max(total,1)*100:.1f}%')
"
```

## Daytona Sandbox Management

### List Active Sandboxes

```bash
curl -s -H "Authorization: Bearer $DAYTONA_API_KEY" \
  "https://app.daytona.io/api/sandbox" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'Sandboxes: {len(data)}, Mem: {sum(s.get(\"memory\",0) for s in data)}/{10} GiB')
for s in data:
    print(f'  {s[\"id\"]} | {s[\"state\"]} | {s.get(\"memory\",\"?\")}GiB')
"
```

### Delete a Sandbox

```bash
curl -s -X DELETE -H "Authorization: Bearer $DAYTONA_API_KEY" \
  "https://app.daytona.io/api/sandbox/<sandbox-id>"
```

### Delete ALL Sandboxes (nuclear option)

```bash
curl -s -H "Authorization: Bearer $DAYTONA_API_KEY" \
  "https://app.daytona.io/api/sandbox" | \
python3 -c "
import json, sys, subprocess
for s in json.load(sys.stdin):
    print(f'Deleting {s[\"id\"]}...')
    subprocess.run(['curl','-s','-X','DELETE',
      '-H',f'Authorization: Bearer {sys.argv[1]}',
      f'https://app.daytona.io/api/sandbox/{s[\"id\"]}'], capture_output=True)
" "$DAYTONA_API_KEY"
```

## Monitoring

### 20-Min Monitor Script

Save as `/tmp/pilot-bench-monitor.sh` and run in background:

```bash
# Key env vars to set before running
export DAYTONA_API_KEY="dtn_..."

# Check loop
while true; do
    # Count results in job dir
    # Check orchestrator alive: pgrep -f "harbor"
    # Check sandbox count via API
    # Alert if orchestrator died before 89 tasks
    sleep 1200  # 20 min
done
```

### Manual Quick Check

```bash
# Is harbor running?
pgrep -f harbor

# How many results so far?
find pilot-bench/jobs/<job-name> -maxdepth 2 -name "result.json" | wc -l

# Active sandboxes?
curl -s -H "Authorization: Bearer $DAYTONA_API_KEY" \
  "https://app.daytona.io/api/sandbox" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"

# Latest trial log?
ls -lt pilot-bench/jobs/<job-name>/*/trial.log | head -1 | xargs tail -3
```

## Common Failure Modes

| Error | Cause | Fix |
|-------|-------|-----|
| `API key or JWT token is required` | Missing `DAYTONA_API_KEY` env var | Set env var before `harbor run` |
| `Total memory limit exceeded` | Stale sandboxes consuming 10GiB quota | Delete old sandboxes via API |
| `Sandbox not found` | Sandbox auto-deleted or expired | Re-run task (harbor resume handles this) |
| Auth token expired (44/89 fail) | Claude OAuth token expired mid-run | Use `claude setup-token` for 1-year token |
| fd leak (32/89 fail) | Docker QEMU issue, not Daytona | Use Daytona instead of Docker |
| Orchestrator dies silently | Process killed, OOM, or terminal closed | Run in tmux/screen, monitor with cron |

## Token Management

### Claude Code OAuth Token (1 year)

```bash
claude setup-token  # Interactive — generates long-lived token
# Output: sk-ant-oat01-...
# Pass via: --ae "CLAUDE_CODE_OAUTH_TOKEN=<token>"
```

### Daytona API Key

Get from: https://app.daytona.io/dashboard/keys

Format: `dtn_<hex>`

## Architecture Notes

- Harbor orchestrator runs **locally** (macOS)
- Each task gets a Daytona cloud sandbox (x86 Linux)
- Harbor creates sandbox → uploads agent → runs commands → downloads results → deletes sandbox
- Sequential mode (`-n 1`): one sandbox at a time, ~4GiB per sandbox
- Parallel mode (`-n 4`): would need tier upgrade for >10GiB
- Results written to local `jobs/<name>/<task>__<hash>/result.json`
