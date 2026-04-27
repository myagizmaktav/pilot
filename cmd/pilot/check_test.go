package main

import (
	"bytes"
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCheckCommandUsesProjectFlag(t *testing.T) {
	restoreCheckTestState(t)
	wantPath := t.TempDir()
	runProjectCheck = func(ctx context.Context, projectPath string, stdout, stderr io.Writer) error {
		if projectPath != wantPath {
			t.Fatalf("projectPath = %q, want %q", projectPath, wantPath)
		}
		_, err := io.WriteString(stdout, "gate ok\n")
		return err
	}

	cmd := newCheckCmd()
	cmd.SetArgs([]string{"--project", wantPath})
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(io.Discard)

	if err := cmd.Execute(); err != nil {
		t.Fatalf("Execute() error = %v", err)
	}
	if got := out.String(); !strings.Contains(got, "gate ok") {
		t.Fatalf("stdout = %q, want gate output", got)
	}
}

func TestCheckCommandUsesCurrentDirectoryByDefault(t *testing.T) {
	restoreCheckTestState(t)
	wantPath := t.TempDir()
	oldWD, err := os.Getwd()
	if err != nil {
		t.Fatalf("Getwd() error = %v", err)
	}
	defer func() {
		if chdirErr := os.Chdir(oldWD); chdirErr != nil {
			t.Fatalf("Chdir restore error = %v", chdirErr)
		}
	}()
	if err := os.Chdir(wantPath); err != nil {
		t.Fatalf("Chdir() error = %v", err)
	}

	runProjectCheck = func(ctx context.Context, projectPath string, stdout, stderr io.Writer) error {
		if projectPath != wantPath {
			t.Fatalf("projectPath = %q, want %q", projectPath, wantPath)
		}
		_, err := io.WriteString(stdout, "gate ok\n")
		return err
	}

	cmd := newCheckCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(io.Discard)

	if err := cmd.Execute(); err != nil {
		t.Fatalf("Execute() error = %v", err)
	}
}

func TestRunProjectCheckExecutesGateScript(t *testing.T) {
	projectPath := t.TempDir()
	scriptDir := filepath.Join(projectPath, "scripts")
	if err := os.MkdirAll(scriptDir, 0755); err != nil {
		t.Fatalf("MkdirAll() error = %v", err)
	}
	scriptPath := filepath.Join(scriptDir, "pre-push-gate.sh")
	script := "#!/bin/sh\nprintf 'gate passed\\n'\n"
	if err := os.WriteFile(scriptPath, []byte(script), 0755); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	var out bytes.Buffer
	if err := runProjectCheck(context.Background(), projectPath, &out, io.Discard); err != nil {
		t.Fatalf("runProjectCheck() error = %v", err)
	}
	if got := out.String(); !strings.Contains(got, "gate passed") {
		t.Fatalf("stdout = %q, want gate output", got)
	}
}

func TestRunProjectCheckFailsWhenGateMissing(t *testing.T) {
	err := runProjectCheck(context.Background(), t.TempDir(), io.Discard, io.Discard)
	if err == nil {
		t.Fatal("runProjectCheck() error = nil, want missing gate error")
	}
	if !strings.Contains(err.Error(), filepath.Join("scripts", "pre-push-gate.sh")) {
		t.Fatalf("error = %q, want gate path", err)
	}
}

func TestRunProjectCheckReturnsGateFailure(t *testing.T) {
	projectPath := t.TempDir()
	scriptDir := filepath.Join(projectPath, "scripts")
	if err := os.MkdirAll(scriptDir, 0755); err != nil {
		t.Fatalf("MkdirAll() error = %v", err)
	}
	scriptPath := filepath.Join(scriptDir, "pre-push-gate.sh")
	script := "#!/bin/sh\nprintf 'gate failed\\n' >&2\nexit 1\n"
	if err := os.WriteFile(scriptPath, []byte(script), 0755); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	var stderr bytes.Buffer
	err := runProjectCheck(context.Background(), projectPath, io.Discard, &stderr)
	if err == nil {
		t.Fatal("runProjectCheck() error = nil, want gate failure")
	}
	if !strings.Contains(err.Error(), "project validation failed") {
		t.Fatalf("error = %q, want wrapped gate failure", err)
	}
	if got := stderr.String(); !strings.Contains(got, "gate failed") {
		t.Fatalf("stderr = %q, want gate output", got)
	}
}

func restoreCheckTestState(t *testing.T) {
	t.Helper()
	oldRunProjectCheck := runProjectCheck
	t.Cleanup(func() {
		runProjectCheck = oldRunProjectCheck
	})
}
