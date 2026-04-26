package executor

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseGateOutput(t *testing.T) {
	output := `
[1/5] Build
  [go build] ✓ (2s)

[2/5] Lint
  [golangci-lint] ✓ (5s)

[3/5] Test (short)
  [go test -short] ✗ (3s)

    FAIL: TestSomething
    Expected: true
    Got: false

[4/5] Secret Patterns
  [check-secrets] ✓ (1s)

[5/5] Integration
  [integration] ✓ (2s)
`

	checks := parseGateOutput(output)

	if len(checks) != 5 {
		t.Errorf("Expected 5 checks, got %d", len(checks))
	}

	// Check Build passed
	if !checks[0].Passed {
		t.Error("Expected Build to pass")
	}

	// Check Test failed
	if checks[2].Passed {
		t.Error("Expected Test to fail")
	}
	if checks[2].Name != "Test (short)" {
		t.Errorf("Expected check name 'Test (short)', got '%s'", checks[2].Name)
	}
}

func TestFormatGateResult(t *testing.T) {
	result := &GateResult{
		Passed: true,
		Checks: []GateCheck{
			{Name: "Build", Passed: true},
			{Name: "Lint", Passed: true},
			{Name: "Test", Passed: true},
		},
	}

	output := FormatGateResult(result)

	if output == "" {
		t.Error("Expected non-empty output")
	}

	// Check for pass indicator
	if !gateContains(output, "PASSED") {
		t.Error("Expected PASSED in output")
	}
}

func TestFormatGateResultFailed(t *testing.T) {
	result := &GateResult{
		Passed: false,
		Checks: []GateCheck{
			{Name: "Build", Passed: true},
			{Name: "Lint", Passed: false},
			{Name: "Test", Passed: false},
		},
	}

	output := FormatGateResult(result)

	// Check for fail indicator
	if !gateContains(output, "FAILED") {
		t.Error("Expected FAILED in output")
	}
}

func TestRunQuickUsesLibGoBootstrap(t *testing.T) {
	tempDir := t.TempDir()
	projectDir := filepath.Join(tempDir, "project")
	scriptDir := filepath.Join(projectDir, "scripts")
	goBinDir := filepath.Join(tempDir, "fake-go", "bin")
	realGoPath, err := exec.LookPath("go")
	if err != nil {
		t.Fatalf("LookPath(go): %v", err)
	}

	if err := os.MkdirAll(scriptDir, 0755); err != nil {
		t.Fatalf("MkdirAll(scriptDir): %v", err)
	}
	if err := os.MkdirAll(goBinDir, 0755); err != nil {
		t.Fatalf("MkdirAll(goBinDir): %v", err)
	}
	if err := os.WriteFile(filepath.Join(projectDir, "go.mod"), []byte("module example.com/test\n\ngo 1.24\n"), 0644); err != nil {
		t.Fatalf("WriteFile(go.mod): %v", err)
	}
	if err := os.WriteFile(filepath.Join(projectDir, "main.go"), []byte("package main\nfunc main() {}\n"), 0644); err != nil {
		t.Fatalf("WriteFile(main.go): %v", err)
	}
	if err := os.Symlink(realGoPath, filepath.Join(goBinDir, "go")); err != nil {
		t.Fatalf("Symlink(go): %v", err)
	}
	if err := os.WriteFile(filepath.Join(scriptDir, "lib-go.sh"), []byte("#!/bin/bash\nrequire_go() { export PATH=\""+goBinDir+":$PATH\"; }\n"), 0755); err != nil {
		t.Fatalf("WriteFile(lib-go.sh): %v", err)
	}

	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", "/usr/bin:/bin")
	defer func() {
		_ = os.Setenv("PATH", oldPath)
	}()

	result, err := NewGate(projectDir).RunQuick(context.Background())
	if err != nil {
		t.Fatalf("RunQuick error: %v", err)
	}
	if !result.Passed {
		t.Fatalf("expected quick gate to pass, output:\n%s", result.Output)
	}
	if len(result.Checks) != 1 {
		t.Fatalf("expected 1 check, got %d", len(result.Checks))
	}
	if result.Checks[0].Name != "build" {
		t.Fatalf("expected build check, got %q", result.Checks[0].Name)
	}
	if strings.Contains(result.Output, "go: command not found") {
		t.Fatalf("expected lib-go bootstrap to provide go, output:\n%s", result.Output)
	}
}

func gateContains(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
