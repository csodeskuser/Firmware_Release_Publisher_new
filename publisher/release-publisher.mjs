import duckdb from 'duckdb';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';

// Resolve directory roots and paths
const ROOT_DIR = process.cwd();
const MANIFEST_PATH = fs.existsSync(path.join(ROOT_DIR, 'fixtures', 'build_manifest.csv'))
  ? path.join(ROOT_DIR, 'fixtures', 'build_manifest.csv')
  : path.join(ROOT_DIR, 'environment', 'fixtures', 'build_manifest.csv');

const DB_PATH = path.join(ROOT_DIR, 'releases.duckdb');
const GATEWAY_URL = process.env.GATEWAY_URL || 'http://127.0.0.1:7070';

// Current signing keypair locations
const CURRENT_CERT_PATH = process.env.CURRENT_CERT_PATH || (
  fs.existsSync(path.join(ROOT_DIR, 'keys', 'current', 'current.cert.pem'))
    ? path.join(ROOT_DIR, 'keys', 'current', 'current.cert.pem')
    : '/app/keys/current/current.cert.pem'
);

const CURRENT_KEY_PATH = process.env.CURRENT_KEY_PATH || (
  fs.existsSync(path.join(ROOT_DIR, 'keys', 'current', 'current.key.pem'))
    ? path.join(ROOT_DIR, 'keys', 'current', 'current.key.pem')
    : '/app/keys/current/current.key.pem'
);

// Canonical JSON encoding: UTF-8, keys sorted lexicographically, no unnecessary whitespace
function canonicalDescriptor(bundleId, artifactCount, totalBytes) {
  const obj = {
    artifact_count: Number(artifactCount),
    bundle_id: String(bundleId),
    total_bytes: Number(totalBytes),
  };
  const sortedKeys = Object.keys(obj).sort();
  const entries = sortedKeys.map((k) => `${JSON.stringify(k)}:${JSON.stringify(obj[k])}`);
  return `{${entries.join(',')}}`;
}

// Signs descriptor with OpenSSL CMS detached signature
function signDescriptor(descriptorStr, certPath, keyPath) {
  const scratchDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cms-sign-'));
  const descPath = path.join(scratchDir, 'descriptor.bin');
  try {
    fs.writeFileSync(descPath, Buffer.from(descriptorStr, 'utf8'));
    const sig = execFileSync(
      'openssl',
      [
        'cms',
        '-sign',
        '-in', descPath,
        '-signer', certPath,
        '-inkey', keyPath,
        '-outform', 'PEM',
        '-binary',
      ],
      { encoding: 'utf8' }
    );
    return sig;
  } finally {
    fs.rmSync(scratchDir, { recursive: true, force: true });
  }
}

// Promisified DuckDB query runner
function runQuery(db, sql, params = []) {
  return new Promise((resolve, reject) => {
    db.all(sql, ...params, (err, rows) => {
      if (err) return reject(err);
      resolve(rows);
    });
  });
}

function runExec(db, sql) {
  return new Promise((resolve, reject) => {
    db.run(sql, (err) => {
      if (err) return reject(err);
      resolve();
    });
  });
}

async function main() {
  // 1. Initialize DuckDB
  const db = new duckdb.Database(DB_PATH);

  await runExec(db, `
    CREATE TABLE IF NOT EXISTS publications (
      bundle_id VARCHAR PRIMARY KEY,
      request_token VARCHAR NOT NULL,
      publication_id VARCHAR NOT NULL,
      status VARCHAR NOT NULL,
      published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
  `);

  // 2. Reconciliation SQL
  const reconciliationSql = `
    WITH raw_manifest AS (
      SELECT DISTINCT * FROM read_csv_auto('${MANIFEST_PATH}')
    ),
    withdrawals AS (
      SELECT supersedes_id FROM raw_manifest WHERE record_type = 'WITHDRAWAL' AND supersedes_id IS NOT NULL
    ),
    active_builds AS (
      SELECT * FROM raw_manifest
      WHERE record_type = 'BUILD'
        AND entry_id NOT IN (SELECT supersedes_id FROM withdrawals)
    )
    SELECT
      bundle_id,
      COUNT(*) AS artifact_count,
      CAST(SUM(size_bytes) AS BIGINT) AS total_bytes
    FROM active_builds
    GROUP BY bundle_id
    HAVING COUNT(*) > 0
    ORDER BY bundle_id ASC;
  `;

  const publishableBundles = await runQuery(db, reconciliationSql);

  // 3. Fetch current signing key metadata from Gateway
  let keyMetadata;
  try {
    const keyRes = await fetch(`${GATEWAY_URL}/v1/signing-key/current`);
    if (!keyRes.ok) {
      throw new Error(`Failed to fetch current key: HTTP ${keyRes.status}`);
    }
    keyMetadata = await keyRes.json();
  } catch (err) {
    console.error(`Error connecting to gateway: ${err.message}`);
    process.exit(1);
  }

  const keyId = keyMetadata.key_id;

  // 4. Publish bundles and persist receipts
  for (const bundle of publishableBundles) {
    const bundleId = bundle.bundle_id;
    const requestToken = `token-${bundleId}`;
    const descriptor = canonicalDescriptor(bundleId, bundle.artifact_count, bundle.total_bytes);

    // Sign with active current key
    const signature = signDescriptor(descriptor, CURRENT_CERT_PATH, CURRENT_KEY_PATH);

    // Output SIGNED status line
    console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);

    // Submit to gateway
    const submitRes = await fetch(`${GATEWAY_URL}/v1/publications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        descriptor,
        signature,
        request_token: requestToken,
      }),
    });

    if (!submitRes.ok) {
      const errorText = await submitRes.text();
      throw new Error(`Submission failed for ${bundleId}: ${errorText}`);
    }

    const receipt = await submitRes.json();
    const publicationId = receipt.publication_id;
    const status = receipt.status;

    // Persist to DuckDB (Upsert / Insert or Replace)
    await runExec(db, `
      INSERT OR REPLACE INTO publications (bundle_id, request_token, publication_id, status)
      VALUES ('${bundleId}', '${requestToken}', '${publicationId}', '${status}');
    `);

    // Output PUBLISHED status line
    console.log(`BUNDLE ${bundleId} PUBLISHED RECEIPT=${publicationId} TOKEN=${requestToken} STATUS=${status}`);
  }

  db.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
