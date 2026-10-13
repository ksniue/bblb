"""Optional debug UI. Identity and data access are enforced by the API."""
import hashlib
import os
import uuid
from datetime import datetime
from urllib.parse import urlsplit

import httpx
import streamlit as st

st.set_page_config(page_title="智扫通机器人智能客服")
st.title("智扫通机器人智能客服")
st.caption("API 调试界面；企业接入请通过受认证的服务端 API。")
base_url = os.environ.get("AGENT_API_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
parsed = urlsplit(base_url)
if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
    st.error("AGENT_API_BASE_URL 配置无效")
    st.stop()
token = st.sidebar.text_input("Bearer 访问凭证", type="password")
identity = hashlib.sha256(token.encode()).hexdigest()
if st.session_state.get("identity") != identity:
    st.session_state["identity"] = identity
    st.session_state["conversation_id"] = None
    st.session_state["messages"] = []
    st.session_state.pop("pending_request", None)
if st.sidebar.button("新建会话"):
    st.session_state["conversation_id"] = None
    st.session_state["messages"] = []
    st.session_state.pop("pending_request", None)
mode = st.sidebar.radio("服务", ["知识问答", "使用月报", "智能助手"])
knowledge_base = st.sidebar.text_input("知识库 ID（仅一个授权库时可留空）")


def call_api(path, payload, *, key=None):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    try:
        response = httpx.post(base_url + path, json=payload, headers=headers,
                              timeout=125, follow_redirects=False, trust_env=False)
        if response.status_code >= 300:
            st.error(f"请求失败（HTTP {response.status_code}），请核对访问权限及服务状态。")
            return None
        return response.json()
    except (httpx.RequestError, ValueError):
        st.error("服务连接失败或响应无效。知识问答可使用下方按钮重试同一请求。")
        return None


if not token:
    st.info("请输入管理员分配的访问凭证。")
    st.stop()
if mode == "智能助手":
    st.caption("输入设备使用问题，或明确指定 YYYY-MM 月份生成本人月报。一次处理一项任务。")
    with st.form("assistant"):
        question = st.text_area("需要什么帮助？", placeholder="滤芯如何维护？ / 生成我的 2026-10 使用月报")
        submitted = st.form_submit_button("提交给智能助手")
    if submitted and question.strip():
        payload = {"question": question.strip()}
        if knowledge_base:
            payload["knowledge_base_id"] = knowledge_base
        with st.spinner("理解请求并执行…"):
            result = call_api("/v1/assistant", payload, key=uuid.uuid4().hex)
        if result:
            st.text(result["answer"])
            if result.get("citations"):
                st.caption("来源：" + "、".join(item["filename"] for item in result["citations"]))
elif mode == "使用月报":
    with st.form("report"):
        month = st.text_input("报告月份（YYYY-MM）", value=datetime.now().strftime("%Y-%m"))
        submitted = st.form_submit_button("生成本人月报")
    if submitted:
        with st.spinner("读取本人记录并生成报告…"):
            result = call_api("/v1/reports", {"month": month})
        if result:
            st.text(result["answer"])
else:
    for message in st.session_state["messages"]:
        st.chat_message(message["role"]).write(message["content"])
    prompt = st.chat_input("输入知识问题")
    if prompt:
        payload = {"question": prompt}
        if st.session_state.get("conversation_id"):
            payload["conversation_id"] = st.session_state["conversation_id"]
        if knowledge_base:
            payload["knowledge_base_id"] = knowledge_base
        st.session_state["pending_request"] = {"payload": payload, "key": uuid.uuid4().hex}
    pending = st.session_state.get("pending_request")
    if pending and (prompt or st.button("重试上一条请求")):
        with st.spinner("检索并生成回答…"):
            result = call_api("/v1/chat", pending["payload"], key=pending["key"])
        if result:
            st.session_state["conversation_id"] = result["conversation_id"]
            st.session_state["messages"].extend([
                {"role": "user", "content": pending["payload"]["question"]},
                {"role": "assistant", "content": result["answer"]},
            ])
            st.session_state.pop("pending_request", None)
            st.rerun()
