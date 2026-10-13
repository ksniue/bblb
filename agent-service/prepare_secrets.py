"""Interactively prepare bootstrap secrets; never overwrite existing credentials."""
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--kb", required=True)
    parser.add_argument("--output", default="secrets")
    parser.add_argument("--reports", action="store_true", help="Also prompt for the internal usage API token")
    args = parser.parse_args()
    for value in [args.tenant, args.user, args.kb]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            parser.error("Identity IDs must contain 1–80 letters, digits, _, -, or .")
    output = Path(args.output).resolve()
    if os.name == "posix" and output.exists() and output.stat().st_mode & 0o077:
        parser.error("The secret directory must not be accessible to other users (use mode 0700)")
    names = ["principals.json", "client_token", "postgres_password", "migration_database_url", "app_database_url", "app_database_password", "model_api_key", "usage_api_token"]
    if any((output / name).exists() for name in names):
        parser.error("A target credential file already exists; refusing to overwrite")
    model_key = getpass.getpass("Model API key (hidden): ").strip()
    if not model_key:
        parser.error("A real model API key is required; no demo key is generated")
    usage_key = getpass.getpass("Usage API internal token (hidden): ").strip() if args.reports else ""
    if args.reports and not usage_key:
        parser.error("Usage API token cannot be empty when reports are enabled")
    token = secrets.token_urlsafe(48)
    password = secrets.token_urlsafe(48)
    app_password = secrets.token_urlsafe(48)
    principal = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "tenant_id": args.tenant,
                 "user_id": args.user, "roles": ["reader", "editor", "admin"], "knowledge_base_ids": [args.kb]}
    files = {
        "principals.json": json.dumps([principal], indent=2) + "\n", "client_token": token + "\n",
        "postgres_password": password + "\n",
        "migration_database_url": f"postgresql://rag:{quote(password, safe='')}@postgres:5432/rag\n",
        "app_database_url": f"postgresql://rag_app:{quote(app_password, safe='')}@postgres:5432/rag\n",
        "app_database_password": app_password + "\n",
        "model_api_key": model_key + "\n", "usage_api_token": usage_key + "\n",
    }
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name, value in files.items():
        # Host directory 0700 protects access; mounted individual files must be
        # readable by non-root containers. The raw bootstrap token is never mounted.
        mode = 0o600 if name == "client_token" else 0o644
        descriptor = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        if os.name == "posix":
            os.chmod(output / name, mode)
    print(f"Created {len(files)} secret files in {output}. No credential values were printed.")
    print("The client_token is a bootstrap administrator credential. Store it securely; do not distribute it to end users.")
    print("Configure host ACLs so only the operator and container secret mount can read these files.")


if __name__ == "__main__":
    main()
