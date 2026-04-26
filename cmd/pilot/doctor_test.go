package main

import (
	"bytes"
	"strings"
	"testing"

	"github.com/qf-studio/pilot/internal/config"
	"github.com/qf-studio/pilot/internal/health"
)

func TestDoctorCommandPrintsHealthyReport(t *testing.T) {
	restoreDoctorTestState(t)
	runHealthChecks = func(cfg *config.Config) *health.HealthReport {
		return &health.HealthReport{
			Dependencies: []health.Check{
				{Name: "git", Status: health.StatusOK, Message: "git version 2.50.0"},
				{Name: "claude", Status: health.StatusOK, Message: "1.0.0 [active backend]"},
			},
			Config: []health.ConfigCheck{
				{Name: "projects", Status: health.StatusOK, Message: "1 project configured"},
			},
			Features: []health.FeatureStatus{{Name: "github", Status: health.StatusOK}},
		}
	}
	cfgFile = writeTempConfig(t)

	cmd := newDoctorCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)

	if err := cmd.Execute(); err != nil {
		t.Fatalf("Execute() error = %v", err)
	}

	got := out.String()
	for _, want := range []string{
		"Pilot Health Check",
		"System Dependencies:",
		"Configuration:",
		"Features Status:",
		"✅ All systems operational!",
		"Run 'pilot setup' for interactive configuration wizard",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("output missing %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "Recommendations:") {
		t.Fatalf("expected no recommendations in healthy report:\n%s", got)
	}
}

func TestDoctorCommandPrintsRecommendationsForBrokenSetup(t *testing.T) {
	restoreDoctorTestState(t)
	runHealthChecks = func(cfg *config.Config) *health.HealthReport {
		return &health.HealthReport{
			Dependencies: []health.Check{
				{Name: "git", Status: health.StatusError, Message: "not found", Fix: "brew install git"},
				{Name: "gh", Status: health.StatusWarning, Message: "not found (PR creation unavailable)", Fix: "brew install gh && gh auth login"},
			},
			Config: []health.ConfigCheck{
				{Name: "projects", Status: health.StatusWarning, Message: "no projects configured", Fix: "add at least one project"},
			},
			Features: []health.FeatureStatus{{
				Name:    "github",
				Status:  health.StatusDisabled,
				Note:    "not configured",
				Missing: []string{"token"},
			}},
		}
	}
	cfgFile = writeTempConfig(t)

	cmd := newDoctorCmd()
	cmd.SetArgs([]string{"--verbose"})
	var out bytes.Buffer
	cmd.SetOut(&out)

	if err := cmd.Execute(); err != nil {
		t.Fatalf("Execute() error = %v", err)
	}

	got := out.String()
	for _, want := range []string{
		"Recommendations:",
		"brew install git",
		"brew install gh && gh auth login",
		"add at least one project",
		"missing: token",
		"❌ Not ready - 1 critical error(s)",
		"Fix required dependencies before running Pilot",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("output missing %q:\n%s", want, got)
		}
	}
}

func restoreDoctorTestState(t *testing.T) {
	t.Helper()
	oldCfgFile := cfgFile
	oldRunHealthChecks := runHealthChecks
	t.Cleanup(func() {
		cfgFile = oldCfgFile
		runHealthChecks = oldRunHealthChecks
	})
}

func writeTempConfig(t *testing.T) string {
	t.Helper()
	configPath := t.TempDir() + "/config.yaml"
	if err := config.Save(config.DefaultConfig(), configPath); err != nil {
		t.Fatalf("config.Save() error = %v", err)
	}
	return configPath
}
