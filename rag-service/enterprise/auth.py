from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from pathlib import Path


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    roles: frozenset
    kb_ids: frozenset

    def require(self, role, kb_id=None):
        if (role not in self.roles and "admin" not in self.roles) or (kb_id is not None and kb_id not in self.kb_ids):
            raise AccessDenied("Access denied")


class Authenticator:
    """Load a read-only secret at startup; rotate/revoke by replacing it and restarting."""

    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            raise RuntimeError("Authentication map must be a non-empty list")
        self.entries = []
        seen = set()
        for row in data:
            digest = row.get("token_sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest in seen or len(set(digest)) == 1:
                raise RuntimeError("Invalid or duplicate authentication token hash")
            seen.add(digest)
            for field in ("tenant_id", "user_id"):
                if not isinstance(row.get(field), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", row[field]):
                    raise RuntimeError("Invalid principal identifier")
            roles, kbs = row.get("roles"), row.get("knowledge_base_ids")
            if not isinstance(roles, list) or not roles or not set(roles) <= {"reader", "editor", "admin"}:
                raise RuntimeError("Invalid role list")
            if not isinstance(kbs, list) or not kbs or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", x) for x in kbs):
                raise RuntimeError("Invalid knowledge base list")
            self.entries.append((digest, Principal(row["tenant_id"], row["user_id"], frozenset(roles), frozenset(kbs))))

    def authenticate(self, authorization):
        if not authorization or authorization[:7].lower() != "bearer ":
            raise AccessDenied("Authentication required")
        token = authorization[7:]
        if not 32 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise AccessDenied("Invalid credentials")
        digest = hashlib.sha256(token.encode()).hexdigest()
        match = None
        for expected, principal in self.entries:
            if hmac.compare_digest(digest, expected):
                match = principal
        if match is None:
            raise AccessDenied("Invalid credentials")
        return match
