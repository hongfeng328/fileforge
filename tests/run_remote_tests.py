"""Tests for the remote layer: storage, downloads, billing, Creem webhook.

Covers checklist items 1-3 without any network: everything runs against
a throwaway sqlite DB and temp output/incoming dirs.

Run from the project root:
    PYTHONPATH=src .venv/bin/python tests/run_remote_tests.py
Exits non-zero if any check fails.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="fileforge-remote-tests-"))

# Environment must be set BEFORE importing fileforge modules: the DB
# path, output/incoming dirs and download secret are read at import.
os.environ["FILEFORGE_DB_PATH"] = str(_TMP / "test.db")
os.environ["FILEFORGE_OUTPUT_DIR"] = str(_TMP / "output")
os.environ["FILEFORGE_INCOMING_DIR"] = str(_TMP / "incoming")
os.environ["FILEFORGE_DOWNLOAD_SECRET"] = "test-secret"
os.environ["CREEM_PRODUCT_STARTER_ID"] = "prod_starter_test"
os.environ["CREEM_PRODUCT_PRO_ID"] = "prod_pro_test"
os.environ["CREEM_WEBHOOK_SECRET"] = "whsec_test"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from fileforge import billing, downloads, janitor, storage, tools  # noqa: E402
from run_selftest import make_image, make_table_pdf, make_text_pdf  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    RESULTS.append(condition)
    return condition


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def expect_value_error(name: str, fn) -> None:
    try:
        fn()
        check(name, False, "no error raised")
    except ValueError as exc:
        check(name, True, str(exc)[:80])


def main() -> int:
    samples = _TMP / "samples"
    samples.mkdir(parents=True)
    table_pdf = samples / "table.pdf"
    make_table_pdf(table_pdf)
    pdf_a, pdf_b = samples / "a.pdf", samples / "b.pdf"
    make_text_pdf(pdf_a, "PAGE-A")
    make_text_pdf(pdf_b, "PAGE-B")
    img = samples / "one.png"
    make_image(img, "steelblue", "IMG")

    # --- storage: happy path + validation --------------------------------
    p = storage.materialize(b64(table_pdf), "pdf")
    check("storage: pdf materialises", p.is_file() and p.suffix == ".pdf")
    storage.discard([p])
    check("storage: discard removes file", not p.exists())

    expect_value_error(
        "storage: wrong kind rejected",
        lambda: storage.materialize(b64(table_pdf), "image"),
    )
    expect_value_error(
        "storage: garbage rejected",
        lambda: storage.materialize(base64.b64encode(b"not a file").decode(), "pdf"),
    )
    expect_value_error(
        "storage: invalid base64 rejected",
        lambda: storage.materialize("!!!not-base64!!!", "pdf"),
    )
    expect_value_error(
        "storage: empty payload rejected", lambda: storage.materialize("", "pdf")
    )
    paths = storage.materialize_many([b64(img), b64(img)], "image")
    check("storage: many images materialise", len(paths) == 2 and all(x.is_file() for x in paths))
    storage.discard(paths)

    # --- end-to-end through storage -> tools ------------------------------
    p = storage.materialize(b64(table_pdf), "pdf")
    info = tools.pdf_tables_to_excel(str(p))
    storage.discard([p])
    out = Path(info["file_path"])
    check("e2e: table pdf -> xlsx via base64 path", out.is_file() and out.suffix == ".xlsx")

    # --- downloads: signed token round-trip -------------------------------
    token, expiry = downloads.make_token(out.name)
    resolved = downloads.verify_token(token)
    check("downloads: token resolves to the file", resolved == out)
    expect_value_error(
        "downloads: tampered token rejected",
        lambda: downloads.verify_token(token[:-2] + "xx"),
    )
    expect_value_error(
        "downloads: expired token rejected",
        lambda: downloads.verify_token(token, now=expiry + 10),
    )
    expect_value_error(
        "downloads: traversal rejected",
        lambda: downloads.verify_token(
            downloads._b64(b"../secret.txt|9999999999") + "." + token.split(".")[1]
        ),
    )

    # --- janitor: old files removed, fresh files kept ----------------------
    old = tools.OUTPUT_DIR / "old-file.pdf"
    old.write_bytes(b"%PDF- fake")
    old_time = time.time() - 25 * 3600
    os.utime(old, (old_time, old_time))
    result = janitor.cleanup_expired()
    check(
        "janitor: 25h-old output removed, fresh kept",
        not old.exists() and out.exists() and result["outputs_removed"] >= 1,
        str(result),
    )

    # --- billing: keys, free quota, paid-first, refund ---------------------
    account_id = billing.create_account(email="buyer@example.com")
    key = billing.issue_api_key(account_id)
    check("billing: key resolves to account", billing.account_for_api_key(key) == account_id)
    check("billing: bad key rejected", billing.account_for_api_key("ff_nope") is None)
    bal = billing.balance(account_id)
    check("billing: fresh account free quota", bal["free_remaining_this_month"] == 5, str(bal))

    ref = billing.charge(account_id, "merge_pdfs")
    billing.refund(account_id, ref, "merge_pdfs")
    billing.refund(account_id, ref, "merge_pdfs")  # idempotent
    bal = billing.balance(account_id)
    check("billing: failed call refunded exactly once", bal["free_used_this_month"] == 0, str(bal))

    for _ in range(5):
        billing.charge(account_id, "merge_pdfs")
    try:
        billing.charge(account_id, "merge_pdfs")
        check("billing: 6th free call blocked", False)
    except billing.InsufficientCredits:
        check("billing: 6th free call blocked", True)

    billing.grant_credits(account_id, 100, "test_grant")
    ref = billing.charge(account_id, "merge_pdfs")
    bal = billing.balance(account_id)
    check(
        "billing: paid credits spent before free quota would matter",
        bal["paid_credits"] == 99 and bal["free_used_this_month"] == 5,
        str(bal),
    )
    billing.refund(account_id, ref, "merge_pdfs")
    check("billing: paid refund restores credit", billing.balance(account_id)["paid_credits"] == 100)

    anon = billing.anonymous_account_id("203.0.113.7")
    check(
        "billing: anon bucket is stable and free-only",
        anon == billing.anonymous_account_id("203.0.113.7")
        and billing.balance(anon)["paid_credits"] == 0,
    )

    # --- Creem webhook: signature + idempotent credit grant -----------------
    payload = {
        "id": "evt_test_1",
        "eventType": "checkout.completed",
        "object": {
            "id": "ch_test",
            "order": {"id": "ord_test_1", "product": "prod_starter_test"},
            "product": {"id": "prod_starter_test", "name": "Starter"},
            "customer": {"email": "webhook-buyer@example.com"},
            "metadata": {},
        },
    }
    raw = json.dumps(payload).encode()
    secret = os.environ["CREEM_WEBHOOK_SECRET"]
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    check("webhook: signature verifies", billing.verify_creem_signature(raw, sig, secret))
    check("webhook: bad signature rejected", not billing.verify_creem_signature(raw, "0" * 64, secret))

    before = billing.balance(billing.find_or_create_by_email("webhook-buyer@example.com"))
    result1 = billing.process_creem_event(payload)
    mid_id = billing.find_or_create_by_email("webhook-buyer@example.com")
    after = billing.balance(mid_id)
    check(
        "webhook: checkout.completed grants 100 credits",
        result1["status"] == "credited" and after["paid_credits"] == before["paid_credits"] + 100,
        str(result1),
    )
    result2 = billing.process_creem_event(payload)
    check(
        "webhook: duplicate event does not double-credit",
        result2["status"] == "duplicate" and billing.balance(mid_id)["paid_credits"] == after["paid_credits"],
        str(result2),
    )

    unknown = dict(payload, id="evt_test_2")
    unknown["object"] = dict(payload["object"], product={"id": "prod_unknown", "name": "?"})
    result3 = billing.process_creem_event(unknown)
    check("webhook: unknown product reported, not guessed", result3["status"] == "unknown_product", str(result3))

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
