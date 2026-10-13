import hashlib
import json
import re
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    user_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    roles: list[str] = Field(min_length=1, max_length=20)
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        if any(role not in {"reader", "editor", "admin"} for role in value):
            raise ValueError("Unknown role")
        return value

    @field_validator("knowledge_base_ids")
    @classmethod
    def valid_kb_ids(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item) for item in value):
            raise ValueError("Invalid knowledge base ID")
        return value


class Identity(Principal):
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IdentityStore:
    def __init__(self, path: Path):
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or not entries:
            raise ValueError("Identity file must contain a nonempty JSON array")
        self._principals: dict[str, Principal] = {}
        for entry in entries:
            identity = Identity.model_validate(entry)
            if identity.token_sha256 in self._principals:
                raise ValueError("Duplicate token hash in identity file")
            self._principals[identity.token_sha256] = Principal.model_validate(
                identity.model_dump(exclude={"token_sha256"})
            )

    def authenticate(self, authorization: str) -> Principal:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise HTTPException(401, "A valid Bearer credential is required", headers={"WWW-Authenticate": "Bearer"})
        principal = self._principals.get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if principal is None:
            raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
        if not set(principal.roles).intersection({"reader", "admin"}):
            raise HTTPException(403, "Knowledge access role required")
        return principal


def current_principal(request: Request) -> Principal:
    principal = request.app.state.identities.authenticate(request.headers.get("Authorization", ""))
    request.state.principal = principal
    return principal
