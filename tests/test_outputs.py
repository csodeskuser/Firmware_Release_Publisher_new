"""Verifier tests for the Firmware Release Publisher task.

Each test maps to a functional_criteria[] entry in scaffold_plan.yaml.
Tests verify:
1. report_output_matches
2. withdrawals_and_duplicates_reconciled
3. bundles_signed_with_current_key_accepted
4. receipts_and_tokens_persisted_in_duckdb
5. idempotent_rerun_no_duplicate_publications
6. revoked_key_signature_rejected
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import duckdb
import pytest
import requests

APP_ROOT = Path("/app") if Path("/app").exists() else Path.cwd()
GATEWAY_URL = "http://127.0.0.1:7070"
REPORT_SCRIPT = "publisher/release-publisher.mjs"
EXPECTED_REPORT_PATH = APP_ROOT / "environment" / "reports" / "publications.expected.txt" if (APP_ROOT / "environment" / "reports").exists() else APP_ROOT / "reports" / "publications.expected.txt"
MANIFEST_PATH = APP_ROOT / "environment" / "fixtures" / "build_manifest.csv" if (APP_ROOT / "environment" / "fixtures").exists() else APP_ROOT / "fixtures" / "build_manifest.csv"
DB_PATH = APP_ROOT / "releases.duckdb"


def mask_receipts(text: str) -> str:
    """Masks non-deterministic RECEIPT=<id> tokens for comparison against golden."""
    return re.sub(r"RECEIPT=\S+", "RECEIPT=<id>", text.strip())


@pytest.fixture(scope="session", autouse=True)
def gateway_service():
    """Ensures the Express distribution gateway is running during the test session."""
    # Check if already running
    try:
        r = requests.get(f"{GATEWAY_URL}/healthz", timeout=1)
        if r.status_code == 200:
            yield
            return
    except requests.RequestException:
        pass

    # Start gateway if not running
    gateway_dir = APP_ROOT / "environment" / "distribution-gateway" if (APP_ROOT / "environment" / "distribution-gateway").exists() else APP_ROOT / "distribution-gateway"
    server_js = gateway_dir / "server.js"
    if not server_js.exists():
        server_js = APP_ROOT / "distribution-gateway" / "server.js"

    proc = subprocess.Popen(
        ["node", str(server_js)],
        cwd=str(gateway_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for gateway to become ready
    ready = False
    for _ in range(30):
        try:
            r = requests.get(f"{GATEWAY_URL}/healthz", timeout=1)
            if r.status_code == 200:
                ready = True
                break
        except requests.RequestException:
            time.sleep(0.1)

    if not ready:
        proc.kill()
        pytest.fail("Express distribution gateway failed to start on port 7070.")

    yield

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_publisher() -> subprocess.CompletedProcess:
    """Executes the candidate release publisher."""
    script_path = APP_ROOT / REPORT_SCRIPT
    if not script_path.exists():
        pytest.fail(f"Publisher script not found at {script_path}")

    return subprocess.run(
        ["node", str(script_path), "--report"],
        cwd=str(APP_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_report_output_matches():
    """functional_criteria[id=report_output_matches]: Running `npm run report`
    produces CLI status lines that match the expected golden output exactly,
    including a stable, deterministic ordering of bundles."""
    res = run_publisher()
    assert res.returncode == 0, f"Publisher exited with code {res.returncode}. Stderr: {res.stderr}"

    if not EXPECTED_REPORT_PATH.exists():
        pytest.fail(f"Expected golden report file not found at {EXPECTED_REPORT_PATH}")

    expected_raw = EXPECTED_REPORT_PATH.read_text(encoding="utf-8")
    expected_masked = mask_receipts(expected_raw)
    actual_masked = mask_receipts(res.stdout)

    assert actual_masked == expected_masked, (
        f"CLI output did not match golden snapshot.\n"
        f"Expected:\n{expected_masked}\n\nActual:\n{actual_masked}"
    )


def test_withdrawals_and_duplicates_reconciled():
    """functional_criteria[id=withdrawals_and_duplicates_reconciled]: The bundles
    derived from the build manifest via SQL exclude withdrawn builds and collapse
    duplicate rows, so bundle membership reflects the reconciled manifest."""
    # Compute ground-truth reconciled bundle set directly from CSV
    con = duckdb.connect()
    con.execute(f"""
        WITH raw_manifest AS (
            SELECT DISTINCT * FROM read_csv_auto('{MANIFEST_PATH}')
        ),
        withdrawals AS (
            SELECT supersedes_id FROM raw_manifest WHERE record_type = 'WITHDRAWAL' AND supersedes_id IS NOT NULL
        ),
        active_builds AS (
            SELECT * FROM raw_manifest
            WHERE record_type = 'BUILD'
              AND entry_id NOT IN (SELECT supersedes_id FROM withdrawals)
        )
        SELECT bundle_id, COUNT(*) AS artifact_count, SUM(size_bytes) AS total_bytes
        FROM active_builds
        GROUP BY bundle_id
        HAVING COUNT(*) > 0
        ORDER BY bundle_id;
    """)
    canonical_bundles = con.fetchall()
    con.close()

    expected_bundle_ids = [row[0] for row in canonical_bundles]
    assert "BND-104" not in expected_bundle_ids, "BND-104 should be completely excluded as all its builds were withdrawn"
    assert expected_bundle_ids == ["BND-101", "BND-102", "BND-103"]

    # Verify DuckDB database contains persisted publications matching the reconciled bundles
    assert DB_PATH.exists(), f"releases.duckdb not found at {DB_PATH}"
    db_con = duckdb.connect(str(DB_PATH), read_only=True)
    tables = [t[0] for t in db_con.execute("SHOW TABLES").fetchall()]
    assert len(tables) > 0, "No tables found in releases.duckdb"

    # Search for token or bundle entries in database
    db_dump = ""
    for t in tables:
        rows = db_con.execute(f"SELECT * FROM {t}").fetchall()
        db_dump += str(rows)
    db_con.close()

    for bnd in expected_bundle_ids:
        assert bnd in db_dump or f"token-{bnd}" in db_dump, f"Bundle {bnd} not found in database records"
    assert "BND-104" not in db_dump, "Withdrawn bundle BND-104 was erroneously persisted in releases.duckdb"


def test_bundles_signed_with_current_key_accepted():
    """functional_criteria[id=bundles_signed_with_current_key_accepted]: Signed
    descriptors submitted to POST /v1/publications are accepted (status PUBLISHED),
    demonstrating the publisher signs with the current key rather than the revoked one."""
    res = run_publisher()
    assert res.returncode == 0
    lines = res.stdout.strip().splitlines()

    # Verify key metadata fetched matches current key
    current_key_resp = requests.get(f"{GATEWAY_URL}/v1/signing-key/current").json()
    current_key_id = current_key_resp["key_id"]

    for line in lines:
        if "SIGNED" in line:
            assert f"KEY={current_key_id}" in line, f"Descriptor signed with unexpected key: {line}"
        if "PUBLISHED" in line:
            assert "STATUS=PUBLISHED" in line, f"Publication was not accepted: {line}"


def test_receipts_and_tokens_persisted_in_duckdb():
    """functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]: After a run,
    releases.duckdb contains the gateway receipts and request tokens returned by the gateway."""
    assert DB_PATH.exists(), f"Database file {DB_PATH} was not created"
    db_con = duckdb.connect(str(DB_PATH), read_only=True)
    tables = [t[0] for t in db_con.execute("SHOW TABLES").fetchall()]
    assert len(tables) > 0, "No tables found in releases.duckdb"

    all_data = []
    for t in tables:
        rows = db_con.execute(f"SELECT * FROM {t}").fetchall()
        all_data.extend([str(col) for row in rows for col in row])
    db_con.close()

    combined_data = " ".join(all_data)
    for bnd in ["BND-101", "BND-102", "BND-103"]:
        assert f"token-{bnd}" in combined_data, f"request_token token-{bnd} not persisted in DuckDB"


def test_idempotent_rerun_no_duplicate_publications():
    """functional_criteria[id=idempotent_rerun_no_duplicate_publications]: Re-running
    the publisher reuses the persisted request tokens so bundles are not double-submitted."""
    run1 = run_publisher()
    assert run1.returncode == 0

    run2 = run_publisher()
    assert run2.returncode == 0

    assert run1.stdout == run2.stdout, (
        "Re-running the publisher produced differing output:\n"
        f"Run 1:\n{run1.stdout}\nRun 2:\n{run2.stdout}"
    )


def test_revoked_key_signature_rejected():
    """functional_criteria[id=revoked_key_signature_rejected]: A descriptor signed
    with the revoked key is rejected by the gateway as UNTRUSTED_SIGNATURE."""
    revoked_key_path = APP_ROOT / "environment" / "keys" / "revoked" / "revoked.key.pem" if (APP_ROOT / "environment" / "keys").exists() else APP_ROOT / "keys" / "revoked" / "revoked.key.pem"
    revoked_cert_path = APP_ROOT / "environment" / "keys" / "revoked" / "revoked.cert.pem" if (APP_ROOT / "environment" / "keys").exists() else APP_ROOT / "keys" / "revoked" / "revoked.cert.pem"

    if not revoked_key_path.exists():
        # If running in bare environment without keys pre-generated, generate temporary revoked cert/key
        temp_dir = tempfile.mkdtemp()
        revoked_key_path = Path(temp_dir) / "revoked.key.pem"
        revoked_cert_path = Path(temp_dir) / "revoked.cert.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1",
                "-keyout", str(revoked_key_path),
                "-out", str(revoked_cert_path),
                "-subj", "/CN=fw-signing-2025-revoked/O=ReleaseEng/C=US",
            ],
            check=True,
            capture_output=True,
        )

    descriptor_str = '{"artifact_count":1,"bundle_id":"BND-TEST-REVOKED","total_bytes":100}'

    # Produce CMS signature with revoked key
    with tempfile.NamedTemporaryFile("w", delete=False) as df:
        df.write(descriptor_str)
        df_path = df.name

    try:
        sig_proc = subprocess.run(
            [
                "openssl", "cms", "-sign",
                "-in", df_path,
                "-signer", str(revoked_cert_path),
                "-inkey", str(revoked_key_path),
                "-outform", "PEM",
                "-binary",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        signature_pem = sig_proc.stdout

        # Submit to gateway
        resp = requests.post(
            f"{GATEWAY_URL}/v1/publications",
            json={
                "descriptor": descriptor_str,
                "signature": signature_pem,
                "request_token": "token-revoked-test",
            },
        )
        assert resp.status_code == 400, f"Expected 400 from revoked key, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("error") == "UNTRUSTED_SIGNATURE", f"Expected error UNTRUSTED_SIGNATURE, got {data}"
    finally:
        if os.path.exists(df_path):
            os.remove(df_path)
