# 基于 LangChain 的企业知识库服务

本项目使用 FastAPI 提供认证 API，LangChain 负责文档分块、向量模型接口、检索器与问答链，PostgreSQL + pgvector 负责文档版本、任务队列、会话与向量存储。目标是企业内部、受控规模的部署基线。真实模型凭据、公司权限名单、TLS 入口、备份与业务质量验收仍由部署方配置。

## LangChain 实现

- `enterprise/ingestion.py`：使用 `Document` 与 `RecursiveCharacterTextSplitter`，按段落、中文标点、空格、字符逐级分块，默认最大 1000 字符、目标重叠 100 字符。重叠量受自然分隔符影响，不保证每块恰好重叠 100 字符。
- `enterprise/llm_adapters.py`：实现 `Embeddings` 与 `BaseChatModel`，复用已有 HTTP 客户端的响应上限、维度检查、截止时间和错误脱敏；不额外叠加 SDK 重试，不使用共享模型缓存。
- `enterprise/retrievers.py`：请求独立的 `BaseRetriever`，将可信身份绑定到 SQL 检索；数据库先过滤租户、知识库、删除状态和当前版本。
- `enterprise/chains.py`：实际执行 LCEL 的检索、上下文裁剪、`ChatPromptTemplate`、历史消息、模型和 `StrOutputParser`。无资料直接返回无依据提示，不调用聊天模型。
- `enterprise/service.py`：协调幂等请求、链执行、引用编号验证及数据库提交。模型调用不占数据库事务；完成时再次验证引用。

核心依赖是 `langchain-core==1.6.3`、`langchain-text-splitters==1.1.2`，无需安装包含额外 Agent 功能的 `langchain` 顶层包。主流程显式关闭外部 LangSmith tracing，环境变量不会隐式上传企业原文。现有服务日志继续只记录请求 ID、状态和耗时。

`requirements.txt` 自动引用 `constraints.txt` 固定本次验证使用的间接依赖。从仓库克隆后，可使用 Python 3.10 或更新版本创建本地环境；Windows 示例：

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

可选 Streamlit 页面需另安装 `requirements-ui.txt`。两个项目的验证范围与历史结果见同一仓库中 Agent 项目的 `docs/verification.md`。

## 从改造前版本升级

本机迁移副本包含本地配置和数据，不属于公开仓库内容；新克隆不会包含该副本。文件副本也不等于外部 PostgreSQL 数据库备份。

1. 停止旧 API 和 worker，安装新 `requirements.txt` 或重新构建镜像；使用迁移凭据运行原有 bootstrap/数据库初始化，再启动新版 API 和 worker。
2. 同一模型、维度和分块大小下，旧 `chars-…:v1` 索引继续可读，新文档使用 `recursive-…:v2`。旧队列任务仍按原固定字符策略完成，旧上传幂等键仍关联原版本。
3. 需要把已有文档转为递归分块时，使用原 `document_id`、原文和新 `Idempotency-Key` 上传。新版本发布前旧版本继续服务，发布成功后切换；无需停用所有旧文档。无需为了使用 LangChain 立即重建全部旧索引。
4. 兼容范围仅限上述两种分块策略；模型、维度或分块大小变化仍需显式迁移到新数据库。升级后不能把旧服务与新版 worker 混跑。回退代码前，应同时恢复匹配的数据库备份，或完成专门的数据回迁。

旧的 `chroma_db/`、`chat_histories/`、`md5.text` 保留原位，生产入口不会读取、修改或自动导入。重新审核原文及归属后，通过认证上传接口重新建立索引。

## 当前行为

- 用户身份来自只读 secret 文件中的高熵 Bearer 令牌哈希。请求不能指定租户或用户。角色和知识库白名单均在后端检查；`admin` 也不能越过知识库范围。
- 会话绑定租户、用户和知识库。PostgreSQL 行锁、请求租约及幂等键控制并发；成功后在一个短事务中保存一整轮消息，模型网络调用不占数据库事务。
- UTF-8 TXT 上传创建持久化任务；worker 最多自动尝试 3 次，每批续租。进程退出后任务在租约过期后接续；最后一次崩溃也会变为失败而不会永久占用文档。
- 文档、版本、chunk 和向量都在同一 PostgreSQL 内。全部向量写入与当前版本发布在一个事务中完成；建新版本期间旧版继续服务。权限和当前版本条件在 SQL 检索阶段生效，生成完成时再次校验引用。删除立即撤销可见性并移除原文和向量；旧消息中的失效引用内容在读取时被隐藏。
- 真实引用编号由服务端校验，越界或没有引用的模型答案降级为拒答；只返回答案实际引用的片段。编号有效不等于内容已经证实，仍需要业务评测。
- 网络模型调用单次最多 45 秒；整个生成阶段有 90 秒预算。SQL 单条 15 秒、锁等待 3 秒、连接 5 秒上限。整体 HTTP 不保证恰好在 90 秒内结束；上游代理建议 120 秒，超时后按相同幂等键和原始请求重试。
- 每个租户/用户合计每分钟 30 次认证请求（数据库跨 API 实例共享），问题最多 4000 字符，上传最多 5 MiB / 30 万字符 / 400 块，上下文最多 14000 字符，历史最多 12000 字符、最近 30 条，模型输出最多 1000 token。字符预算是保守资源上限，不是精确 tokenizer 计数。
- **首版只支持 UTF-8 TXT。PDF 明确返回 422**，需先由可信流程转换成 TXT。PDF 的解析 CPU/内存隔离尚未实现，不能靠文件大小限制替代。
- HTTP API 返回完整 JSON，不提供 token 流式输出。Streamlit 文件是可选认证 API 客户端，提交操作明确触发，不直接连数据库或模型。

## 本机或单机 Docker 部署

1. 安装 Docker Compose，确认可以访问模型提供商的 HTTPS 地址。在项目目录执行 `python tools/create_secrets.py`，按提示输入真实 API key。该工具不调用模型，生成独立数据库管理/运行凭据和随机访问令牌，绝不输出密钥；已有 secrets 不会被覆盖。
2. 将 `.env.example` 复制为 `.env`，按实际服务填写模型和维度。默认兼容 DashScope 的 OpenAI 协议端点。模型 ID 和维度必须由账号实际支持；修改 embedding 模型、维度、切块签名需显式重建到新数据库，现有库不自动混用。
3. 审核 `secrets/principals.json` 中的身份及知识库范围。生成的初始用户仅用于建立本次安装，正式人员应各分配独立随机令牌。安全读取 `secrets/access_token.txt` 分发给初始用户；该明文令牌不会挂载到服务中。
4. 执行 `docker compose up --build -d`。迁移任务使用管理凭据创建 extension/表和 `rag_app` 普通角色；API 与 worker 仅挂载普通角色 DSN，没有建表、建角色或 superuser 权限。API 只绑定宿主 `127.0.0.1:8000`，通过企业 HTTPS 网关发布，网关应限制连接和请求体，且不得记录 Authorization 或完整请求内容。
5. 检查 `docker compose ps`、`docker compose logs migrate` 和 `GET http://127.0.0.1:8000/health/ready`。健康检查不调用收费模型；ready 只确认数据库及索引签名。需另行监控 worker 存活和版本任务的等待时间，避免健康的 API 掩盖停止的 worker。

所有 secret 使用文件挂载。可使用 `DATABASE_URL` / `MODEL_API_KEY` 环境变量替代其 `_FILE` 配置，但同一项两者同时配置将拒绝启动。生产应优先使用外部 secret 管理系统；Compose 本机文件 secrets 不等于独立加密的凭据库。POSIX 上 secrets 目录应为 0700，挂载文件需让容器 UID 10001 可读；Windows 上使用相应 ACL。不要提交或发送 secrets、`.env` 和数据库备份。

独立连接现有 PostgreSQL 时，先由 DBA 安装 pgvector，使用迁移身份执行 `python -m enterprise.db`；授予 API/worker 所需表的 SELECT/INSERT/UPDATE/DELETE 和 sequence USAGE/SELECT，运行身份不得有 schema CREATE、角色管理或 superuser 权限。启动 API：`uvicorn enterprise.api:app --host 127.0.0.1 --port 8000 --workers 2 --no-access-log`；另启动 `python -m enterprise.worker`。依赖通过 `python -m pip install -r requirements.txt` 安装。

可选 UI：安装 `requirements-ui.txt`，设置固定的 `RAG_API_BASE_URL`，运行 `streamlit run app_qa.py` 或 `streamlit run app_file_uploader.py`。非本机地址要求 HTTPS；正式页面同样置于公司认证/TLS 网关后。页面用户输入服务访问令牌，退出会清除该页面会话凭据。

## API 协议

所有 `/v1/` 接口需要 `Authorization: Bearer <个人令牌>`。JSON 问答与 multipart 上传同时需要 `Idempotency-Key`，格式为 8–128 个字母、数字、`.`、`_`、`:` 或 `-`；重试必须使用原始键及相同请求。对同一用户复用键但改变内容返回 409。

| 方法与路径 | 请求或行为 |
| --- | --- |
| `POST /v1/chat` | JSON `{question, conversation_id?, knowledge_base_id?}`。新会话有多个授权知识库时必须选定知识库；已有会话绑定知识库。 |
| `GET /v1/conversations/{id}/messages` | 返回本人会话最近 200 条，时间正序；外来会话 404。 |
| `POST /v1/documents` | multipart：`knowledge_base_id`、`file`、可选 `document_id`。返回 202 和 `document_id/version_id/state`，此时只是接收任务，不表示可检索。 |
| `GET /v1/versions/{id}` | 查看 `pending/processing/ready/failed/superseded` 与脱敏失败码。 |
| `GET /v1/knowledge-bases/{id}/documents` | 可见文档最近 200 条。 |
| `DELETE /v1/documents/{id}` | editor/admin，成功 204，立即撤销文档可见性。 |
| `GET /health/live`、`GET /health/ready` | 无鉴权的最小存活/就绪状态，不返回配置或凭据。 |

聊天响应固定为 `{answer,citations,conversation_id,request_id}`。每个 citation 为 `{document_id,version_id,chunk_id,filename,chunk_index}`；答案中 `[1]` 对应数组第一项。用户标识、租户、模型端点等额外字段会被拒绝。API 当前将具体业务异常映射为有限错误类别，用户可凭 `X-Request-ID` 联系运维，响应不会包含原文或 provider 异常。

上传新文档与修改文档是明确区分的操作：更新时传 `document_id`，新文件名不会自动覆盖旧文件。相同版本重试复用该版本；失败版本可重新提交同一内容重试。重传已被替换的旧版本返回 409，首版不提供隐式回滚。删除知识不会删除用户自己输入的消息，也不承诺已显示在浏览器中的内容可被撤回；历史保留/删除需根据企业政策另行执行。

## 验证与运维

离线及条件集成测试：`python -m unittest discover -s tests -v`。`test_langchain.py` 实际运行 LangChain 链，覆盖上下文、权限、请求隔离、截止时间与旧任务分块兼容；模型输出为合成数据，不发起收费请求。未配置独立测试数据库时，数据库集成测试会明确跳过。

真实 pgvector 集成测试：必须使用 **独立可丢弃的 PostgreSQL 实例及名为 `rag_test` 的测试数据库**，设置 `RAG_TEST_DATABASE_URL` 并显式设置 `RAG_TEST_ALLOW_RESET=1`，再执行 `python -m unittest discover -s tests -p test_postgres_integration.py -v`。测试会清空所列业务表，并配置实例级 `rag_app` 角色；不得使用共享生产实例，即使数据库名是 `rag_test`。数据库名不符会拒绝执行，未设置测试地址或未允许重置时集成测试跳过，不能记为完整验证。集成测试仍使用假 embedding/聊天输出；两项目测试应顺序运行。

发布前以真实配置执行一组已批准的小规模连通性测试，再用企业题集评估引用正确性、无答案拒答、多轮追问、权限边界、延迟和费用。当前多轮检索仅拼接最近两个用户问题，不等于完整的多轮查询改写；混合检索和 reranker 未实现。检索阈值默认 0.45 是配置起点，需要按实际模型和题集校准。

数据库是唯一数据持久化点。使用 PostgreSQL 的 `pg_dump`/恢复或组织已有备份系统，同时保管模型/切块配置和密钥恢复流程；定期在隔离环境演练恢复并复核文档、当前版本、消息及权限。备份包括敏感原文和对话，须使用受控加密存储并限制访问。

这份 Compose 使用单台 PostgreSQL、单 worker 和精确向量扫描，尚不提供高可用、跨地域容灾、自动按租户计费或大规模向量 ANN 索引。大批量使用前应添加经压测验证的索引、任务配额/积压告警、数据保留清理任务和正式审计日志管道。静态哈希令牌为第一版可用认证机制，人员离职/撤权需要更新 principals 并重启 API；SSO/OIDC 接入需使用可信身份校验实现替换 Authenticator，不能直接信任前端请求头。
