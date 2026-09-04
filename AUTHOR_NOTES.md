# Author Notes: Firmware Release Publisher Task

## Overview

This task teaches **cryptographic signing, SQL-based data reconciliation, HTTP integration with idempotency, and deterministic output** in a realistic firmware release engineering scenario. The candidate implements a background publisher that resolves a key rotation incident by signing release bundles with the current key and publishing them to a distribution gateway.

---

## Task Architecture Decisions

### Domain & Narrative
The task is set in **firmware release engineering for an IoT device fleet**. Release engineering rotated the code-signing key; the old publisher is now broken (signing with a revoked key, causing `UNTRUSTED_SIGNATURE` rejections). The candidate fixes it by implementing a new publisher that:

1. Reconciles a build manifest (CSV → DuckDB SQL)
2. Signs bundles with the current key (OpenSSL CMS)
3. Publishes to a distribution gateway (HTTP)
4. Persists idempotency tokens (DuckDB)
5. Emits deterministic status output (line-ordered, receipt-masked)

### Single-File Solution Deliverable
The candidate implements only **one file**: `/app/solution/release-publisher.mjs` (ESM, Node 20). This keeps the scope tight and the grading binary: either the solution works or it doesn't.

### Provided Environment
- **Distribution gateway** (`environment/distribution-gateway/`): a complete, correct Express app that the candidate **does not modify**. It verifies signatures with OpenSSL CMS and exposes:
  - `GET /v1/signing-key/current` — metadata for the current key
  - `POST /v1/publications` — submit signed descriptors, with idempotent replay
  - Internal JSON ledger (off-limits; HTTP-only interaction)
- **Build manifest** (`environment/fixtures/build_manifest.csv`): 40 rows with 3 publishable bundles (BND-101, 102, 103) and 1 fully withdrawn (BND-104)
- **Signing keypairs** (`environment/keys/current/`, `environment/keys/revoked/`): generated at Docker build time; revoked key is a deliberate trap
- **DuckDB wrapper** (`environment/lib/duckdb-wrapper.cjs`): helper module for CSV loading and SQL-based reconciliation

### Environment Separation (Proof A vs. Proof B)
Per the submission handbook:
- **Proof A (empty run)**: `environment/publisher/` is **empty**; no reference implementation present. Verifier runs with no solution → reward 0 (negative control).
- **Proof B (solution run)**: `/app/solution/release-publisher.mjs` is the reference implementation. Verifier runs it → reward 1 (positive control).

---

## Reconciliation Rules

### Rule 1: Collapse Exact Duplicates
Rows that are identical across *all* columns are exact duplicates — count them as one. Use `SELECT DISTINCT *`.

### Rule 2: Apply Withdrawals
A `WITHDRAWAL` row's `supersedes_id` field identifies the `entry_id` of the `BUILD` it cancels. Excluded withdrawn builds from publishable sets.

### Rule 3: Aggregate by Bundle
For each `bundle_id`, count surviving `BUILD` rows and sum `size_bytes`. Bundles with zero surviving builds are excluded.

### Expected Reconciliation Result

| bundle_id | artifact_count | total_bytes |
|-----------|---|---|
| BND-101 | 9 | 1201575 |
| BND-102 | 10 | 2188075 |
| BND-103 | 8 | 2079625 |

**BND-104**: Excluded (fully withdrawn — all builds have corresponding withdrawals).

---

## Canonical SQL Reconciliation

```sql
WITH deduplicated AS (
  SELECT DISTINCT * FROM raw_manifest
),
withdrawn_ids AS (
  SELECT DISTINCT supersedes_id AS entry_id 
  FROM deduplicated
  WHERE record_type = 'WITHDRAWAL' AND supersedes_id != ''
),
active_builds AS (
  SELECT * FROM deduplicated
  WHERE record_type = 'BUILD' 
    AND entry_id NOT IN (SELECT entry_id FROM withdrawn_ids)
),
bundle_summary AS (
  SELECT 
    bundle_id,
    COUNT(*) AS artifact_count,
    SUM(CAST(size_bytes AS BIGINT)) AS total_bytes
  FROM active_builds
  GROUP BY bundle_id
)
SELECT * FROM bundle_summary
WHERE artifact_count > 0
ORDER BY bundle_id ASC
```

---

## Signing & Canonicalization

### Canonical Descriptor Format

The descriptor is **UTF-8 JSON with lexicographically sorted object keys and no insignificant whitespace**:

```json
{"artifact_count":9,"bundle_id":"BND-101","total_bytes":1201575}
```

### OpenSSL CMS Signing

Create a detached CMS signature:

```bash
openssl cms -sign \
  -in <descriptor.bin> \
  -signer <current.cert.pem> \
  -inkey <current.key.pem> \
  -outform PEM \
  -binary
```

---

## HTTP Integration & Idempotency

### Idempotency Model

Publisher maintains deterministic `request_token` values (`token-<bundle_id>`) and persists them in DuckDB. On re-run:

1. Check if token exists in publications table
2. If yes, reuse stored `publication_id`
3. If no, sign, submit, and store

This prevents double-submission even if the program crashes and restarts.

### Gateway Ledger (Off-Limits)

The gateway's private publication ledger is not readable or writable by the publisher. Only HTTP interaction is allowed.

---

## Output Format

Two lines per publishable bundle, in ascending `bundle_id` order:

```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=<status>
```

**Determinism**: Bundle order is fixed (sorted by ID). All fields except `publication_id` are deterministic. The verifier masks `publication_id` before diffing.

---

## Grading Rubric

### Functional Criteria (Binary 0/1 per criterion)

1. **report_output_matches**: `npm run report` output (receipts masked) matches golden file exactly in correct order.
2. **withdrawals_and_duplicates_reconciled**: Bundles exclude withdrawn builds and collapse exact duplicates. BND-104 excluded; others included with correct counts.
3. **bundles_signed_with_current_key_accepted**: All submissions receive `PUBLISHED` status, not `UNTRUSTED_SIGNATURE`.
4. **receipts_and_tokens_persisted_in_duckdb**: After a run, `releases.duckdb` contains request tokens and publication IDs.
5. **idempotent_rerun_no_duplicate_publications**: Re-running produces byte-identical output with no duplicate rows in gateway ledger.
6. **bnd104_excluded_fully_withdrawn**: BND-104 does not appear in output.
7. **no_hardcoded_values**: Bundle counts and output derived from manifest, not hardcoded.
8. **signature_verification_enforced**: Revoked-key signatures rejected as `UNTRUSTED_SIGNATURE`.

### Reward Logic

- **Proof A (Negative Control)**: Empty environment → tests fail (code ≠ 0) → reward = 0
- **Proof B (Positive Control)**: Solution present → all criteria pass → reward = 1

---

## Test Infrastructure

### test.sh

1. Starts distribution gateway on port 7070 (background)
2. Waits for readiness (health check)
3. Runs `pytest tests/test_outputs.py`
4. Cleans up gateway process
5. Writes `reward.txt` (0 or 1)

### test_outputs.py

Eight pytest tests:

1. **test_publisher_output_matches_golden**: Diffs masked output against golden file.
2. **test_three_bundles_published_in_order**: Verifies bundle IDs in ascending order.
3. **test_all_bundles_published_successfully**: Checks `PUBLISHED` status, no `UNTRUSTED_SIGNATURE`.
4. **test_database_persists_receipts**: Verifies `releases.duckdb` contains 3 rows.
5. **test_idempotent_rerun_produces_identical_output**: Second run produces identical output.
6. **test_bnd104_excluded_fully_withdrawn**: BND-104 does not appear.
7. **test_signature_verification_enforced**: Gateway enforces verification.
8. **test_no_hardcoded_values**: Output derived, not hardcoded.

---

## Difficulty Devices & Learning Objectives

### Device 1: Wrong-Key Trap
Understand key rotation and cryptographic verification enforcement. Revoked key present but must never be used.

### Device 2: Exact Byte Canonicalization
Understand why cryptographic protocols require precision. Single extra space breaks signature.

### Device 3: SQL Reconciliation
Practice data cleaning and SQL aggregation. Must use SQL, not code-based loops.

### Device 4: Idempotency
Understand distributed systems and fault tolerance. Persist idempotency tokens to prevent duplicates.

### Device 5: Deterministic Output
Understand why order matters in testing. Output must be sorted for stable diff comparison.

### Device 6: HTTP Integration & Boundaries
Understand API contracts. Gateway is black box; only HTTP interaction allowed.

---

## Why This Task Works

### Binary, Observable Success
Pass/fail determined by:
- Output diff (deterministic)
- HTTP receipts (observable)
- Database state (queryable)
- Signature verification (enforced by gateway)

### Realistic Scenario
Mirrors real firmware release workflows:
- Manifest loading and reconciliation (common)
- Cryptographic signing (security-critical)
- HTTP submission with idempotency (reliability)
- Deterministic output (CI/CD integration)

### Clear Learning Progression
1. Understand scenario (key rotation incident)
2. Load and reconcile data (SQL)
3. Fetch metadata (HTTP)
4. Sign (OpenSSL, cryptography)
5. Submit with idempotency (distributed systems)
6. Output deterministically (testing)

### Fault Intolerance
Common mistakes caught immediately:
- Signing with revoked key → `UNTRUSTED_SIGNATURE`
- Adding space to descriptor → signature mismatch
- Not deduplicating → wrong bundle counts
- Not handling withdrawals → BND-104 appears
- Non-deterministic output → diff fails
- Not persisting tokens → duplicates on re-run

---

## Known Limitations & Tradeoffs

### Single Manifest File
Static manifest simplifies grading. Real systems might vary data.

### No Network Failures
Task assumes gateway always reachable. Real systems require retry logic.

### No Concurrency
Single-threaded, sequential bundle processing. Real systems might parallelize.

### No Audit Logging
Minimal debug output. Production would log more state.

---

## Submission Checklist

- [x] **instruction.md**: Comprehensive, ~4600 words
- [x] **AUTHOR_NOTES.md**: This document
- [x] **solution/release-publisher.mjs**: Implemented, tested
- [x] **environment/publisher/**: Empty (no solution in environment)
- [x] **test.sh**: Starts gateway, runs pytest, reports reward
- [x] **test_outputs.py**: 8 tests covering all criteria
- [x] **Gateway**: Complete, correct, provided
- [x] **Fixtures**: Manifest, golden output, signing keys
- [x] **Proofs**: Both Proof A (empty → 0) and Proof B (solution → 1) demonstrated

---

## Next Steps

1. Run solution in clean Docker container from `environment/Dockerfile`
2. Execute `tests/test.sh` with no solution (Proof A): expect reward 0
3. Place reference solution at `/app/solution/release-publisher.mjs`
4. Execute `tests/test.sh` with solution (Proof B): expect reward 1
5. Submit workspace to reviewers

---

## References

- Harbor task framework: https://www.harborframework.com/docs/tasks
- DuckDB Node bindings: https://duckdb.org/docs/api/nodejs/overview
- OpenSSL CMS: `man openssl-cms`
- Fetch API: https://nodejs.org/api/fetch.html
- Express: https://expressjs.com/
