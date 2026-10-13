"""Tool inputs contain business parameters only; identity stays in closures."""
from collections.abc import Awaitable, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from service.contracts import ChatResponse, ReportRequest, ReportResponse


class KnowledgeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def build_tools(knowledge: Callable[[], Awaitable[ChatResponse]],
                report: Callable[[str], Awaitable[ReportResponse]], *, reports_enabled: bool) -> dict[str, StructuredTool]:
    tools = {"knowledge_qa": StructuredTool.from_function(
        coroutine=knowledge, name="knowledge_qa",
        description="回答清洁设备使用、故障排查、说明书等知识问题。使用服务端保存的原始问题、会话和授权知识库；没有参数。",
        args_schema=KnowledgeArguments,
    )}
    if reports_enabled:
        tools["generate_usage_report"] = StructuredTool.from_function(
            coroutine=report, name="generate_usage_report",
            description="只为当前已认证用户生成使用月报。month 必须是用户本条问题明确提供的 YYYY-MM；月份不明确时不要调用。",
            args_schema=ReportRequest,
        )
    return tools
