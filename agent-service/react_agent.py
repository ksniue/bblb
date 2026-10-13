"""The unauthenticated demo Agent has been retired. Use service.main:app."""

class ReactAgent:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Legacy ReactAgent is disabled. Start uvicorn service.main:app and call the authenticated /v1/chat or /v1/reports API.")
