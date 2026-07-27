# Changelog

All notable changes to this project are documented in this file.

The format follows Keep a Changelog, and this project uses Semantic Versioning.

## [Unreleased]

### Added

- `Workspace Commit Bridge` library and CLI for checkpoint ZIP publication;
- GitHub App installation-token compatible REST adapter;
- packaged v1 handoff and repository-policy JSON Schemas and contract documentation;
- exact file operation, digest, tree, commit, branch, and compare verification;
- repository-owned Issue Work Claim and canonical commit-message hook integration;
- structured success/rejection results and Draft-only optional PR creation.

### Security

- checkpoint ZIP traversal, absolute-path, symlink, encryption, duplicate,
  case-collision, expansion-ratio, size-limit, forbidden-artifact, UTF-8, and digest checks;
- stale bases are rebased only when selected paths and direct dependencies are
  unchanged;
- default branches and unrelated existing work branches are never overwritten.

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
