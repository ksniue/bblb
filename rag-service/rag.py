"""Compatibility import. A service requires authenticated injected dependencies."""
from enterprise.service import RagService

__all__ = ["RagService"]

if __name__ == "__main__":
    raise SystemExit("Start: uvicorn enterprise.api:app --host 127.0.0.1 --port 8000")
