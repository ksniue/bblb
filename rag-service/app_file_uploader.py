"""Optional authenticated ingestion client; reruns do not implicitly upload."""
import hashlib
import json
import uuid
import streamlit as st
from web_client import call, connection

st.title("知识库文档管理")
base, headers = connection()
with st.form("upload"):
    kb = st.text_input("知识库 ID", value="default")
    doc_id = st.text_input("更新现有文档时填写文档 ID；留空表示新建")
    upload = st.file_uploader("上传 UTF-8 TXT（不超过 5 MB）", type=["txt"])
    submitted = st.form_submit_button("提交索引任务")
if submitted and upload is not None:
    content = upload.getvalue()
    normalized_doc_id = doc_id.strip()
    signature_payload = json.dumps(
        [kb, upload.name, hashlib.sha256(content).hexdigest(), normalized_doc_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hashlib.sha256(signature_payload).hexdigest()
    keys = st.session_state.setdefault("upload_keys", {})
    key = keys.setdefault(signature, str(uuid.uuid4()))
    data = {"knowledge_base_id": kb}
    if normalized_doc_id:
        data["document_id"] = normalized_doc_id
    result = call(base, {**headers, "Idempotency-Key": key}, "POST", "/v1/documents",
                  data=data, files={"file": (upload.name, content, upload.type)})
    if result:
        st.session_state["last_version"] = result["version_id"]
        st.json(result)
if st.session_state.get("last_version") and st.button("刷新最近任务状态"):
    result = call(base, headers, "GET", "/v1/versions/" + st.session_state["last_version"])
    if result:
        st.json(result)
