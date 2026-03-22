package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/alekspetrov/pilot/internal/logging"
)

// EffortClassifier uses Claude Code (Haiku model) to classify task effort level
// instead of static complexity→effort mapping. Falls back to static mapping on subprocess failure.
// Caches results per task ID to avoid re-classification on retries.
//
// GH-727: LLM-based effort selection for smarter resource allocation.
// Cost: ~$0.0002 per classification (negligible vs execution savings).
type EffortClassifier struct {
	model               string
	timeout             time.Duration
	log                 *slog.Logger
	useStructuredOutput bool

	// cmdRunner is the function that executes the claude command.
	// Can be overridden for testing.
	cmdRunner func(ctx context.Context, args ...string) ([]byte, error)

	mu            sync.Mutex
	cache         map[string]string // task ID → cached effort level
	strategyCache map[string]string // task ID → cached strategy hint
}

// effortClassificationResponse is the JSON structure returned by the LLM.
type effortClassificationResponse struct {
	Effort   string `json:"effort"`
	Reason   string `json:"reason"`
	Strategy string `json:"strategy,omitempty"` // GH-bench: approach hint for the agent
}

const effortClassifierSystemPrompt = `You are a task analyzer for an AI coding agent pipeline. You do two things:

1. CLASSIFY EFFORT — how much thinking the agent needs
2. SUGGEST STRATEGY — a brief hint about the best approach

Effort levels control how many tokens the agent uses:
- LOW: Straightforward, mechanical tasks. No ambiguity.
- MEDIUM: Standard work with clear requirements.
- HIGH: Tasks requiring careful analysis or multiple considerations.

Decision factors (ranked by importance):
1. Ambiguity → higher effort (unclear requirements need more reasoning)
2. Risk (security/data integrity) → higher effort (mistakes are costly)
3. Scope (multi-file, cross-system) → higher effort (coordination needed)
4. Clear step-by-step instructions → lower effort (even if detailed)

IMPORTANT: A detailed issue with clear instructions is NOT automatically high effort.

For strategy, provide a ONE-SENTENCE hint about the best technical approach. Focus on:
- What algorithm or technique to use first
- What to check or read before coding
- What common pitfall to avoid

Respond with ONLY a JSON object (no markdown, no explanation):
{"effort": "low|medium|high", "reason": "brief explanation", "strategy": "one-sentence approach hint"}`

// NewEffortClassifier creates a classifier that uses ` + "`claude --print`" + ` subprocess.
// Uses the user's existing Claude Code subscription - no separate API key needed.
func NewEffortClassifier() *EffortClassifier {
	c := &EffortClassifier{
		model:   "claude-haiku-4-5-20251001",
		timeout: 30 * time.Second,
		log:     logging.WithComponent("effort-classifier"),
		cache:   make(map[string]string),
	}
	c.cmdRunner = c.defaultCmdRunner
	c.strategyCache = make(map[string]string)
	return c
}

// defaultCmdRunner executes the claude command.
func (c *EffortClassifier) defaultCmdRunner(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "claude", args...)
	return cmd.Output()
}

// newEffortClassifierWithRunner creates a classifier with a custom command runner for testing.
func newEffortClassifierWithRunner(runner func(ctx context.Context, args ...string) ([]byte, error)) *EffortClassifier {
	c := NewEffortClassifier()
	c.cmdRunner = runner
	return c
}

// SetUseStructuredOutput configures whether to use Claude Code's --json-schema structured output.
func (c *EffortClassifier) SetUseStructuredOutput(enabled bool) {
	c.useStructuredOutput = enabled
}

// Classify determines task effort level using Claude Code subprocess.
// Returns cached result if available for the given task ID.
// Returns empty string on subprocess failure (allows fallback to static mapping).
func (c *EffortClassifier) Classify(ctx context.Context, task *Task) string {
	if task == nil {
		return ""
	}

	// Check cache first (prevents re-classification on retry)
	if task.ID != "" {
		c.mu.Lock()
		if cached, ok := c.cache[task.ID]; ok {
			c.mu.Unlock()
			c.log.Debug("using cached effort", slog.String("task_id", task.ID), slog.String("effort", cached))
			return cached
		}
		c.mu.Unlock()
	}

	// Call Claude Code subprocess
	result, strategy, err := c.classify(ctx, task)
	if err != nil {
		c.log.Warn("LLM effort classification failed, falling back to static mapping",
			slog.String("task_id", task.ID),
			slog.Any("error", err),
		)
		return "" // Empty string signals fallback
	}

	// Cache result
	if task.ID != "" {
		c.mu.Lock()
		c.cache[task.ID] = result
		if strategy != "" {
			c.strategyCache[task.ID] = strategy
		}
		c.mu.Unlock()
	}

	c.log.Info("LLM classified task effort",
		slog.String("task_id", task.ID),
		slog.String("effort", result),
	)

	return result
}

// GetStrategy returns the cached strategy hint for a task, if available.
func (c *EffortClassifier) GetStrategy(taskID string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.strategyCache[taskID]
}

// classify calls Claude Code subprocess with Haiku model and parses the response.
// Returns effort level and strategy hint.
func (c *EffortClassifier) classify(ctx context.Context, task *Task) (string, string, error) {
	userContent := fmt.Sprintf("## Issue Title\n%s\n\n## Issue Description\n%s", task.Title, task.Description)

	// Truncate to avoid token overflow (description can be very long)
	const maxChars = 4000
	if len(userContent) > maxChars {
		userContent = userContent[:maxChars] + "\n...[truncated]"
	}

	// Build prompt with system instructions embedded
	prompt := fmt.Sprintf("%s\n\n---\n\n%s", effortClassifierSystemPrompt, userContent)

	// Add timeout to context
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()

	// Call claude --print with Haiku model
	var args []string
	if c.useStructuredOutput {
		args = []string{
			"--print",
			"-p", prompt,
			"--model", c.model,
			"--output-format", "json",
			"--json-schema", EffortSchema,
		}
	} else {
		args = []string{
			"--print",
			"-p", prompt,
			"--model", c.model,
			"--output-format", "text",
		}
	}

	output, err := c.cmdRunner(ctx, args...)
	if err != nil {
		return "", "", fmt.Errorf("claude command failed: %w", err)
	}

	if len(output) == 0 {
		return "", "", fmt.Errorf("empty response from claude")
	}

	if c.useStructuredOutput {
		effort, err := parseStructuredEffortResponse(output)
		return effort, "", err // structured output doesn't include strategy yet
	}
	return parseEffortResponseWithStrategy(string(output))
}

// parseEffortResponse extracts effort level from the LLM's JSON response.
func parseEffortResponse(text string) (string, error) {
	// Strip any markdown code fence wrapper
	text = strings.TrimSpace(text)
	text = strings.TrimPrefix(text, "```json")
	text = strings.TrimPrefix(text, "```")
	text = strings.TrimSuffix(text, "```")
	text = strings.TrimSpace(text)

	var resp effortClassificationResponse
	if err := json.Unmarshal([]byte(text), &resp); err != nil {
		return "", fmt.Errorf("parse effort JSON: %w (raw: %s)", err, text)
	}

	effort := strings.ToLower(resp.Effort)
	switch effort {
	case "low", "medium", "high":
		return effort, nil
	default:
		return "", fmt.Errorf("unknown effort level: %q", resp.Effort)
	}
}

// parseEffortResponseWithStrategy extracts effort level AND strategy hint from the LLM's JSON response.
func parseEffortResponseWithStrategy(text string) (string, string, error) {
	text = strings.TrimSpace(text)
	text = strings.TrimPrefix(text, "```json")
	text = strings.TrimPrefix(text, "```")
	text = strings.TrimSuffix(text, "```")
	text = strings.TrimSpace(text)

	var resp effortClassificationResponse
	if err := json.Unmarshal([]byte(text), &resp); err != nil {
		return "", "", fmt.Errorf("parse effort JSON: %w (raw: %s)", err, text)
	}

	effort := strings.ToLower(resp.Effort)
	switch effort {
	case "low", "medium", "high":
		return effort, resp.Strategy, nil
	default:
		return "", "", fmt.Errorf("unknown effort level: %q", resp.Effort)
	}
}

// parseStructuredEffortResponse extracts effort level from Claude Code's structured JSON output.
func parseStructuredEffortResponse(jsonResponse []byte) (string, error) {
	structuredOutput, err := extractStructuredOutput(jsonResponse)
	if err != nil {
		return "", fmt.Errorf("extract structured output: %w", err)
	}

	var resp effortClassificationResponse
	if err := json.Unmarshal(structuredOutput, &resp); err != nil {
		return "", fmt.Errorf("parse structured effort: %w", err)
	}

	effort := strings.ToLower(resp.Effort)
	switch effort {
	case "low", "medium", "high":
		return effort, nil
	default:
		return "", fmt.Errorf("unknown effort level: %q", resp.Effort)
	}
}
