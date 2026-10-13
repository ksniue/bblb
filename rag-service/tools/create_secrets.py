"""Create local compose secrets interactively. Never print a token or provider key."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat


def check_directory(directory):
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return
    # Reject POSIX symlinks and Windows reparse points (including junctions).
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise SystemExit("The secrets directory must not be a symbolic link or junction")
    if not stat.S_ISDIR(info.st_mode):
        raise SystemExit("The secrets path must be a directory")
    if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise SystemExit("The secrets directory must be owned by this user and private (mode 0700)")


def write_secret(directory, filename, value, directory_fd=None):
    # O_EXCL rejects existing regular files and existing/dangling symlinks.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    target = filename if directory_fd is not None else directory / filename
    descriptor = os.open(target, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(value + "\n")
        stream.flush()
        # Only mounted files are readable by container UID 10001. The enclosing
        # host directory is private; the raw access token is never mounted.
        if os.name == "posix":
            os.fchmod(stream.fileno(), 0o600 if filename == "access_token.txt" else 0o444)


def main():
    directory = Path(__file__).resolve().parents[1] / "secrets"
    check_directory(directory)
    if directory.exists() and any(directory.iterdir()):
        raise SystemExit("secrets/ is not empty; refusing to overwrite existing credentials")
    key = getpass.getpass("Enter your model provider API key (hidden): ").strip()
    if not key:
        raise SystemExit("A real provider API key is required; nothing was written")
    admin_password, app_password, token = secrets.token_hex(32), secrets.token_hex(32), secrets.token_urlsafe(48)
    directory.mkdir(mode=0o700, exist_ok=True)
    check_directory(directory)
    data = {
        "postgres_password.txt": admin_password,
        "database_app_password.txt": app_password,
        "database_admin_url.txt": f"postgresql://rag_admin:{admin_password}@postgres:5432/rag",
        "database_url.txt": f"postgresql://rag_app:{app_password}@postgres:5432/rag",
        "model_api_key.txt": key,
        "access_token.txt": token,
        "principals.json": json.dumps([{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "tenant_id": "company", "user_id": "operator", "roles": ["reader", "editor"],
            "knowledge_base_ids": ["default"]}], indent=2),
    }
    directory_fd = None
    if os.name == "posix":
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            os.close(directory_fd)
            raise SystemExit("The secrets directory must be owned by this user and private (mode 0700)")
    try:
        for filename, value in data.items():
            check_directory(directory)
            write_secret(directory, filename, value, directory_fd)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    print("Created local secret files under secrets/. No credentials were printed.")
    print("Distribute access_token.txt securely; it is not mounted in the application container.")


if __name__ == "__main__":
    main()
