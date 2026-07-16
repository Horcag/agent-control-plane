# Changelog

All notable changes are recorded here. This project follows Keep a Changelog.

## [0.1.0] - 2026-07-16

### Added

- Documented the public alpha compatibility contract, upgrade procedure, and maintainer release checklist.
- Documented durable job, plan, review-inbox, and verification artifacts.
- Added a self-contained offline demo for the durable pipeline.
- Added process-level recovery drills for interrupted dispatch and finalization, PID identity mismatches, SQLite contention, post-checkpoint edits, and explicit restart/retry behavior.
- Decomposed plan lifecycle ownership into `PlanService` and extracted CLI command modules.
