# backup-restore Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Backup scope

A CyberFS backup SHALL consist of a consistent Postgres dump of all metadata — including nodes, versions, grants, public links, wrapped KEKs, wrapped DEKs, quotas, and audit records — together with a mirror of the MinIO content bucket. A backup SHALL be sufficient, given the deployment `MASTER_KEY`, to reconstitute the service completely.

#### Scenario: Metadata captured

- **WHEN** a backup runs
- **THEN** it SHALL produce a Postgres dump covering every application table, taken at a single consistent snapshot

#### Scenario: Objects mirrored

- **WHEN** a backup runs
- **THEN** it SHALL mirror every object in the content bucket to the backup target, including all retained versions

#### Scenario: Restore reproduces the service

- **WHEN** a backup is restored into an empty stack together with the correct `MASTER_KEY`
- **THEN** every file SHALL be downloadable with content identical to the source, and every grant, quota, and public link SHALL be intact

#### Scenario: Encrypted content stays encrypted in the backup

- **WHEN** objects are mirrored
- **THEN** encrypted files SHALL be copied as ciphertext and SHALL NOT be decrypted at any point in the backup pipeline

### Requirement: Master key is out of band

`MASTER_KEY` SHALL NOT be written into any backup artifact. Backups SHALL be useless for reading encrypted content without it, and this SHALL be stated in the restore runbook.

#### Scenario: Key absent from artifacts

- **WHEN** any backup artifact is inspected
- **THEN** it SHALL contain no copy of `MASTER_KEY`, in plain or encrypted form

#### Scenario: Restore without the key is partially degraded

- **WHEN** a restore is attempted without the correct `MASTER_KEY`
- **THEN** unencrypted files SHALL be readable, encrypted files SHALL fail with a clear `key_unavailable` error, and the service SHALL NOT report itself healthy

#### Scenario: Runbook states the dependency

- **WHEN** the restore runbook is read
- **THEN** it SHALL identify `MASTER_KEY` custody as a prerequisite and describe where it is held

### Requirement: Scheduling and target

Backups SHALL run on a schedule defined by `BACKUP_CRON`, SHALL write to an S3-compatible target distinct from the primary MinIO deployment, and SHALL be disabled cleanly when `BACKUP_ENABLED` is false.

#### Scenario: Scheduled run

- **WHEN** the configured schedule fires
- **THEN** a backup SHALL start, and its start, end, duration, byte count, and outcome SHALL be recorded

#### Scenario: Manual run

- **WHEN** an administrator triggers a backup
- **THEN** it SHALL run with the same procedure as a scheduled one and SHALL be audited

#### Scenario: Overlapping runs prevented

- **WHEN** a scheduled run fires while a previous run is still in progress
- **THEN** the new run SHALL be skipped and the skip SHALL be recorded

#### Scenario: Backups disabled

- **WHEN** `BACKUP_ENABLED` is false
- **THEN** no backup SHALL run and the health view SHALL report backups as intentionally disabled rather than failing

#### Scenario: Target separate from primary

- **WHEN** the backup target is configured identically to the primary MinIO endpoint and bucket
- **THEN** startup validation SHALL reject the configuration

### Requirement: Integrity verification

Every backup SHALL be verified before it is marked successful, and verification failure SHALL be treated as backup failure.

#### Scenario: Manifest written

- **WHEN** a backup completes its copy phase
- **THEN** it SHALL write a manifest listing every object key, its size, and its checksum, plus the dump's checksum and the schema migration revision

#### Scenario: Checksums verified

- **WHEN** verification runs
- **THEN** it SHALL confirm the dump's checksum and sample at least `BACKUP_VERIFY_SAMPLE_COUNT` objects against the manifest

#### Scenario: Mismatch fails the backup

- **WHEN** any verified checksum does not match
- **THEN** the backup SHALL be marked failed, SHALL NOT count toward retention, and an alert SHALL be emitted

#### Scenario: Dump/mirror skew detected

- **WHEN** the manifest references an object that the metadata dump does not, or vice versa, beyond the tolerated in-flight window
- **THEN** the discrepancy SHALL be reported in the backup record

### Requirement: Retention

Backups SHALL be retained on a configurable policy and pruned automatically, and pruning SHALL never remove the most recent verified backup.

#### Scenario: Policy applied

- **WHEN** retention runs
- **THEN** it SHALL keep `BACKUP_KEEP_DAILY` daily, `BACKUP_KEEP_WEEKLY` weekly, and `BACKUP_KEEP_MONTHLY` monthly backups and delete the rest

#### Scenario: Last good backup protected

- **WHEN** retention would delete the only verified backup
- **THEN** it SHALL retain it and log a warning

#### Scenario: Failed backups pruned aggressively

- **WHEN** a backup is marked failed
- **THEN** its partial artifacts SHALL be removed after `BACKUP_FAILED_GRACE_HOURS`

### Requirement: Restore procedure

CyberFS SHALL ship a documented, scripted restore procedure that takes a named backup and a target stack, and SHALL support restoring into a scratch environment without touching production.

#### Scenario: Full restore scripted

- **WHEN** the restore command is run against an empty stack with a chosen backup identifier
- **THEN** it SHALL load the dump, apply migrations to the recorded revision, mirror objects into the target bucket, and report success only if the service passes its readiness probe

#### Scenario: Restore is non-destructive by default

- **WHEN** a restore targets a stack that already contains data
- **THEN** it SHALL refuse unless an explicit destructive confirmation flag is supplied

#### Scenario: Point-in-time selection

- **WHEN** an operator lists available backups
- **THEN** each SHALL be identified by timestamp, verification state, size, and schema revision

#### Scenario: Migration skew handled

- **WHEN** a backup's recorded schema revision is older than the deployed code
- **THEN** the restore SHALL apply the intervening migrations and report the upgrade path taken

### Requirement: Restore is tested automatically

The test suite SHALL include an integration test that performs a real backup and a real restore against live Postgres, Redis, and MinIO containers, and asserts byte-level fidelity.

#### Scenario: Round trip asserted

- **WHEN** the restore integration test runs
- **THEN** it SHALL seed a tree containing encrypted files, unencrypted files, multiple versions, shares, and trashed nodes; back it up; restore into a scratch stack; and assert that every file's downloaded bytes match the original

#### Scenario: Encrypted files readable after restore

- **WHEN** the restored stack is given the same `MASTER_KEY`
- **THEN** encrypted files SHALL decrypt correctly for their owner and for share recipients

#### Scenario: Test fails on silent data loss

- **WHEN** any node, version, grant, or public link present before backup is missing after restore
- **THEN** the test SHALL fail

#### Scenario: Restore test runs in CI

- **WHEN** the CI integration job runs
- **THEN** the backup/restore round trip SHALL be part of it

### Requirement: Backup observability

Backup state SHALL be visible to operators without inspecting the storage target by hand.

#### Scenario: Status exposed

- **WHEN** an administrator opens the health view
- **THEN** it SHALL show the last backup's time, outcome, duration, size, and verification state

#### Scenario: Staleness alerts

- **WHEN** no verified backup has completed within `BACKUP_MAX_AGE_HOURS`
- **THEN** the system SHALL surface an alert-level condition on the health endpoint and in metrics

#### Scenario: Failures are loud

- **WHEN** a backup fails
- **THEN** an error-level log and a failure metric SHALL be emitted, and the failure SHALL persist in the backup history

#### Scenario: History retained

- **WHEN** backup history is queried
- **THEN** it SHALL cover at least the last `BACKUP_HISTORY_DAYS` days including failures

