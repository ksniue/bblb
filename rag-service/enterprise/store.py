import hashlib
import json
import time
import uuid

from .errors import Conflict, NotFound, RateLimited


def vector_literal(values):
    import math
    if not values or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
        raise ValueError("Invalid embedding")
    return "[" + ",".join(str(float(v)) for v in values) + "]"


class Store:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def limit(self, p):
        scope = hashlib.sha256(f"{p.tenant_id}:{p.user_id}".encode()).hexdigest()
        bucket = int(time.time() // 60)
        with self.db.connection() as c:
            n = c.execute("""INSERT INTO rate_limits(scope,bucket,count) VALUES(%s,%s,1)
                ON CONFLICT(scope,bucket) DO UPDATE SET count=rate_limits.count+1 RETURNING count""", (scope, bucket)).fetchone()["count"]
            c.execute("DELETE FROM rate_limits WHERE bucket < %s", (bucket - 2,))
        if n > self.settings.requests_per_minute:
            raise RateLimited()

    def _conversation(self, c, p, conversation_id, lock=False):
        row = c.execute("SELECT * FROM conversations WHERE id=%s AND tenant_id=%s AND user_id=%s" + (" FOR UPDATE" if lock else ""),
                        (conversation_id, p.tenant_id, p.user_id)).fetchone()
        if not row:
            raise NotFound()
        p.require("reader", row["kb_id"])
        return row

    def claim_chat(self, p, key, question, conversation_id, kb_id):
        p.require("reader")
        digest = hashlib.sha256(json.dumps([question, conversation_id, kb_id], ensure_ascii=False).encode()).hexdigest()
        with self.db.connection() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"chat:{p.tenant_id}:{p.user_id}:{key}",))
            old = c.execute("SELECT * FROM chat_requests WHERE tenant_id=%s AND user_id=%s AND idempotency_key=%s",
                            (p.tenant_id, p.user_id, key)).fetchone()
            if old:
                self._conversation(c, p, old["conversation_id"])
                if old["payload_hash"] != digest:
                    raise Conflict("Idempotency key was used with a different request")
                if old["state"] == "complete":
                    self._validate_citations(c, p, old["response"].get("citations", []))
                    return {"cached": old["response"]}
                conversation_id = old["conversation_id"]
            if conversation_id:
                conv = self._conversation(c, p, conversation_id, lock=True)
                if old:
                    # Completion may have committed while we waited for the conversation lock.
                    old = c.execute("SELECT * FROM chat_requests WHERE id=%s FOR UPDATE", (old["id"],)).fetchone()
                    if old["state"] == "complete":
                        self._validate_citations(c, p, old["response"].get("citations", []))
                        return {"cached": old["response"]}
                if kb_id and kb_id != conv["kb_id"]:
                    raise Conflict("Conversation belongs to a different knowledge base")
                kb_id = conv["kb_id"]
            else:
                if kb_id is None:
                    if len(p.kb_ids) != 1:
                        raise Conflict("Choose a knowledge_base_id")
                    kb_id = next(iter(p.kb_ids))
                p.require("reader", kb_id)
                conversation_id = uuid.uuid4()
                c.execute("INSERT INTO conversations(id,tenant_id,user_id,kb_id) VALUES(%s,%s,%s,%s)",
                          (conversation_id, p.tenant_id, p.user_id, kb_id))
            active = c.execute("SELECT id FROM chat_requests WHERE conversation_id=%s AND state='running' AND lease_until>now()",
                               (conversation_id,)).fetchone()
            if active:
                raise Conflict("A response is already being generated for this conversation")
            c.execute("UPDATE chat_requests SET state='failed',failure_code='lease_expired' WHERE conversation_id=%s AND state='running'", (conversation_id,))
            request_id = old["id"] if old else uuid.uuid4()
            token = uuid.uuid4()
            if old:
                c.execute("UPDATE chat_requests SET state='running',lease_token=%s,lease_until=now()+(%s * interval '1 second'),failure_code=NULL WHERE id=%s",
                          (token, self.settings.lease_seconds, request_id))
            else:
                c.execute("""INSERT INTO chat_requests(id,tenant_id,user_id,idempotency_key,conversation_id,payload_hash,question,state,lease_token,lease_until)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'running',%s,now()+(%s * interval '1 second'))""",
                          (request_id, p.tenant_id, p.user_id, key, conversation_id, digest, question, token, self.settings.lease_seconds))
            return {"request_id": str(request_id), "conversation_id": str(conversation_id), "lease_token": str(token), "kb_id": kb_id}

    def history(self, p, conversation_id, bounded=True):
        with self.db.connection() as c:
            self._conversation(c, p, conversation_id)
            rows = c.execute("""SELECT role,content,citations,created_at FROM
                (SELECT * FROM messages WHERE conversation_id=%s ORDER BY id DESC LIMIT %s) m ORDER BY id""",
                             (conversation_id, 30 if bounded else 200)).fetchall()
        # Recalled sources must not re-enter generation through previous answers.
        with self.db.connection() as c:
            for row in rows:
                try:
                    self._validate_citations(c, p, row["citations"])
                except Conflict:
                    row["content"] = "[此消息引用的资料已更新或撤回，请重新提问。]"
                    row["citations"] = []
        if not bounded:
            return rows
        result, size = [], 0
        for row in reversed(rows):
            size += len(row["content"])
            if size > self.settings.history_chars:
                break
            result.append({"role": row["role"], "content": row["content"]})
        return list(reversed(result))

    def _validate_citations(self, c, p, citations):
        for citation in citations:
            row = c.execute("""SELECT d.id FROM documents d WHERE d.id=%s AND d.tenant_id=%s
                AND d.kb_id=ANY(%s) AND d.current_version=%s AND NOT d.deleted FOR SHARE""",
                            (citation["document_id"], p.tenant_id, list(p.kb_ids), citation["version_id"])).fetchone()
            if not row:
                raise Conflict("Source document changed; submit a new request")

    def finish_chat(self, p, claim, question, response):
        with self.db.connection() as c:
            self._conversation(c, p, claim["conversation_id"], lock=True)
            req = c.execute("SELECT * FROM chat_requests WHERE id=%s FOR UPDATE", (claim["request_id"],)).fetchone()
            if req["state"] != "running" or str(req["lease_token"]) != claim["lease_token"]:
                raise Conflict("Response lease was superseded")
            self._validate_citations(c, p, response["citations"])
            for role, content, citations in (("user", question, []), ("assistant", response["answer"], response["citations"])):
                c.execute("INSERT INTO messages(conversation_id,request_id,role,content,citations) VALUES(%s,%s,%s,%s,%s::jsonb)",
                          (claim["conversation_id"], claim["request_id"], role, content, json.dumps(citations)))
            c.execute("UPDATE chat_requests SET state='complete',response=%s::jsonb WHERE id=%s", (json.dumps(response), claim["request_id"]))

    def fail_chat(self, claim):
        with self.db.connection() as c:
            c.execute("UPDATE chat_requests SET state='failed',failure_code='generation_failed' WHERE id=%s AND lease_token=%s AND state='running'",
                      (claim["request_id"], claim["lease_token"]))

    def create_version(self, p, kb_id, filename, text, document_id, key):
        p.require("editor", kb_id)
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        signature = self.settings.index_signature
        def hash_payload(index_signature):
            return hashlib.sha256(json.dumps([kb_id, filename, content_hash, document_id, index_signature]).encode()).hexdigest()
        payload_hash = hash_payload(signature)
        new_doc = document_id is None
        doc_id = document_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"rag:{p.tenant_id}:{p.user_id}:{key}"))
        with self.db.connection() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"upload:{p.tenant_id}:{p.user_id}:{key}",))
            receipt = c.execute("SELECT * FROM upload_requests WHERE tenant_id=%s AND user_id=%s AND idempotency_key=%s",
                                (p.tenant_id, p.user_id, key)).fetchone()
            if receipt:
                recorded = c.execute("SELECT signature FROM versions WHERE id=%s", (receipt["version_id"],)).fetchone()
                if not recorded or recorded["signature"] not in self.settings.readable_index_signatures:
                    raise Conflict("Upload receipt uses an incompatible index signature")
                signature = recorded["signature"]
                payload_hash = hash_payload(signature)
                if receipt["payload_hash"] != payload_hash:
                    raise Conflict("Upload idempotency key was used with different content")
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"document:{doc_id}",))
            doc = c.execute("SELECT * FROM documents WHERE id=%s FOR UPDATE", (doc_id,)).fetchone()
            if not doc:
                if not new_doc:
                    raise NotFound()
                c.execute("INSERT INTO documents(id,tenant_id,kb_id,filename,created_by) VALUES(%s,%s,%s,%s,%s)",
                          (doc_id, p.tenant_id, kb_id, filename, p.user_id))
            elif doc["tenant_id"] != p.tenant_id or doc["kb_id"] != kb_id or doc["deleted"]:
                raise NotFound()
            old = c.execute("SELECT * FROM versions WHERE document_id=%s AND content_hash=%s AND signature=%s",
                            (doc_id, content_hash, signature)).fetchone()
            busy = c.execute("SELECT id FROM versions WHERE document_id=%s AND state IN ('pending','processing')", (doc_id,)).fetchone()
            if old:
                if old["state"] == "superseded":
                    raise Conflict("This content belongs to a superseded version; explicit rollback is not supported")
                if old["state"] == "failed":
                    if busy:
                        raise Conflict("A document version is already being indexed")
                    c.execute("UPDATE versions SET state='pending',attempts=0,failure_code=NULL WHERE id=%s", (old["id"],))
                c.execute("""INSERT INTO upload_requests(tenant_id,user_id,idempotency_key,payload_hash,document_id,version_id)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (p.tenant_id, p.user_id, key, payload_hash, doc_id, old["id"]))
                return {"document_id": str(doc_id), "version_id": str(old["id"]), "state": "pending" if old["state"] == "failed" else old["state"]}
            if new_doc and doc:
                raise Conflict("Upload idempotency key was already used")
            if busy:
                raise Conflict("A document version is already being indexed")
            version_id = uuid.uuid4()
            c.execute("INSERT INTO versions(id,document_id,content_hash,source_text,signature,state) VALUES(%s,%s,%s,%s,%s,'pending')",
                      (version_id, doc_id, content_hash, text, signature))
            c.execute("INSERT INTO upload_requests(tenant_id,user_id,idempotency_key,payload_hash,document_id,version_id) VALUES(%s,%s,%s,%s,%s,%s)",
                      (p.tenant_id, p.user_id, key, payload_hash, doc_id, version_id))
            return {"document_id": str(doc_id), "version_id": str(version_id), "state": "pending"}

    def documents(self, p, kb_id):
        p.require("reader", kb_id)
        with self.db.connection() as c:
            return c.execute("""SELECT d.id,d.filename,d.current_version,d.created_at FROM documents d
                WHERE tenant_id=%s AND kb_id=%s AND NOT deleted ORDER BY created_at DESC LIMIT 200""", (p.tenant_id, kb_id)).fetchall()

    def version(self, p, version_id):
        with self.db.connection() as c:
            row = c.execute("""SELECT v.id,v.document_id,v.state,v.attempts,v.failure_code,v.chunk_count,d.kb_id
                FROM versions v JOIN documents d ON d.id=v.document_id WHERE v.id=%s AND d.tenant_id=%s AND NOT d.deleted""",
                            (version_id, p.tenant_id)).fetchone()
        if not row:
            raise NotFound()
        p.require("reader", row["kb_id"])
        return row

    def delete_document(self, p, document_id):
        with self.db.connection() as c:
            doc = c.execute("SELECT * FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE", (document_id, p.tenant_id)).fetchone()
            if not doc:
                raise NotFound()
            p.require("editor", doc["kb_id"])
            c.execute("UPDATE documents SET deleted=true,current_version=NULL WHERE id=%s", (document_id,))
            c.execute("UPDATE versions SET state='superseded',source_text='',lease_token=NULL WHERE document_id=%s", (document_id,))
            c.execute("DELETE FROM chunks WHERE version_id IN (SELECT id FROM versions WHERE document_id=%s)", (document_id,))

    def claim_job(self):
        with self.db.connection() as c:
            c.execute("""UPDATE versions SET state='failed',failure_code='worker_lease_expired',lease_token=NULL
                WHERE state='processing' AND lease_until<now() AND attempts>=3""")
            job = c.execute("""SELECT v.* FROM versions v JOIN documents d ON d.id=v.document_id
                WHERE NOT d.deleted AND v.signature=ANY(%s) AND v.attempts<3 AND
                (v.state='pending' OR (v.state='processing' AND v.lease_until<now()))
                ORDER BY v.created_at FOR UPDATE OF v SKIP LOCKED LIMIT 1""", (list(self.settings.readable_index_signatures),)).fetchone()
            if not job:
                return None
            token = uuid.uuid4()
            c.execute("UPDATE versions SET state='processing',attempts=attempts+1,lease_token=%s,lease_until=now()+(%s * interval '1 second') WHERE id=%s",
                      (token, self.settings.lease_seconds, job["id"]))
            job["lease_token"] = str(token)
            return job

    def renew_job(self, job):
        with self.db.connection() as c:
            result = c.execute("UPDATE versions SET lease_until=now()+(%s * interval '1 second') WHERE id=%s AND lease_token=%s AND state='processing'",
                               (self.settings.lease_seconds, job["id"], job["lease_token"]))
            if result.rowcount != 1:
                raise Conflict("Indexing lease was superseded")

    def publish_job(self, job, chunks, embeddings):
        if len(chunks) != len(embeddings) or not chunks:
            raise ValueError("Incomplete indexing result")
        with self.db.connection() as c:
            doc = c.execute("SELECT * FROM documents WHERE id=%s FOR UPDATE", (job["document_id"],)).fetchone()
            version = c.execute("SELECT * FROM versions WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
            if doc["deleted"] or version["state"] != "processing" or str(version["lease_token"]) != job["lease_token"]:
                raise Conflict("Indexing job is no longer current")
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                if len(embedding) != self.settings.embedding_dimension:
                    raise ValueError("Embedding dimension mismatch")
                chunk_id = uuid.uuid5(uuid.UUID(str(job["id"])), str(i))
                c.execute("INSERT INTO chunks(id,version_id,ordinal,content,embedding) VALUES(%s,%s,%s,%s,%s::vector) ON CONFLICT(id) DO UPDATE SET content=excluded.content,embedding=excluded.embedding",
                          (chunk_id, job["id"], i, chunk, vector_literal(embedding)))
            c.execute("UPDATE versions SET state='superseded' WHERE document_id=%s AND state='ready'", (job["document_id"],))
            c.execute("UPDATE versions SET state='ready',chunk_count=%s,lease_until=NULL WHERE id=%s", (len(chunks), job["id"]))
            c.execute("UPDATE documents SET current_version=%s WHERE id=%s", (job["id"], job["document_id"]))

    def fail_job(self, job):
        with self.db.connection() as c:
            c.execute("""UPDATE versions SET state=CASE WHEN attempts<3 THEN 'pending' ELSE 'failed' END,
                failure_code='indexing_failed',lease_until=NULL WHERE id=%s AND lease_token=%s AND state='processing'""",
                      (job["id"], job["lease_token"]))

    def search(self, p, kb_id, embedding):
        p.require("reader", kb_id)
        vector = vector_literal(embedding)
        with self.db.connection() as c:
            return c.execute("""SELECT ch.content,ch.ordinal,ch.id AS chunk_id,d.id AS document_id,
                d.filename,v.id AS version_id,(ch.embedding <=> %s::vector) AS distance
                FROM chunks ch JOIN versions v ON v.id=ch.version_id JOIN documents d ON d.id=v.document_id
                WHERE d.tenant_id=%s AND d.kb_id=%s AND NOT d.deleted AND d.current_version=v.id
                AND v.state='ready' AND v.signature=ANY(%s) AND (ch.embedding <=> %s::vector)<=%s
                ORDER BY ch.embedding <=> %s::vector LIMIT %s""",
                             (vector, p.tenant_id, kb_id, list(self.settings.readable_index_signatures), vector,
                              self.settings.max_cosine_distance, vector, self.settings.top_k)).fetchall()
