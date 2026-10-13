"""Optional authenticated client: streamlit run app_qa.py"""
import uuid
import streamlit as st
from web_client import call, connection

st.title("企业知识库问答")
base, headers = connection()
kb = st.sidebar.text_input("知识库 ID", value="default")
if st.sidebar.button("新对话"):
    for field in ("conversation_id", "messages", "pending_chat"):
        st.session_state.pop(field, None)
    st.rerun()
for message in st.session_state.get("messages", []):
    st.chat_message(message["role"]).write(message["content"])
    if message.get("citations"):
        with st.expander("本条回答的资料来源"):
            for index, citation in enumerate(message["citations"], 1):
                st.write(f"[{index}] {citation['filename']} · 片段 {citation['chunk_index']} · 版本 {citation['version_id']}")
prompt = st.chat_input("请输入问题", max_chars=4000, disabled="pending_chat" in st.session_state)
if prompt:
    body = {"question": prompt}
    if st.session_state.get("conversation_id"):
        body["conversation_id"] = st.session_state["conversation_id"]
    else:
        body["knowledge_base_id"] = kb
    st.session_state["pending_chat"] = {"body": body, "key": str(uuid.uuid4())}
retry = bool(st.session_state.get("pending_chat")) and st.button("提交 / 重试当前问题")
if prompt or retry:
    pending = st.session_state["pending_chat"]
    with st.spinner("正在检索并生成回答…"):
        result = call(base, {**headers, "Idempotency-Key": pending["key"]}, "POST", "/v1/chat", json=pending["body"])
    if result:
        st.session_state["conversation_id"] = result["conversation_id"]
        st.session_state.setdefault("messages", []).extend([
            {"role": "user", "content": pending["body"]["question"]},
            {"role": "assistant", "content": result["answer"], "citations": result["citations"]}])
        st.session_state.pop("pending_chat", None)
        st.rerun()
