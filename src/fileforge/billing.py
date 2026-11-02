"""Credit billing layer for FileForge (checklist item 3).

Deliberately kept OUT of ``tools.py``: billing lives in the transport
layer. The server wraps every tool call with :func:`charge` before the
call and :func:`refund` if the call fails, so a failed job never costs
the user anything (the landing page promises exactly that).

Model (pricing as decided 2026-11-02):
- 1 credit = 1 tool call. Credits never expire. One-time packs, no
  subscription: Starter $6 = 100 credits, Pro $15 = 300 credits.
- Free tier: 5 calls per calendar month (UTC), no key required.
- Paid credits are spent first; the free monthly quota is used only
  when the paid balance is zero. (Spending paid first is the generous
  reading for buyers whose free quota would otherwise rot - and it
  keeps the free quota as a trial for non-buyers.)

Identity:
- API key: ``Authorization: Bearer ff_...`` - only its SHA-256 hash is
  stored. Keys are issued by the admin CLI (``fileforge.admin``) and,
  after a Creem purchase, claimed by the buyer (see webhook below).
- Anonymous callers (no key) get a bucket account derived from a hash
  of the client IP; they can only ever use the free quota.

Payments: Creem is the merchant of record. Its ``checkout.completed``
webhook is the only signal that money moved:
- Signature = hex HMAC-SHA256 of the raw body with the webhook secret
  (header ``creem-signature``), compared in constant time.
- Credits are granted to the account matching the checkout customer
  email (created as a shadow account if the buyer has no key yet), or
  to ``metadata.fileforge_account_id`` when present.
- Idempotent twice over: by webhook event id and by Creem order id,
  because Creem retries deliveries.
- Product -> credits comes from env CREEM_PRODUCT_STARTER_ID /
  CREEM_PRODUCT_PRO_ID; unknown products are reported, never guessed.
- ``refund.created`` claws the grant back (best effort, never below 0).

Storage: stdlib sqlite3, one file (FILEFORGE_DB_PATH). Every balance
mutation happens in a BEGIN IMMEDIATE transaction and is mirrored in
the append-only ledger table.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FREE_PER_MONTH = int(os.environ.get("FILEFORGE_FREE_PER_MONTH", "5"))
STARTER_CREDITS = 100
PRO_CREDITS = 300

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "fileforge.db"
DB_PATH = Path(os.environ.get("FILEFORGE_DB_PATH", _DEFAULT_DB))

_LOCK = threading.Lock()


class BillingError(Exception):
    """User-readable billing failure (turned into an MCP tool error)."""


class InsufficientCredits(BillingError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_key(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            kind TEXT NOT NULL DEFAULT 'key',
            paid_credits INTEGER NOT NULL DEFAULT 0,
            free_month TEXT NOT NULL DEFAULT '',
            free_used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            key_hash TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            disabled INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            delta INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'paid',
            reason TEXT NOT NULL,
            tool TEXT,
            ref TEXT,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            account_id TEXT,
            credits INTEGER NOT NULL DEFAULT 0,
            order_id TEXT,
            processed_at TEXT NOT NULL
        )
        """
    )
    return conn


def _hash_key(plain_key: str) -> str:
    return hashlib.sha256(plain_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Accounts and keys
# ---------------------------------------------------------------------------

def create_account(email: str | None = None, kind: str = "key") -> str:
    account_id = uuid.uuid4().hex
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO accounts (id, email, kind, free_month, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, email, kind, _month_key(), _now().isoformat()),
        )
    return account_id


def find_or_create_by_email(email: str) -> str:
    email = email.strip().lower()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
        if row:
            return str(row["id"])
        conn.execute("BEGIN IMMEDIATE")
        account_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO accounts (id, email, kind, free_month, created_at) "
            "VALUES (?, ?, 'shadow', ?, ?)",
            (account_id, email, _month_key(), _now().isoformat()),
        )
        return account_id


def anonymous_account_id(client_ip: str | None) -> str:
    """Stable bucket id for key-less callers (free quota only)."""
    digest = hashlib.sha256((client_ip or "unknown").encode()).hexdigest()[:24]
    account_id = f"anon-{digest}"
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO accounts (id, email, kind, free_month, created_at) "
                "VALUES (?, NULL, 'anon', ?, ?)",
                (account_id, _month_key(), _now().isoformat()),
            )
    return account_id


def issue_api_key(account_id: str, label: str = "") -> str:
    """Create an API key for an account. The plain key is returned once;
    only its hash is stored."""
    plain = "ff_" + secrets.token_urlsafe(32)
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO api_keys (key_hash, account_id, label, created_at) VALUES (?, ?, ?, ?)",
            (_hash_key(plain), account_id, label, _now().isoformat()),
        )
    return plain


def account_for_api_key(plain_key: str) -> str | None:
    if not plain_key.startswith("ff_"):
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM api_keys WHERE key_hash = ? AND disabled = 0",
            (_hash_key(plain_key),),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
            (_now().isoformat(), _hash_key(plain_key)),
        )
        conn.commit()
        return str(row["account_id"])


def _roll_free_month(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise BillingError("Unknown account.")
    if row["free_month"] != _month_key():
        conn.execute(
            "UPDATE accounts SET free_month = ?, free_used = 0 WHERE id = ?",
            (_month_key(), account_id),
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return row


def balance(account_id: str) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _roll_free_month(conn, account_id)
        conn.commit()
    return {
        "account_id": account_id,
        "paid_credits": int(row["paid_credits"]),
        "free_used_this_month": int(row["free_used"]),
        "free_remaining_this_month": max(0, FREE_PER_MONTH - int(row["free_used"])),
        "free_per_month": FREE_PER_MONTH,
    }


# ---------------------------------------------------------------------------
# Charge / refund / grant
# ---------------------------------------------------------------------------

def charge(account_id: str, tool: str) -> str:
    """Take 1 credit for a tool call. Returns a charge ref for refund().

    Paid credits are spent first, then the free monthly quota.
    Raises InsufficientCredits with a purchase hint when neither is left.
    """
    ref = uuid.uuid4().hex
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _roll_free_month(conn, account_id)
        if int(row["paid_credits"]) >= 1:
            conn.execute(
                "UPDATE accounts SET paid_credits = paid_credits - 1 WHERE id = ?", (account_id,)
            )
            source = "paid"
        elif int(row["free_used"]) < FREE_PER_MONTH:
            conn.execute(
                "UPDATE accounts SET free_used = free_used + 1 WHERE id = ?", (account_id,)
            )
            source = "free"
        else:
            raise InsufficientCredits(
                "Out of credits: the free plan includes "
                f"{FREE_PER_MONTH} calls per month. Buy a credit pack "
                "(Starter $6 = 100 credits, Pro $15 = 300 credits, credits "
                "never expire) at https://hongfeng328.github.io/niux-guides/fileforge/ "
                "and add your API key as a Bearer token."
            )
        conn.execute(
            "INSERT INTO ledger (account_id, delta, source, reason, tool, ref, created_at) "
            "VALUES (?, -1, ?, 'tool_charge', ?, ?, ?)",
            (account_id, source, tool, ref, _now().isoformat()),
        )
        conn.commit()
    return ref


def refund(account_id: str, ref: str, tool: str = "") -> bool:
    """Undo a charge (tool call failed). Idempotent per charge ref."""
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT source FROM ledger WHERE ref = ? AND reason = 'tool_charge'", (ref,)
        ).fetchone()
        if row is None:
            return False
        try:
            conn.execute(
                "INSERT INTO ledger (account_id, delta, source, reason, tool, ref, "
                "idempotency_key, created_at) VALUES (?, 1, ?, 'tool_refund', ?, ?, ?, ?)",
                (account_id, row["source"], tool, ref, f"refund:{ref}", _now().isoformat()),
            )
        except sqlite3.IntegrityError:
            return False  # already refunded
        if row["source"] == "paid":
            conn.execute(
                "UPDATE accounts SET paid_credits = paid_credits + 1 WHERE id = ?", (account_id,)
            )
        else:
            conn.execute(
                "UPDATE accounts SET free_used = MAX(0, free_used - 1) WHERE id = ?", (account_id,)
            )
        conn.commit()
    return True


def grant_credits(
    account_id: str, credits: int, reason: str, idempotency_key: str | None = None
) -> bool:
    """Add paid credits. Returns False if the idempotency key already ran."""
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO ledger (account_id, delta, source, reason, idempotency_key, "
                "created_at) VALUES (?, ?, 'paid', ?, ?, ?)",
                (account_id, credits, reason, idempotency_key, _now().isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute(
            "UPDATE accounts SET paid_credits = paid_credits + ? WHERE id = ?",
            (credits, account_id),
        )
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Creem webhook
# ---------------------------------------------------------------------------

def verify_creem_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Creem signs webhooks as hex HMAC-SHA256(raw body, webhook secret)."""
    if not signature_header or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def _product_credits(product_id: str | None, product_name: str | None) -> int | None:
    if product_id and product_id == os.environ.get("CREEM_PRODUCT_STARTER_ID"):
        return STARTER_CREDITS
    if product_id and product_id == os.environ.get("CREEM_PRODUCT_PRO_ID"):
        return PRO_CREDITS
    return None


def process_creem_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one (already signature-verified) Creem webhook event.

    Returns a status dict; never raises for expected business cases so
    the HTTP layer can answer 200 and stop Creem's retries, except for
    unknown products which are recorded for manual handling.
    """
    event_id = str(payload.get("id") or "")
    event_type = str(payload.get("eventType") or payload.get("event_type") or "")
    obj = payload.get("object") or {}
    if not event_id:
        return {"status": "ignored", "reason": "missing event id"}

    with _LOCK, _connect() as conn:
        seen = conn.execute(
            "SELECT event_id FROM webhook_events WHERE event_id = ?", (event_id,)
        ).fetchone()
    if seen:
        return {"status": "duplicate", "event_id": event_id}

    if event_type == "checkout.completed":
        product = obj.get("product") or {}
        product_id = product.get("id") if isinstance(product, dict) else str(product)
        order = obj.get("order") or {}
        order_id = str(order.get("id") or obj.get("id") or "")
        credits = _product_credits(product_id, product.get("name") if isinstance(product, dict) else None)
        metadata = obj.get("metadata") or {}
        customer = obj.get("customer") or {}
        email = customer.get("email") if isinstance(customer, dict) else None

        if credits is None:
            _record_event(event_id, event_type, None, 0, order_id)
            return {
                "status": "unknown_product",
                "event_id": event_id,
                "product_id": product_id,
                "note": "Set CREEM_PRODUCT_STARTER_ID / CREEM_PRODUCT_PRO_ID; "
                "grant manually with the admin CLI.",
            }
        account_id = metadata.get("fileforge_account_id") or (
            find_or_create_by_email(email) if email else None
        )
        if not account_id:
            _record_event(event_id, event_type, None, 0, order_id)
            return {"status": "no_account", "event_id": event_id}
        applied = grant_credits(
            account_id, credits, "creem_purchase", idempotency_key=f"creem-order:{order_id}"
        )
        _record_event(event_id, event_type, account_id, credits if applied else 0, order_id)
        return {
            "status": "credited" if applied else "duplicate_order",
            "event_id": event_id,
            "account_id": account_id,
            "credits": credits if applied else 0,
        }

    if event_type == "refund.created":
        order = obj.get("order") or {}
        order_id = str(
            (order.get("id") if isinstance(order, dict) else None)
            or obj.get("order_id")
            or obj.get("id")
            or ""
        )
        # Find the original grant by its order idempotency key and claw back.
        with _LOCK, _connect() as conn:
            row = conn.execute(
                "SELECT account_id, delta FROM ledger WHERE idempotency_key = ?",
                (f"creem-order:{order_id}",),
            ).fetchone()
        clawed = 0
        account_id = None
        if row:
            account_id = str(row["account_id"])
            granted = int(row["delta"])
            with _LOCK, _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                bal = conn.execute(
                    "SELECT paid_credits FROM accounts WHERE id = ?", (account_id,)
                ).fetchone()
                clawed = min(granted, int(bal["paid_credits"])) if bal else 0
                try:
                    conn.execute(
                        "INSERT INTO ledger (account_id, delta, source, reason, "
                        "idempotency_key, created_at) VALUES (?, ?, 'paid', "
                        "'creem_refund_clawback', ?, ?)",
                        (account_id, -clawed, f"creem-refund:{order_id}", _now().isoformat()),
                    )
                    conn.execute(
                        "UPDATE accounts SET paid_credits = paid_credits - ? WHERE id = ?",
                        (clawed, account_id),
                    )
                except sqlite3.IntegrityError:
                    clawed = 0
                conn.commit()
        _record_event(event_id, event_type, account_id, -clawed, order_id)
        return {"status": "refund_processed", "event_id": event_id, "clawed_back": clawed}

    _record_event(event_id, event_type, None, 0, None)
    return {"status": "ignored", "event_id": event_id, "event_type": event_type}


def _record_event(
    event_id: str, event_type: str, account_id: str | None, credits: int, order_id: str | None
) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO webhook_events (event_id, event_type, account_id, credits, "
                "order_id, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, event_type, account_id, credits, order_id, _now().isoformat()),
            )
        except sqlite3.IntegrityError:
            pass
        conn.commit()


def dumps_balance_for_key(plain_key: str) -> dict[str, Any] | None:
    account_id = account_for_api_key(plain_key)
    if not account_id:
        return None
    return balance(account_id)


# Re-exported for tests/tools that build payloads locally.
__all__ = [
    "BillingError",
    "InsufficientCredits",
    "FREE_PER_MONTH",
    "create_account",
    "find_or_create_by_email",
    "anonymous_account_id",
    "issue_api_key",
    "account_for_api_key",
    "balance",
    "charge",
    "refund",
    "grant_credits",
    "verify_creem_signature",
    "process_creem_event",
    "dumps_balance_for_key",
    "json",
]
