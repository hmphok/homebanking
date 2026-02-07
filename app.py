#!/usr/bin/env python3
import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

BASE = "https://bankaccountdata.gocardless.com/api/v2"

# Mount points inside container
GCBAD_SECRETS_DIR = Path(os.environ.get("GCBAD_SECRETS_DIR", "/secrets/gcbad"))
GSHEETS_SECRETS_DIR = Path(os.environ.get("GSHEETS_SECRETS_DIR", "/secrets/gsheets"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TOKEN_CACHE_PATH = DATA_DIR / "gcbad_token_cache.json"


def find_single_json(dir_path: Path) -> Path:
    if not dir_path.exists():
        raise RuntimeError(f"Secrets dir does not exist: {dir_path}")
    files = sorted([p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() == ".json"])
    if len(files) != 1:
        raise RuntimeError(f"Expected exactly 1 .json in {dir_path}, found {len(files)}: {[p.name for p in files]}")
    return files[0]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_gcbad_user_secrets() -> tuple[str, str]:
    secrets_file = find_single_json(GCBAD_SECRETS_DIR)
    data = load_json(secrets_file)

    # tolerate some variants
    secret_id = data.get("secret_id") or data.get("secretId") or data.get("SECRET_ID") or data.get("id")
    secret_key = data.get("secret_key") or data.get("secretKey") or data.get("SECRET_KEY") or data.get("key")

    if not secret_id or not secret_key:
        raise RuntimeError(f"Could not find secret_id/secret_key in {secrets_file}")
    return secret_id, secret_key


def token_new(secret_id: str, secret_key: str) -> tuple[str, int]:
    r = requests.post(
        f"{BASE}/token/new/",
        json={"secret_id": secret_id, "secret_key": secret_key},
        timeout=30,
    )
    r.raise_for_status()
    out = r.json()
    refresh = out["refresh"]
    refresh_expires = int(out.get("refresh_expires", 0))  # seconds
    refresh_expires_at = int(time.time()) + refresh_expires if refresh_expires else 0
    return refresh, refresh_expires_at


def token_refresh(refresh: str) -> str:
    r = requests.post(f"{BASE}/token/refresh/", json={"refresh": refresh}, timeout=30)
    r.raise_for_status()
    return r.json()["access"]


def get_refresh_token_cached() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if TOKEN_CACHE_PATH.exists():
        cache = load_json(TOKEN_CACHE_PATH)
        refresh = cache.get("refresh")
        exp_at = int(cache.get("refresh_expires_at", 0) or 0)
        if refresh and (exp_at == 0 or time.time() < exp_at - 60):
            return refresh

    secret_id, secret_key = load_gcbad_user_secrets()
    refresh, refresh_expires_at = token_new(secret_id, secret_key)
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"refresh": refresh, "refresh_expires_at": refresh_expires_at}, indent=2),
        encoding="utf-8",
    )
    return refresh


def get_access_token() -> str:
    refresh = get_refresh_token_cached()
    return token_refresh(refresh)


def gc_get(path: str) -> dict:
    access = get_access_token()
    r = requests.get(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {access}", "accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def gc_post(path: str, payload: dict) -> dict:
    access = get_access_token()
    r = requests.post(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {access}", "accept": "application/json"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def choose_balance(balances_json: dict) -> tuple[Decimal, str, str, str]:
    balances = balances_json.get("balances", [])
    if not balances:
        raise RuntimeError("No balances returned")

    preferred = (os.environ.get("BALANCE_TYPE_PREFERENCE") or
                 "closingBooked,closingAvailable,interimBooked,interimAvailable,expected").split(",")
    preferred = [p.strip() for p in preferred if p.strip()]

    by_type = {b.get("balanceType"): b for b in balances if b.get("balanceType")}
    chosen = None
    for t in preferred:
        if t in by_type:
            chosen = by_type[t]
            break
    if chosen is None:
        chosen = balances[0]

    amt = Decimal(chosen["balanceAmount"]["amount"])
    cur = chosen["balanceAmount"]["currency"]
    btype = chosen.get("balanceType", "")
    ref = chosen.get("referenceDate") or chosen.get("lastChangeDateTime") or ""
    return amt, cur, btype, ref


def sheets_write(values_2d: list[list[str]], spreadsheet_id: str, a1_range: str) -> None:
    sa_file = find_single_json(GSHEETS_SECRETS_DIR)
    creds = Credentials.from_service_account_file(
        str(sa_file),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=a1_range,
        valueInputOption="USER_ENTERED",
        body={"values": values_2d},
    ).execute()


# ---- commands ----

def cmd_institutions(args):
    data = gc_get(f"/institutions/?country={args.country.lower()}")
    q = (args.search or "").lower().strip()
    for inst in data:
        name = inst.get("name", "")
        iid = inst.get("id", "")
        if not q or q in name.lower() or q in iid.lower():
            print(f"{iid}\t{name}")


def cmd_create_requisition(args):
    payload = {
        "redirect": args.redirect,
        "institution_id": args.institution_id,
        "reference": args.reference,
        "user_language": args.user_language,
    }
    out = gc_post("/requisitions/", payload)
    print(json.dumps(out, indent=2))


def cmd_requisition(args):
    out = gc_get(f"/requisitions/{args.requisition_id}/")
    print(json.dumps(out, indent=2))


def cmd_balance(args):
    out = gc_get(f"/accounts/{args.account_id}/balances/")
    amt, cur, btype, ref = choose_balance(out)
    print(f"{amt} {cur}\tbalanceType={btype}\tref={ref}")


def cmd_run(args):
    account_id = os.environ.get("GC_ACCOUNT_ID")
    spreadsheet_id = os.environ.get("GSHEET_ID")
    a1_range = os.environ.get("GSHEET_RANGE")

    if not account_id:
        raise RuntimeError("Missing env GC_ACCOUNT_ID")
    if not spreadsheet_id:
        raise RuntimeError("Missing env GSHEET_ID")
    if not a1_range:
        raise RuntimeError("Missing env GSHEET_RANGE")

    out = gc_get(f"/accounts/{account_id}/balances/")
    amt, cur, btype, ref = choose_balance(out)

    # Write: amount | currency | balanceType | referenceDate | unix_ts
    values = [[str(amt), cur, btype, ref, str(int(time.time()))]]
    sheets_write(values, spreadsheet_id, a1_range)
    print(f"Wrote {amt} {cur} ({btype}) to sheet range {a1_range}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("institutions")
    sp.add_argument("--country", default="PT")
    sp.add_argument("--search", default=None)
    sp.set_defaults(func=cmd_institutions)

    sp = sub.add_parser("create-requisition")
    sp.add_argument("--institution-id", required=True)
    sp.add_argument("--redirect", required=True)
    sp.add_argument("--reference", default="orange-pi-balance")
    sp.add_argument("--user-language", default="EN")
    sp.set_defaults(func=cmd_create_requisition)

    sp = sub.add_parser("requisition")
    sp.add_argument("--requisition-id", required=True)
    sp.set_defaults(func=cmd_requisition)

    sp = sub.add_parser("balance")
    sp.add_argument("--account-id", required=True)
    sp.set_defaults(func=cmd_balance)

    sp = sub.add_parser("run")
    sp.set_defaults(func=cmd_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
