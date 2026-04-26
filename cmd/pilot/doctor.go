package main

import (
	"fmt"
	"io"
	"strings"

	"github.com/qf-studio/pilot/internal/config"
	"github.com/qf-studio/pilot/internal/health"
	"github.com/spf13/cobra"
)

var runHealthChecks = health.RunChecks

func newDoctorCmd() *cobra.Command {
	var verbose bool

	cmd := &cobra.Command{
		Use:   "doctor",
		Short: "Check system health and configuration",
		Long: `Run health checks on system dependencies, configuration, and features.

Shows what's working, what's missing, and how to fix issues.

Examples:
  pilot doctor           # Run all checks
  pilot doctor --verbose # Show detailed output`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Load config
			cfg, err := loadConfig()
			if err != nil {
				cfg = config.DefaultConfig()
			}

			// Run health checks
			report := runHealthChecks(cfg)
			renderDoctorReport(cmd.OutOrStdout(), report, verbose)

			return nil
		},
	}

	cmd.Flags().BoolVarP(&verbose, "verbose", "v", false, "Show detailed output with fix suggestions")

	return cmd
}

func renderDoctorReport(out io.Writer, report *health.HealthReport, verbose bool) {
	// Header
	fmt.Fprintln(out)
	fmt.Fprintln(out, "Pilot Health Check")
	fmt.Fprintln(out, "==================")
	fmt.Fprintln(out)

	// Dependencies
	fmt.Fprintln(out, "System Dependencies:")
	for _, d := range report.Dependencies {
		symbol := d.Status.ColorSymbol()
		fmt.Fprintf(out, "  %s %-12s %s\n", symbol, d.Name, d.Message)
		if verbose && d.Fix != "" && d.Status != health.StatusOK {
			fmt.Fprintf(out, "                    → %s\n", d.Fix)
		}
	}
	fmt.Fprintln(out)

	// Configuration
	fmt.Fprintln(out, "Configuration:")
	for _, c := range report.Config {
		symbol := c.Status.ColorSymbol()
		fmt.Fprintf(out, "  %s %-24s %s\n", symbol, c.Name, c.Message)
		if verbose && c.Fix != "" && c.Status != health.StatusOK {
			fmt.Fprintf(out, "                              → %s\n", c.Fix)
		}
	}
	fmt.Fprintln(out)

	// Features
	fmt.Fprintln(out, "Features Status:")
	for _, f := range report.Features {
		symbol := f.Status.ColorSymbol()
		note := ""
		if f.Note != "" {
			note = " (" + f.Note + ")"
		}
		fmt.Fprintf(out, "  %s %-16s%s\n", symbol, f.Name, note)
		if verbose && len(f.Missing) > 0 {
			fmt.Fprintf(out, "                     missing: %s\n", strings.Join(f.Missing, ", "))
		}
	}
	fmt.Fprintln(out)

	// Summary and recommendations
	errors, warnings := report.Summary()
	if errors > 0 || warnings > 0 {
		fmt.Fprintln(out, "Recommendations:")
		shown := 0
		maxRecs := 5

		// Show critical fixes first
		for _, d := range report.Dependencies {
			if d.Status == health.StatusError && d.Fix != "" && shown < maxRecs {
				fmt.Fprintf(out, "  %d. %s: %s\n", shown+1, d.Name, d.Fix)
				shown++
			}
		}
		for _, c := range report.Config {
			if c.Status == health.StatusError && c.Fix != "" && shown < maxRecs {
				fmt.Fprintf(out, "  %d. %s: %s\n", shown+1, c.Name, c.Fix)
				shown++
			}
		}

		// Then warnings
		for _, d := range report.Dependencies {
			if d.Status == health.StatusWarning && d.Fix != "" && shown < maxRecs {
				fmt.Fprintf(out, "  %d. %s: %s\n", shown+1, d.Name, d.Fix)
				shown++
			}
		}
		for _, c := range report.Config {
			if c.Status == health.StatusWarning && c.Fix != "" && shown < maxRecs {
				fmt.Fprintf(out, "  %d. %s: %s\n", shown+1, c.Name, c.Fix)
				shown++
			}
		}

		fmt.Fprintln(out)
	}

	// Final status
	if report.ReadyToStart() {
		if errors == 0 && warnings == 0 {
			fmt.Fprintln(out, "✅ All systems operational!")
		} else if errors == 0 {
			fmt.Fprintf(out, "✅ Ready to start (%d warning(s))\n", warnings)
		} else {
			fmt.Fprintf(out, "⚠️  Ready with issues (%d error(s), %d warning(s))\n", errors, warnings)
		}
	} else {
		fmt.Fprintf(out, "❌ Not ready - %d critical error(s)\n", errors)
		fmt.Fprintln(out, "   Fix required dependencies before running Pilot")
	}
	fmt.Fprintln(out)

	// Helpful next steps
	fmt.Fprintln(out, "Run 'pilot setup' for interactive configuration wizard")
}
