import re
import time
from pathlib import PurePath

from .errors import InvalidInput, ProviderUnavailable
from .chains import build_rag_chain, invoke_rag_chain
from .ingestion import split_documents
from .llm_adapters import ProviderEmbeddings


def decode_document(filename, content, settings):
    if not content or len(content) > settings.max_upload_bytes:
        raise InvalidInput("File is empty or exceeds upload limit")
    safe_name = filename.replace("\\", "/").split("/")[-1]
    if not safe_name or len(safe_name) > 180 or any(ord(c) < 32 for c in safe_name):
        raise InvalidInput("Invalid filename")
    suffix = PurePath(safe_name).suffix.lower()
    if suffix == ".txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InvalidInput("TXT files must use UTF-8 encoding") from None
    elif suffix == ".pdf":
        raise InvalidInput("PDF ingestion requires an isolated parser and is disabled; export to UTF-8 TXT")
    else:
        raise InvalidInput("Only UTF-8 TXT is supported")
    text = text.replace("\x00", "").strip()
    if not text or len(text) > settings.max_document_chars:
        raise InvalidInput("Document has no usable text or exceeds text limit")
    return safe_name, text


def split_document(text, settings):
    return [document.page_content for document in split_documents(text, settings)]


def validate_key(key):
    if not key or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise InvalidInput("Idempotency-Key must contain 8-128 letters, digits, '.', '_', ':' or '-'")
    return key


class RagService:
    def __init__(self, settings, store, model):
        self.settings, self.store, self.model = settings, store, model

    def ask(self, principal, question, key, conversation_id=None, kb_id=None):
        principal.require("reader")
        question = question.strip()
        if not question or len(question) > self.settings.max_question_chars:
            raise InvalidInput("Question is empty or too long")
        validate_key(key)
        claim = self.store.claim_chat(principal, key, question, conversation_id, kb_id)
        if "cached" in claim:
            return claim["cached"]
        try:
            deadline = time.monotonic() + 90
            history = self.store.history(principal, claim["conversation_id"])
            # Include recent user questions to resolve short follow-ups without another model call.
            prior = [m["content"] for m in history if m["role"] == "user"][-2:]
            query = "\n".join(prior + [question])[-self.settings.max_question_chars:]
            chain = build_rag_chain(self.settings, self.store, self.model, principal, claim["kb_id"], deadline)
            generated = invoke_rag_chain(chain, {"query": query, "question": question, "history": history})
            chosen, answer = generated["documents"], generated["answer"]
            citations = [dict(document.metadata) for document in chosen]
            if chosen:
                numbers = [int(n) for n in re.findall(r"\[(\d+)\]", answer)]
                if not numbers or any(n < 1 or n > len(chosen) for n in numbers):
                    answer = "模型未能提供可核验的资料引用，暂不提供该答案。请补充问题或联系知识库管理员。"
                    citations = []
                else:
                    used = list(dict.fromkeys(numbers))
                    remap = {old: new + 1 for new, old in enumerate(used)}
                    answer = re.sub(r"\[(\d+)\]", lambda m: f"[{remap[int(m.group(1))]}]", answer)
                    citations = [citations[n - 1] for n in used]
            if time.monotonic() > deadline:
                raise ProviderUnavailable("Generation deadline exceeded")
            result = {"answer": answer, "citations": citations, "conversation_id": claim["conversation_id"], "request_id": claim["request_id"]}
            self.store.finish_chat(principal, claim, question, result)
            return result
        except Exception:
            try:
                self.store.fail_chat(claim)
            except Exception:
                pass
            raise


class IndexWorker:
    def __init__(self, settings, store, model):
        self.settings, self.store, self.model = settings, store, model

    def run_once(self):
        job = self.store.claim_job()
        if not job:
            return False
        try:
            documents = split_documents(job["source_text"], self.settings, signature=job.get("signature"),
                                        metadata={"version_id": str(job["id"])})
            chunks = [document.page_content for document in documents]
            embeddings = []
            embedder = ProviderEmbeddings(self.model)
            for start in range(0, len(chunks), 10):
                self.store.renew_job(job)
                embeddings.extend(embedder.embed_documents(chunks[start:start + 10]))
            self.store.publish_job(job, chunks, embeddings)
        except Exception:
            self.store.fail_job(job)
        return True
