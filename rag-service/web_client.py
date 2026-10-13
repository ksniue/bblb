"""Optional Streamlit API client; no provider keys or direct database access."""
import os
from urllib.parse import urlparse
import httpx
import streamlit as st

def connection():
    base = os.getenv("RAG_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "api"}):
        st.error("RAG_API_BASE_URL must use HTTPS except local/container service.")
        st.stop()
    with st.sidebar.form("credentials"):
        token = st.text_input("访问令牌", type="password", autocomplete="off")
        connected = st.form_submit_button("连接 / 切换用户")
    if connected:
        st.session_state.clear()
        st.session_state["access_token"] = token
        st.rerun()
    if st.sidebar.button("退出并清除当前会话"):
        st.session_state.clear()
        st.rerun()
    token = st.session_state.get("access_token")
    if not token:
        st.info("请先在侧栏输入管理员分配的访问令牌。")
        st.stop()
    return base, {"Authorization": "Bearer " + token}

def call(base, headers, method, path, **kwargs):
    try:
        with httpx.Client(timeout=125, follow_redirects=False, trust_env=False) as client:
            response = client.request(method, base + path, headers=headers, **kwargs)
        if not response.is_success:
            st.error(f"服务未完成请求（HTTP {response.status_code}）。请检查权限或稍后重试。")
            return None
        return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError):
        st.error("服务暂时不可用。可使用原请求重试，避免重复提交。")
        return None
