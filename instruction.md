# Firmware Release Publisher

Release engineering recently rotated the firmware **code-signing key**. Since the rotation, every release bundle submitted by the publisher to the distribution gateway is rejected with `UNTRUSTED_SIGNATURE` because releases are still being signed with the revoked key.

Your task is to implement the release publisher script at:

```
publisher/release-publisher.mjs
```

When invoked via `npm run report` (or `node publisher/release-publisher.mjs --report`), it must ingest the raw build manifest, reconcile builds and withdrawals with SQL, generate detached OpenSSL CMS signatures using the current signing key, submit the bundles to the Express distribution gateway, persist receipts in DuckDB, and print deterministic status lines.

---

## 1. Input Data & Reconciliation Rules

The raw manifest is provided at `fixtures/build_manifest.csv` with columns:
`entry_id,bundle_id,component_id,version,size_bytes,record_type,supersedes_id,recorded_at`

You must import this CSV into a local DuckDB database (`releases.duckdb`) and apply the following reconciliation rules using SQL:

1. **Deduplication:** Collapse exact duplicate rows (rows identical across every column).
2. **Withdrawals:** If a row has `record_type = 'WITHDRAWAL'`, its `supersedes_id` specifies the `entry_id` of the `BUILD` row that it cancels. Cancelled builds must be excluded from publication.
3. **Eligible Bundles:** A bundle is publishable if and only if it has at least one surviving build after applying withdrawals and deduplication. Bundles where every build was withdrawn must be omitted entirely.
4. **Bundle Metrics:** For each eligible bundle, compute:
   - `artifact_count`: total count of surviving build records for this bundle.
   - `total_bytes`: sum of `size_bytes` of all surviving build records for this bundle.

---

## 2. Cryptographic Signing & Canonical Descriptors

The Express distribution gateway is running on `http://127.0.0.1:7070`.

1. **Signing Key Metadata:** Fetch the current key metadata by sending a `GET` request to:
   `http://127.0.0.1:7070/v1/signing-key/current`
   This returns `{ key_id, algorithm, certificate_ref, status }`.

2. **Canonical Descriptor:** For each publishable bundle, construct a canonical JSON descriptor string formatted in UTF-8 with object keys sorted lexicographically and no insignificant whitespace:
   ```json
   {"artifact_count":<count>,"bundle_id":"<bundle_id>","total_bytes":<bytes>}
   ```

3. **Detached CMS Signature:** Sign the exact bytes of the canonical descriptor using OpenSSL detached CMS signature with the **current** keypair located at:
   - Certificate: `keys/current/current.cert.pem`
   - Private Key: `keys/current/current.key.pem`

   *(⚠️ Warning: Never use `keys/revoked/` — doing so will fail verification with `UNTRUSTED_SIGNATURE`.)*

   Command pattern:
   ```bash
   openssl cms -sign -in <descriptor_file> -signer keys/current/current.cert.pem -inkey keys/current/current.key.pem -outform PEM -binary
   ```

---

## 3. Gateway Submission & Persistence

1. **Submit Publication:** Send a `POST` request to `http://127.0.0.1:7070/v1/publications` with JSON body:
   ```json
   {
     "descriptor": "<canonical_descriptor_string>",
     "signature": "<pem_signature_string>",
     "request_token": "token-<bundle_id>"
   }
   ```
   On success (HTTP 200), the gateway returns:
   ```json
   {
     "publication_id": "<publication_id>",
     "request_token": "token-<bundle_id>",
     "status": "PUBLISHED"
   }
   ```

2. **Persistence & Idempotency:** Persist the publication receipt (`publication_id`, `request_token`, `status`, etc.) in `releases.duckdb`. On repeated runs, the publisher must be idempotent (re-submitting with the same `request_token` replays the original receipt without creating duplicate publication records on the gateway).

---

## 4. Deterministic Output

For each publishable bundle, sorted in **ascending order of `bundle_id`**, print two deterministic lines to standard output:

```text
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=PUBLISHED
```

The output must match the structure of `reports/publications.expected.txt`.

---

## 5. Constraints

- Deliverable must be in `publisher/release-publisher.mjs`.
- Communicate with the gateway only via HTTP; do not read or modify internal files in `distribution-gateway/data/`.
- Do not hardcode values, receipt IDs, or bundle lists; all computations must be derived dynamically from the CSV manifest and gateway endpoints.
