"""Local JSON history is retired. Transactional chat requests own writes."""
def get_history(session_id, *, principal, store):
    principal.require("reader")
    return store.history(principal, session_id)

class FileChatMessageHistory:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("File history is disabled; use the authenticated PostgreSQL API.")
