"""Manage Travelian's receiving accounts (where customer payments arrive).

    .venv/bin/python -m app.cli.payment_accounts list
    .venv/bin/python -m app.cli.payment_accounts add --method bank_transfer --label "Meezan Bank, Travelian current account"
    .venv/bin/python -m app.cli.payment_accounts deactivate --id <account id>

Methods: bank_transfer, jazzcash, 1link. Use a name staff recognise; do not put full account
numbers in it (it is shown on the Payments tab and in the audit trail).
"""

import argparse
import sys

from app.db import payment_accounts as store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage receiving accounts for customer payments.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list accounts")
    add = commands.add_parser("add", help="add an account")
    add.add_argument("--method", choices=store.METHODS, required=True)
    add.add_argument("--label", required=True)
    off = commands.add_parser("deactivate", help="stop offering an account (past payments keep it)")
    off.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            account = store.add(args.method, args.label)
            print(f"Added {account.id}  {account.method}  {account.label}")
        elif args.command == "deactivate":
            store.deactivate(args.id)
            print(f"Deactivated {args.id}")
        else:
            for account in store.list_accounts(include_inactive=True):
                print(f"{account.id}  {account.method:<13}  {'active  ' if account.active else 'inactive'}  {account.label}")
    except store.AccountError as error:
        print(error.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
