"""LangChain interfaces over the existing bounded, validated provider client."""
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class ProviderEmbeddings(Embeddings):
    def __init__(self, client, deadline=None):
        self._client, self._deadline = client, deadline

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed(texts, deadline=self._deadline)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class ProviderChatModel(BaseChatModel):
    # Keep HTTP clients, credentials and request deadlines out of serialization.
    _client: Any = PrivateAttr()
    _deadline: float | None = PrivateAttr(default=None)

    def __init__(self, client, deadline=None):
        super().__init__(cache=False)
        self._client, self._deadline = client, deadline

    @property
    def _llm_type(self) -> str:
        return "bounded-enterprise-chat"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        if stop or kwargs:
            raise ValueError("Unsupported provider invocation options")
        roles = {"system": "system", "human": "user", "ai": "assistant"}
        payload = []
        for message in messages:
            if message.type not in roles or not isinstance(message.content, str):
                raise ValueError("Only text chat messages are supported")
            payload.append({"role": roles[message.type], "content": message.content})
        answer = self._client.chat(payload, deadline=self._deadline)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])
