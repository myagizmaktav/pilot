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
MAIN_TIMEOUT = 3600  # 60 min


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
        """Single command: run pilot task --local."""
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
                "FORCE_AUTO_BACKGROUND_TASKS": "1",
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
            "ANTHROPIC_AUTH_TOKEN",
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

    async def setup(self, environment) -> None:
        """Install Claude Code + pilot binary, write config, upload test files."""
        # Base setup: renders install-pilot-agent.sh.j2, uploads, executes
        await super().setup(environment)

        # Upload pre-built pilot binary
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

        # Write config with resolved model
        # Force all complexity tiers to use the same model (Opus)
        # Default routing sends medium tasks to Sonnet — causes API timeouts in bench
        model = self._resolve_model()
        config = (
            f'version: "1.0"\n'
            f"orchestrator:\n"
            f'  model: "{model}"\n'
            f"executor:\n"
            f'  type: "claude-code"\n'
            f"  model_routing:\n"
            f"    enabled: true\n"
            f'    trivial: "{model}"\n'
            f'    simple: "{model}"\n'
            f'    medium: "{model}"\n'
            f'    complex: "{model}"\n'
        )
        await environment.exec(
            command=f"mkdir -p /root/.pilot && cat > /root/.pilot/config.yaml << 'CFGEOF'\n{config}CFGEOF",
        )

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
