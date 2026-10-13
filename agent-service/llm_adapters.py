"""Native async LangChain model adapter using the service's bounded HTTP client."""
import json
from typing import Any, Sequence

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from service.config import Settings
from service.upstream import ServiceError, request_json, require_success


class BoundedChatModel(BaseChatModel):
    """No SDK retries, threads, streaming, redirects, or unbounded response bodies."""

    model_name: str
    _settings: Settings = PrivateAttr()
    _client: httpx.AsyncClient = PrivateAttr()
    _request_id: str = PrivateAttr()

    def __init__(self, settings: Settings, client: httpx.AsyncClient, request_id: str):
        super().__init__(model_name=settings.model_name, cache=False)
        self._settings, self._client, self._request_id = settings, client, request_id

    @property
    def _llm_type(self) -> str:
        return "bounded-enterprise-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: str = "auto", **kwargs: Any):
        if tool_choice != "auto" or kwargs:
            raise ValueError("Only the controlled auto tool selection is supported")
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], tool_choice=tool_choice)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("Use ainvoke; blocking model execution is disabled")

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        if stop or set(kwargs) - {"tools", "tool_choice"}:
            raise ValueError("Unsupported model invocation options")
        payload_messages = []
        for message in messages:
            if not isinstance(message.content, str):
                raise ValueError("Only text messages are supported")
            if isinstance(message, SystemMessage):
                role = "system"
            elif isinstance(message, HumanMessage):
                role = "user"
            else:
                raise ValueError("Only system and user input messages are supported")
            payload_messages.append({"role": role, "content": message.content})
        payload = {"model": self.model_name, "temperature": 0,
                   "max_tokens": self._settings.model_max_tokens, "messages": payload_messages}
        payload.update(kwargs)
        status, response = await request_json(
            self._client, "POST", f"{self._settings.model_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self._settings.model_api_key}",
                     "X-Request-ID": self._request_id}, json=payload,
        )
        require_success(status)
        try:
            if not isinstance(response, dict) or not isinstance(response.get("choices"), list):
                raise ValueError("Invalid model response envelope")
            choice = response["choices"][0]
            if not isinstance(choice, dict):
                raise ValueError("Invalid model choice")
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Invalid model message")
            content = message.get("content")
            calls = message.get("tool_calls")
            tool_calls = []
            if calls:
                if not kwargs.get("tools") or choice.get("finish_reason") != "tool_calls":
                    raise ValueError("Unexpected tool call")
                if not isinstance(calls, list) or len(calls) != 1:
                    raise ValueError("Only one tool call is allowed")
                call = calls[0]
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str):
                    raise ValueError("Invalid tool call")
                function = call["function"]
                if not isinstance(function, dict):
                    raise ValueError("Invalid tool function")
                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict) or not isinstance(function["name"], str):
                    raise ValueError("Invalid tool arguments")
                tool_calls = [{"name": function["name"], "args": arguments, "id": call["id"]}]
                content = ""  # Free text beside a tool call is never exposed.
            elif choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                raise ValueError("Incomplete model response")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content, tool_calls=tool_calls))])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServiceError(502, "invalid_model_response", "The model did not return a complete valid response") from None
