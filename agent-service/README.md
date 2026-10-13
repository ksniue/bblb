# 智扫通：基于 LangChain 的知识问答与使用报告助手

本项目使用 FastAPI 提供接口，LangChain 组织报告建议生成链和受控工具调用 Agent。知识问答与会话由相邻的 `rag-service` LangChain RAG 服务负责；本项目负责统一入口、身份校验、工具选择和个人报告流程。旧版随机身份、固定天气、任意用户记录工具及本地 Chroma 入口已禁用。原 CSV、日志和 Chroma 数据保留在磁盘中，不会自动导入服务，也不会打入 Docker 镜像。

## LangChain 架构

- `service/llm_adapters.py`：真正实现 LangChain `BaseChatModel` 的异步模型适配器。复用受限 HTTPX 客户端，支持原生 `bind_tools`，不增加自动重试或不可取消的后台线程。
- `service/report_chain.py`：`ChatPromptTemplate → BoundedChatModel → StrictRecommendationsParser` 组成 LCEL 链；严格验证完整 JSON 和 Pydantic 契约。身份与数字事实校验仍由 `service/reports.py` 完成。
- `service/tools.py`：通过 `StructuredTool` 注册 `knowledge_qa` 和 `generate_usage_report`。知识工具无可填写参数，使用服务端保留的原始问题、授权知识库和会话；月报工具只接受 `month`。
- `service/assistant.py`：一次模型工具选择、至多一次工具执行；无工具调用时返回固定澄清提示。报告月份必须在本条问题中明确出现且只有一个月份。工具结果直接返回，不经过额外模型改写；不提供自主循环或多智能体协作。

用户身份、凭证、服务 URL、会话及知识库选择都不会作为工具参数交给模型。业务事实仍会发送至已配置模型生成建议。链与工具执行用 `tracing_context(enabled=False)` 禁止环境变量隐式开启 LangSmith 外传；本地请求日志仍保留脱敏的请求 ID、耗时和状态。

依赖由 `requirements.txt` 和自动引用的 `constraints.txt` 固定。克隆后按部署说明创建本地虚拟环境并安装依赖；试用可选页面前还需安装 `requirements-ui.txt`。验证范围与历史结果见本仓库的 `docs/verification.md`。本机备份包含配置或数据，不属于公开仓库内容，也不能替代外部 PostgreSQL 数据库备份。

## 服务边界

- `POST /v1/chat`：转发到固定的 `RAG_BASE_URL`，携带当前用户凭证；RAG 再次校验身份、知识库权限与会话归属。要求 `Idempotency-Key`；用同一 key 重试时必须保留完全相同的请求字段。最终输出仅包含答案、引用、会话 ID 和请求 ID。
- `GET /v1/conversations/{id}/messages`：获取本人会话中的用户与助手消息，最多 200 条。工具消息不会通过本入口返回。
- `POST /v1/reports`：只接受 `month`，服务端从身份映射确定租户与用户。固定执行一次业务数据查询、结构校验及身份回校验，再执行最多一次模型建议生成。数值事实由代码渲染。报告不持久化；重复生成可能重复消耗模型额度，不提供跨进程报告幂等保证。
- `POST /v1/assistant`：请求字段与 `/v1/chat` 相同，也要求 `Idempotency-Key`。模型最多选择一个工具执行；返回 `action`、`answer`、`citations`、`conversation_id`、`report` 和 `request_id`。默认关闭，通过 `ASSISTANT_ENABLED=true` 启用。知识请求最多一次路由模型加一次 RAG 回答模型；报告请求最多一次路由模型加一次建议模型。禁用报告时不会向模型提供月报工具。
- `GET /health/live`：进程存活，无需认证。
- `GET /health/ready`：需要 reader/admin 凭证，检查 RAG 的数据库 readiness。返回 `reports_enabled`、`assistant_enabled` 和 `usage_dependency=validated_configuration_only`；不会为了探活请求收费模型，也不等同于业务 API/模型已联调成功。

没有匿名模式，没有随机数据兜底，没有默认访问凭证。`REPORTS_ENABLED=true` 时业务接口及模型配置缺失会导致启动失败；显式关闭报告时，月报接口返回 503。`ASSISTANT_ENABLED=true` 也要求配置模型地址、名称和密钥；可以单独启用知识路由而不启用报告。

## 身份映射与授权

Agent 与 RAG 必须挂载**同一份**只读身份文件，以免两边对同一凭证解释不同。文件为非空 JSON 数组：

```json
[
  {
    "token_sha256": "此处填写真实高强度Bearer凭证的SHA256十六进制摘要",
    "tenant_id": "tenant-a",
    "user_id": "user-a",
    "roles": ["reader"],
    "knowledge_base_ids": ["manuals"]
  }
]
```

上例中的摘要占位符故意不能通过校验。使用安全随机生成器创建至少 32 字符的用户凭证，只保存其摘要到该文件，并通过企业现有渠道向用户发放原始凭证。`reader` 或 `admin` 可调用问答、历史和本人报告；需要上传知识的人员还要在 RAG 侧具备 `editor` 或 `admin`。身份/库 ID 使用 1–80 位字母、数字、下划线、连字符或点。用户输入和请求体不能设置 `tenant_id`、`user_id` 或角色。

这是服务端配置管理的静态 Bearer 身份映射，**不是已经完成 SSO/JWT 集成**。文件在启动时读取，更换或撤销凭证后需重启两个服务。用短有效期组织凭证轮换，限制 secret 文件权限。管理员角色也只能生成本人报告；跨用户报告没有开放。

## 部署

建议使用联合作业文件，具体见 [部署与联调说明](docs/deployment.md)。单独部署本服务时：

1. 安装 Python 3.11，创建虚拟环境，运行 `python -m pip install -r requirements.txt`。
2. 根据 `.env.example` 设置真实环境变量。命令行启动不会自动读取 `.env`；Docker Compose 的 `env_file` 会读取。所有 secrets 通过 `*_FILE` 文件或环境变量提供，日志不输出凭证、输入原文和业务记录。
3. 运行 `python -m uvicorn service.main:app --host 127.0.0.1 --port 8001 --no-access-log`。一个容器使用一个 worker。

Docker 单服务启动：配置 `.env` 和 `secrets/`，将 `RAG_BASE_URL` 指向本容器能够访问的已部署服务，然后 `docker compose up --build -d`。单服务 compose 不创建 RAG；只有联合部署的私有网络中才可以使用 `http://rag:8000`。端口默认只绑定宿主机回环地址，外部访问应经过企业 TLS 网关。

本机查看界面可以运行 `python tools/local_preview.py start`，前提是两个项目保持同级目录、安装了两项目及 Streamlit/psycopg 依赖、Docker Desktop 中已有 pgvector 镜像。脚本只绑定 `127.0.0.1`，生成独立的临时数据库和随机登录凭证，预置一份合成 TXT，再启动 RAG、worker、Agent 和两张 Streamlit 调试页。RAG 问答使用固定的模拟向量与明确标注的模拟回答，不调用真实模型；报告关闭。页面地址和凭证文件位置会打印出来。试完运行 `python tools/local_preview.py stop`，演示数据库及生成的凭据会删除。该命令不是正式部署方式。

HTTP 默认被拒绝。可信私网 RAG/业务 API 确实使用明文时显式设置 `ALLOW_INTERNAL_HTTP=true`；它不会允许模型密钥通过 HTTP 发送。只有自托管私网模型明确需要时才设置 `ALLOW_MODEL_HTTP=true`。客户端禁用环境代理和自动重定向，避免凭证被意外转发。

## 业务使用记录接口

开启报告前，需要为真实业务系统实现以下只读接口（本仓库不会假设现有 CSV 是企业 API）：

```http
GET {USAGE_API_BASE_URL}/v1/users/{authenticated_user_id}/usage?month=2026-10
Authorization: Bearer <内部业务访问密钥>
X-Tenant-ID: <authenticated_tenant_id>
X-Request-ID: <追踪编号>
```

成功响应必须满足以下结构，不允许额外未知字段：

```json
{
  "tenant_id": "tenant-a",
  "user_id": "user-a",
  "month": "2026-10",
  "cleaning_count": 12,
  "cleaning_minutes": 360,
  "area_sqm": 800,
  "consumables": [{"name": "滤芯", "remaining_percent": 35}],
  "source_version": "usage-snapshot-2026-10-v1"
}
```

业务 API 本身也必须验证内部凭证及租户范围。无记录返回 404，不要把无记录与接口故障混成空字符串。数量必须为整数，指标不得为负、NaN 或无穷值；耗材百分比为 0–100。身份或月份不一致时 Agent 返回 502，且不会将记录发送给模型。

模型配置为 OpenAI 兼容的 `/chat/completions` 地址前缀，必须支持 `max_tokens`、`temperature`、`choices[].finish_reason` 和文本 JSON 输出。直接请求 `/v1/reports` 最多调用一次模型，该报告链中的模型只能生成结构化维护建议，无法调用工具。启用 `/v1/assistant` 时供应商还必须支持原生 `tools`、`tool_choice=auto` 和 `finish_reason=tool_calls`；路由后才执行被选中的工具，不进行第二轮工具规划。身份 ID、内部访问密钥不出现在模型消息中；使用指标和耗材资料会发送至所配置模型提供方，请按企业数据策略选择服务。代码只接受正常结束的完整 JSON，不接受截断输出。

## 请求示例与故障语义

知识问答 JSON：`{"question":"滤芯如何维护？","knowledge_base_id":"manuals"}`。后续问题传回响应的 `conversation_id`；新会话只授权一个知识库时可省略库 ID。所有请求需要 `Authorization: Bearer ...`，问答还需要如 `Idempotency-Key: request-20261008-001`，字符限于字母、数字、`._:-`，长度 8–128。

个人报告 JSON：`{"month":"2026-10"}`，不支持任意用户参数。月份必须为 `YYYY-MM`。

智能助手：先配置 `ASSISTANT_ENABLED=true` 并重启服务，再向 `/v1/assistant` 提交 `{"question":"滤芯如何维护？","knowledge_base_id":"manuals"}` 或 `{"question":"生成我的 2026-10 使用月报"}`；后者还需 `REPORTS_ENABLED=true` 及真实业务接口。`app.py` 的“智能助手”模式可用于试用。相对月份如“上个月”当前会要求明确 YYYY-MM，不依赖模型推算月份。

`/v1/assistant` 不持久化路由决策或报告。幂等键只传给下游知识问答，不能保证整个助手请求幂等，重试仍会消耗路由模型额度。知识工具传递原始问题，避免模型改写问题造成幂等冲突；需要稳定重试的知识请求请直接用 `/v1/chat`。各步骤共用一次 120 秒业务总时限，不为每个模型或工具重新分配预算。

| 状态 | 含义与处理 |
|---|---|
| 401 / 403 | 凭证无效或权限不足；修复身份配置，不盲目重试 |
| 404 | 个人记录不存在，或 RAG 会话不可访问；不生成假报告 |
| 409 | RAG 幂等冲突、并发占用或需要选择知识库；核对原请求 |
| 413 / 422 | 请求过大或字段错误 |
| 429 | 当前进程用户预算或并发容量已满 |
| 502 | 上游响应、业务记录或模型格式不符合契约 |
| 503 | 报告/智能助手未开启，或依赖不可用/被限流 |
| 504 | 单次网络读取超时或整个请求超时 |

默认每用户（租户+用户）每分钟 30 次，每进程同时执行 16 个请求；问答、报告、智能助手和历史读取共用该预算。未开始的超限请求不会发起模型调用。默认 HTTP 上游读取时限 110 秒、整个业务执行时限 120 秒，读取请求体另限 5 秒；整个执行预算使用可取消异步等待，所有上游请求使用异步 HTTP。取消并不能保证远端已开始的模型计算不计费。RAG 问答发生网络超时后应复用同一个幂等 key 和原请求体查询/重试，不生成新 key 重复提交。

当前限流是单进程保护；多实例上线需要网关共享用户配额、租户日/月模型预算和 IP 限流，不能把进程内计数当成分布式总额度。网关请求总时限应大于 120 秒，并配置正文大小和上传速率限制。

## 验证与尚需外部完成的工作

运行 `python -m unittest discover -s tests -v`。测试使用内存 HTTP mocks 和临时身份文件，覆盖匿名拒绝、身份伪造、越权库、业务身份错配、异常脱敏、重定向拒绝、无记录、无效模型格式、输出筛选、体积限制、用户限流、并发上限及总时限取消，不调用收费模型。新增 `test_langchain_service.py` 实际执行 LangChain 链和工具，验证非法/多工具拒绝、缺失月份澄清、原始问题转发、事实不被二次改写和同一总预算取消。

真实 PostgreSQL 跨服务与进程启动验证另见 [改造验证记录](docs/verification.md)。Agent 测试现有 47 项（24 项基础单元/HTTP 测试、15 项 LangChain 测试、7 项跨服务集成、1 项进程启动）。集成测试必须使用独立、可丢弃的 PostgreSQL 实例和名为 `rag_test` 的数据库，同时显式设置 `RAG_TEST_ALLOW_RESET=1`；测试会清空数据并配置实例级 `rag_app` 角色，不能连接共享生产实例。两项目测试应顺序运行。

该实现提供可部署的服务与契约，不代表已经接通用户真实业务 API、企业 SSO、数据合规审批、备份恢复演练或生产容量验证。上线前要用真实供应商接口验证模型兼容性、评估建议质量，完成业务 API 联调与企业网关配置；联合 RAG 的数据迁移和恢复测试另见其文档。

可选 Streamlit 调试界面：安装 `requirements-ui.txt`，设置 `AGENT_API_BASE_URL` 后 `streamlit run app.py`。界面只调用 API，不直接访问数据库或模型；浏览器刷新后的正式历史应通过会话 API 获取。该界面不是企业 SSO 门户。
