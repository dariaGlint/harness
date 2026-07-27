# Changelog

All notable changes to this project are documented in this file.

The format follows Keep a Changelog, and this project uses Semantic Versioning.

## [0.1.0] - 2026-07-27

### Added

- fail-closed foreground continuation for resumable command-line workflows;
- atomic JSON state persistence and unfinished-task discovery;
- configurable continuation and terminal exit-code policies;
- timeout recovery that requires valid durable state;
- POSIX and Windows process-group termination;
- deterministic retry chunk shrinking;
- versioned task-state and foreground-report JSON Schemas;
- command-template digests that avoid persisting raw command arguments;
- Linux 3.11, Linux 3.12, and Windows 3.12 CI coverage;
- isolated installed-wheel consumer validation.

### Security

- invalid, corrupt, or validator-rejected state is not treated as resumable;
- report output excludes raw command-template arguments;
- unknown exit codes fail closed.

### Scope

This release contains generic workflow primitives only. It does not include
Chaos-specific stages, gameplay, visual gates, repository policy, or Godot
project paths.
