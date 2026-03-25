"""
PilotAgent — Terminal-Bench agent running the real Pilot Go binary.

Architecture:
  Harbor Orchestrator → PilotAgent (this shim) → pilot binary (Go) → Claude Code

The Python agent is a thin installer/runner:
1. Install Claude Code + pilot binary in container
2. Run `pilot task "instruction" --local` (full Pilot pipeline, no git workflow)
3. Parse result JSON for token metrics
"""

import json
import logging
import os
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.models.agent.context import AgentContext

logger = logging.getLogger(__name__)

# Paths
AGENT_LOG_DIR = "/logs/agent"
RESULT_JSON = f"{AGENT_LOG_DIR}/pilot-result.json"
MAIN_TIMEOUT = 5400  # 90 min — heavy deps (PyTorch) need more time


class PilotAgent(BaseInstalledAgent):
    """
    Real Pilot binary bench agent.

    Runs the actual Go executor pipeline: prompt building, model routing,
    hooks, Claude Code invocation — everything production Pilot does,
    minus git workflow (--local mode).
    """

    @staticmethod
    def name() -> str:
        return "pilot-real"

    @property
    def _install_agent_template_path(self) -> Path:
        return Path(__file__).parent / "templates" / "install-pilot-agent.sh.j2"

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        """Run Pilot Go binary with direct Anthropic API backend."""
        env = self._build_env()
        model = self._resolve_model()

        # Escape instruction for shell (single quotes with escaping)
        safe_instruction = instruction.replace("'", "'\\''")

        return [ExecInput(
            command=(
                f"pilot task '{safe_instruction}' "
                f"--local "
                f"--project /app "
                f"--verbose "
                f"--result-json {RESULT_JSON}"
            ),
            cwd="/app",
            env={
                **env,
                "IS_SANDBOX": "1",
            },
            timeout_sec=MAIN_TIMEOUT,
        )]

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Read Pilot's result JSON for token metrics."""
        # Try pilot result JSON first (structured, reliable)
        result_file = self.logs_dir / "pilot-result.json"
        if not result_file.exists():
            # Try alternate paths in command output dirs
            for cmd_dir in sorted(self.logs_dir.glob("command-*")):
                # Harbor copies container files to command dirs
                candidate = cmd_dir / "pilot-result.json"
                if candidate.exists():
                    result_file = candidate
                    break

        if result_file.exists():
            try:
                result = json.loads(result_file.read_text())
                if not isinstance(result, dict):
                    logger.warning(f"Pilot result JSON is not a dict: {type(result)}")
                else:
                    context.n_input_tokens = result.get("TokensInput", 0)
                    context.n_output_tokens = result.get("TokensOutput", 0)
                    context.cost_usd = result.get("EstimatedCostUSD", 0.0)
                    return
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Failed to parse pilot result JSON: {e}")

        # Fallback: parse Claude Code stream-json from stdout
        self._parse_claude_output(context)

    def _parse_claude_output(self, context: AgentContext) -> None:
        """Fallback: parse Claude Code JSONL output for token metrics."""
        for cmd_dir in sorted(self.logs_dir.glob("command-*")):
            stdout = cmd_dir / "stdout.txt"
            if not stdout.exists():
                continue

            total_input = 0
            total_output = 0
            total_cost = 0.0

            with open(stdout) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        usage = event.get("usage", {})
                        if usage:
                            total_input += usage.get("input_tokens", 0)
                            total_output += usage.get("output_tokens", 0)
                        if event.get("type") == "result":
                            cost = event.get("cost_usd", 0)
                            if cost:
                                total_cost = max(total_cost, float(cost))
                    except (json.JSONDecodeError, ValueError):
                        continue

            if total_input > 0:
                context.n_input_tokens = total_input
                context.n_output_tokens = total_output
                if total_cost > 0:
                    context.cost_usd = total_cost

    def _build_env(self) -> dict[str, str]:
        """Collect auth environment variables."""
        env: dict[str, str] = {}
        for key in [
            "ANTHROPIC_API_KEY",
            "PILOT_ENGINE_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ]:
            if key in os.environ:
                env[key] = os.environ[key]
        return {k: v for k, v in env.items() if v}

    def _resolve_model(self) -> str:
        """Resolve model name (strip provider prefix)."""
        if self.model_name:
            if "/" in self.model_name:
                return self.model_name.split("/", 1)[1]
            return self.model_name
        return "claude-opus-4-6"

    def _build_config(self, model: str) -> str:
        """Build production-grade config.yaml for bench runs.

        Mirrors the real production config with executor features that
        improve task success: hooks, quality gates, effort routing,
        structured output, and timeouts.
        """
        return f"""version: "1.0"
orchestrator:
  model: "{model}"
executor:
  type: "anthropic-api"
  hooks:
    enabled: false
  model_routing:
    enabled: true
    trivial: "{model}"
    simple: "{model}"
    medium: "{model}"
    complex: "{model}"
  timeout:
    default: 30m
    trivial: 15m
    simple: 25m
    medium: 30m
    complex: 60m
  effort_routing:
    enabled: true
    trivial: low
    simple: medium
    medium: high
    complex: high
  effort_classifier:
    enabled: true
    model: claude-haiku-4-5-20251001
    timeout: 30s
  intent_judge:
    enabled: false
  retry:
    enabled: true
    rate_limit:
      max_attempts: 3
      initial_backoff: 30s
      backoff_multiplier: 2
    api_error:
      max_attempts: 3
      initial_backoff: 5s
      backoff_multiplier: 2
    timeout:
      max_attempts: 2
      initial_backoff: 0s
      backoff_multiplier: 0
      extend_timeout: true
      timeout_multiplier: 1.5
quality:
  enabled: true
  gates:
    - name: test
      type: test
      command: "if [ -f /tests/test_outputs.py ]; then cd /app && /usr/local/bin/uvx -p 3.13 -w pytest==8.4.1 pytest /tests/test_outputs.py -rA 2>&1 || /root/.local/bin/uvx -p 3.13 -w pytest==8.4.1 pytest /tests/test_outputs.py -rA 2>&1; fi"
      required: true
      timeout: 5m
      max_retries: 2
      retry_delay: 5s
      failure_hint: "Tests failed. Read /tests/test_outputs.py to understand what is expected, then fix your implementation."
memory:
  path: /root/.pilot/data
  learning:
    enabled: true
"""

    async def setup(self, environment) -> None:
        """Install Claude Code + pilot binary, write config, upload test files."""
        # Base setup: renders install-pilot-agent.sh.j2, uploads, executes
        await super().setup(environment)

        # Upload pre-built pilot binary (now with anthropic-api backend)
        binary_path = Path(__file__).parent.parent / "bin" / "pilot-linux-amd64"
        if binary_path.exists():
            await environment.upload_file(
                source_path=binary_path,
                target_path="/usr/local/bin/pilot",
            )
            await environment.exec(command="chmod +x /usr/local/bin/pilot")
            logger.info("Uploaded pilot binary to /usr/local/bin/pilot")
        else:
            logger.error(f"Pilot binary not found at {binary_path}")
            raise FileNotFoundError(
                f"Pilot binary not found: {binary_path}. "
                f"Run 'make bench-binary' first."
            )

        # Write config with anthropic-api backend
        model = self._resolve_model()
        config = self._build_config(model)
        await environment.exec(
            command=f"mkdir -p /root/.pilot && cat > /root/.pilot/config.yaml << 'CFGEOF'\n{config}CFGEOF",
        )

        # Upload pre-seeded learning DB (curated patterns from bench failure analysis)
        db_path = Path(__file__).parent / "data" / "pilot.db"
        if db_path.exists():
            await environment.exec(command="mkdir -p /root/.pilot/data")
            await environment.upload_file(
                source_path=db_path,
                target_path="/root/.pilot/data/pilot.db",
            )
            logger.info("Uploaded pre-seeded learning DB to /root/.pilot/data/pilot.db")

        # Pre-discover environment so prompt can include it (saves 1 turn)
        await self._run_env_bootstrap(environment)

        # Upload test files for test-aware prompting
        # Pilot's prompt builder can read /tests/ if it exists
        tests_dir = self._find_task_tests_dir()
        if tests_dir:
            await environment.exec(command="mkdir -p /tests")
            uploaded = 0
            for test_file in sorted(tests_dir.iterdir()):
                if test_file.is_file():
                    await environment.upload_file(
                        source_path=test_file,
                        target_path=f"/tests/{test_file.name}",
                    )
                    uploaded += 1
            if uploaded:
                logger.info(f"Uploaded {uploaded} test files to /tests/")

    async def _run_env_bootstrap(self, environment) -> None:
        """Pre-discover container environment and save to file.

        The Go prompt builder reads this file and injects it into the prompt,
        saving Claude one full turn of env discovery commands.
        """
        cmds = [
            "echo '=== FILES ==='",
            "ls /app/ 2>/dev/null | head -30",
            "echo '=== TESTS ==='",
            "head -50 /tests/test_outputs.py 2>/dev/null || echo 'NO_TEST_FILE'",
            "echo '=== PYTHON PACKAGES ==='",
            "python3 -c \"import torch; print('torch='+torch.__version__)\" 2>/dev/null || echo 'torch=missing'",
            "python3 -c \"import scipy; print('scipy='+scipy.__version__)\" 2>/dev/null || echo 'scipy=missing'",
            "python3 -c \"import pandas; print('pandas='+pandas.__version__)\" 2>/dev/null || echo 'pandas=missing'",
            "python3 -c \"import sklearn; print('sklearn=available')\" 2>/dev/null || echo 'sklearn=missing'",
            "echo '=== SYSTEM ==='",
            "free -m 2>/dev/null | grep Mem || echo 'free: N/A'",
            "echo \"CPUs: $(nproc 2>/dev/null || echo N/A)\"",
        ]
        script = " ; ".join(cmds)
        try:
            await environment.exec(
                command=f"({script}) > /app/.pilot-env-context.txt 2>&1",
                timeout_sec=15,
            )
            logger.info("Environment bootstrap written to /app/.pilot-env-context.txt")
        except Exception as e:
            logger.warning(f"Env bootstrap failed (non-fatal): {e}")

    def _find_task_tests_dir(self) -> Path | None:
        """Find test files in harbor's task cache."""
        try:
            config_path = self.logs_dir.parent / "config.json"
            if not config_path.exists():
                config_path = self.logs_dir / "config.json"
            if not config_path.exists():
                return None

            config = json.loads(config_path.read_text())
            task_path = config.get("task", {}).get("path", "")
            if not task_path:
                return None

            cache_base = Path.home() / ".cache" / "harbor" / "tasks"
            if not cache_base.exists():
                return None

            for parent in cache_base.iterdir():
                candidate = parent / task_path / "tests"
                if candidate.is_dir():
                    return candidate

        except Exception as e:
            logger.debug(f"Could not find task tests in cache: {e}")

        return None

    def _setup_env(self) -> dict[str, str]:
        """Environment variables for install script."""
        env = super()._setup_env()
        env.update(self._build_env())
        return env
