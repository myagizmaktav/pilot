# Pilot Terminal-Bench Agent

AI terminal agent wrapping Claude Code with test awareness, environment scanning, and self-verification loops. Built to top the Terminal-Bench 2.0 leaderboard.

## Architecture

```
Harbor → Docker → Install Claude Code → Bootstrap env →
Build optimized prompt → Claude Code executes → Run tests →
If fail: feed errors to Claude Code (--resume) → Retry up to 3x
```

**Not a from-scratch agent.** Claude Code is the executor. Pilot adds intelligence before (bootstrapping + prompt) and after (verification + retry).

## 5-Step Pipeline

| Step | What | Impact |
|------|------|--------|
| 1. Bootstrap | Scan container: OS, tools, packages | +2-3pp |
| 2. Test Awareness | Read `/tests/test.sh`, inject into prompt | +3-5pp |
| 3. Prompt Construction | Autonomy + env + tests + patterns + checklist | +2-3pp |
| 4. Claude Code Execution | 30min timeout, stream-json output | baseline |
| 5. Self-Verification | Run tests, retry with `--resume` up to 3x | +5-7pp |

## Quick Start

```bash
# Prerequisites
pip install harbor
export ANTHROPIC_API_KEY=...

# Smoke test (1 task)
./run.sh

# Sample run (10 tasks)
./run.sh 10

# Full benchmark (all 89 tasks, 32 parallel)
./run.sh all 32
```

## Score Targets

| Baseline | Score | Notes |
|----------|-------|-------|
| Claude Code (vanilla) | 58.0% | No optimizations |
| KIRA (best Claude agent) | 74.7% | Opus 4.6, 3 tools |
| Forge Code (#1) | 78.4% | Gemini 3.1 Pro |
| **Pilot (target)** | **78-82%** | Opus 4.6 + verification |

## File Structure

```
pilot-bench/
├── pilot_agent/
│   ├── __init__.py              # Exports PilotAgent
│   ├── agent.py                 # PilotAgent(BaseInstalledAgent) — 5-step pipeline
│   ├── prompt_builder.py        # Prompt construction
│   ├── patterns.py              # Failure pattern library
│   ├── templates/
│   │   └── install-pilot-agent.sh.j2   # Docker installation
│   └── scripts/
│       ├── bootstrap.sh         # Container env scanning
│       └── verify-and-retry.sh  # Test verification loop
├── pyproject.toml
├── run.sh                       # Quick-run helper
└── README.md
```
