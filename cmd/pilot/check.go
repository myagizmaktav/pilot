package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/spf13/cobra"
)

var runProjectCheck = func(ctx context.Context, projectPath string, stdout, stderr io.Writer) error {
	gateScript := filepath.Join(projectPath, "scripts", "pre-push-gate.sh")
	if _, err := os.Stat(gateScript); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("validation gate not found: %s", gateScript)
		}
		return fmt.Errorf("failed to access validation gate: %w", err)
	}

	checkCmd := exec.CommandContext(ctx, "bash", gateScript)
	checkCmd.Dir = projectPath
	checkCmd.Stdout = stdout
	checkCmd.Stderr = stderr
	if err := checkCmd.Run(); err != nil {
		return fmt.Errorf("project validation failed: %w", err)
	}

	return nil
}

func newCheckCmd() *cobra.Command {
	var projectPath string

	cmd := &cobra.Command{
		Use:           "check",
		Short:         "Run project validation gate",
		SilenceUsage:  true,
		SilenceErrors: true,
		Long: `Run project validation gate to confirm repository builds, tests, and
quality checks pass.

Examples:
  pilot check
  pilot check --project /path/to/repo`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if projectPath == "" {
				cwd, err := os.Getwd()
				if err != nil {
					return fmt.Errorf("failed to get current directory: %w", err)
				}
				projectPath = cwd
			}

			absProjectPath, err := filepath.Abs(projectPath)
			if err != nil {
				return fmt.Errorf("failed to resolve project path: %w", err)
			}

			info, err := os.Stat(absProjectPath)
			if err != nil {
				return fmt.Errorf("failed to access project path: %w", err)
			}
			if !info.IsDir() {
				return fmt.Errorf("project path is not a directory: %s", absProjectPath)
			}

			return runProjectCheck(cmd.Context(), absProjectPath, cmd.OutOrStdout(), cmd.ErrOrStderr())
		},
	}

	cmd.Flags().StringVarP(&projectPath, "project", "p", "", "Project path (default: current directory)")

	return cmd
}
