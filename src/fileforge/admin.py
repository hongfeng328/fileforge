"""FileForge admin CLI: issue API keys, check balances, grant credits.

Usage (from the project root, on the server):

    PYTHONPATH=src .venv/bin/python -m fileforge.admin create-key --email buyer@example.com
    PYTHONPATH=src .venv/bin/python -m fileforge.admin balance --email buyer@example.com
    PYTHONPATH=src .venv/bin/python -m fileforge.admin grant --email buyer@example.com --credits 100 --reason manual-fix
    PYTHONPATH=src .venv/bin/python -m fileforge.admin list-accounts

The plain API key is printed exactly once by create-key; only its hash
is stored, so a lost key means issuing a new one.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from fileforge import billing


def _account_by_email(email: str) -> str | None:
    with billing._connect() as conn:  # local admin tool: reuse billing's connection
        row = conn.execute(
            "SELECT id FROM accounts WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
    return str(row["id"]) if row else None


def _resolve(args: argparse.Namespace) -> str:
    if getattr(args, "account_id", None):
        return args.account_id
    if getattr(args, "email", None):
        account_id = _account_by_email(args.email)
        if account_id:
            return account_id
        if args.cmd == "create-key":
            return billing.create_account(email=args.email.strip().lower())
        raise SystemExit(f"No account found for email {args.email}")
    raise SystemExit("Pass --email or --account-id.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fileforge.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-key", help="Create/find an account and issue an API key")
    p.add_argument("--email", required=True)
    p.add_argument("--label", default="")

    p = sub.add_parser("balance", help="Show an account balance")
    p.add_argument("--email")
    p.add_argument("--account-id")

    p = sub.add_parser("grant", help="Manually grant paid credits")
    p.add_argument("--email")
    p.add_argument("--account-id")
    p.add_argument("--credits", type=int, required=True)
    p.add_argument("--reason", default="manual_grant")

    sub.add_parser("list-accounts", help="List recent accounts")

    args = parser.parse_args(argv)

    if args.cmd == "create-key":
        account_id = _resolve(args)
        key = billing.issue_api_key(account_id, label=args.label)
        print(f"account_id: {account_id}")
        print(f"api_key:    {key}")
        print("Store this key now - it is shown only once.")
        print(f"balance:    {billing.balance(account_id)}")
        return 0

    if args.cmd == "balance":
        account_id = _resolve(args)
        print(billing.balance(account_id))
        return 0

    if args.cmd == "grant":
        account_id = _resolve(args)
        applied = billing.grant_credits(account_id, args.credits, args.reason)
        print(f"granted: {applied}; balance: {billing.balance(account_id)}")
        return 0

    if args.cmd == "list-accounts":
        with billing._connect() as conn:
            rows = conn.execute(
                "SELECT id, email, kind, paid_credits, free_used, created_at "
                "FROM accounts ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        for row in rows:
            print(dict(row))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
