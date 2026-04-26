# Ops telemetry is surfaced through /ops/command-center/ — Django admin is
# intentionally not registered for OddsApiUsage / CronRunLog. Both tables
# are append-only and grow quickly; the visual dashboard is the primary UI.
