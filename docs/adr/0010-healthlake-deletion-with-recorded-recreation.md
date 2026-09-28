# ADR-0010: Delete the HealthLake datastore to stop billing, with a recorded and tested recreation workflow

**Status:** implemented (2026-09-19 deletion; 2026-09-20 recreation workflow). Datastore currently deleted;
recreation not executed.

## Context
The HealthLake datastore billed continuously (~$0.27/hour) from Phase 3 onward, including through all of Phase 4's
implementation and verification work, once its purpose (proving live parity) was served. Tearing it down with the
existing Phase 3 `90_teardown.sh` would also have deleted the S3 buckets, KMS key, and IAM roles it shares with
the rest of the deployment — undesired, since Phase 4 and the import artifacts needed to persist.

## Decision
Delete only the datastore (`delete-fhir-datastore`), explicitly not running the broader teardown script. Before
deleting, record everything needed to recreate it identically — region, FHIR version, authorization strategy,
KMS key/alias, analytics status, tags, import job configuration and S3 object manifests — in a committed,
version-controlled file (`infrastructure/aws/healthlake/datastore-recreation-record.json`), pushed *before* the
delete call. Afterward, a dedicated, tested, non-mutating script (`91_recreate.sh`) was built from that record: it
verifies the recorded dependencies still match live state, refuses to reuse either AWS idempotency client token
recorded for the deleted resources, clears only the datastore-specific stale identifiers from local deploy state,
and prints the exact by-hand recreation plan — it creates nothing itself.

## Actual findings
- Deleting the datastore automatically removed the Glue resource-link database HealthLake's analytics integration
  had created, and the Lake Formation permissions that referenced it — confirmed by a before/after comparison, not
  assumed. Everything else (KMS key, both S3 buckets' objects, all five IAM roles, all Phase 4 resources) was
  confirmed unchanged.
- A real latent bug was found and fixed while designing the recreation path: `50_import.sh` would reuse a stale
  `JOB_ID` left in local state by a *previous, now-deleted* datastore instead of re-resolving it live, which would
  have targeted the wrong datastore's import job on the very next recreation attempt.

## Consequences
- Cost stops immediately and completely for the dominant line item, while every other resource (and its ability to
  be redeployed against a fresh datastore) is preserved and independently verified.
- Recreating the datastore is a reviewed, by-hand sequence rather than a single opaque command, and the record
  survives independently of both AWS and this conversation.
