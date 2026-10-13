import json
from urllib.parse import quote

import httpx
from langsmith import tracing_context
from pydantic import ValidationError

from service.auth import Principal
from service.config import Settings
from service.contracts import ReportResponse, UsageRecord
from service.llm_adapters import BoundedChatModel
from service.report_chain import build_report_chain
from service.upstream import ServiceError, request_json, require_success


class ReportService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def generate(self, principal: Principal, month: str, request_id: str) -> ReportResponse:
        if not self.settings.reports_enabled:
            raise ServiceError(503, "reports_disabled", "Reports are not configured on this deployment")
        # Only server-authenticated identity controls the target account.
        url = f"{self.settings.usage_api_base_url.rstrip('/')}/v1/users/{quote(principal.user_id, safe='')}/usage"
        status, raw = await request_json(
            self.client, "GET", url, params={"month": month},
            headers={"Authorization": f"Bearer {self.settings.usage_api_token}",
                     "X-Tenant-ID": principal.tenant_id, "X-Request-ID": request_id},
        )
        require_success(status, no_data=True)
        try:
            facts = UsageRecord.model_validate(raw)
        except ValidationError:
            raise ServiceError(502, "invalid_usage_record", "Usage service returned an invalid record") from None
        if (facts.tenant_id, facts.user_id, facts.month) != (principal.tenant_id, principal.user_id, month):
            raise ServiceError(502, "usage_identity_mismatch", "Usage service returned a record for a different account or month")

        # A real LangChain prompt -> async model -> strict parser chain runs once.
        model_facts = facts.model_dump(exclude={"tenant_id", "user_id"})
        chain = build_report_chain(BoundedChatModel(self.settings, self.client, request_id))
        # Business facts must not be uploaded by ambient LangSmith environment flags.
        with tracing_context(enabled=False):
            recommendations = (await chain.ainvoke({"facts": json.dumps(model_facts, ensure_ascii=False)})).recommendations
        # Numeric facts are rendered by code, never delegated to the model.
        lines = [f"{month} 使用报告", f"清洁次数：{facts.cleaning_count}",
                 f"清洁时长：{facts.cleaning_minutes:g} 分钟", f"清洁面积：{facts.area_sqm:g} 平方米"]
        lines.extend(f"{item.name}剩余寿命：{item.remaining_percent:g}%" for item in facts.consumables)
        lines.extend(["", "维护建议（AI 生成，具体操作以设备说明书为准）："])
        lines.extend(f"{index}. {value}" for index, value in enumerate(recommendations, 1))
        return ReportResponse(answer="\n".join(lines), facts=facts, recommendations=recommendations, request_id=request_id)
