# 联合部署、联调与运维交接

## 拓扑及目录要求

两个项目保持同级目录：`rag-service/` 与 `agent-service/`。在 `agent-service/` 中使用 `compose.integrated.yaml`，它通过 `../rag-service` 构建知识服务。

```
企业 TLS 网关 -> Agent :8001 -> RAG :8000 -> PostgreSQL + pgvector
                    |             |               ^
                    |             +-> 模型 API     |
                    |                       RAG worker
                    +-> 真实业务使用记录 API
                    +-> 模型 API（月报建议 / 可选工具路由）
```

PostgreSQL 只加入内部 `data` 网络，没有宿主机端口；RAG API 与 worker 同时加入允许模型出站的 `services` 网络。Agent 只加入 `services`，使用固定主机名 `rag`。RAG 和 Agent 的 HTTP 端口只绑定 `127.0.0.1`，不会因这个示例自动公开到局域网或公网。需要外部服务时在网关终止 TLS、限制请求大小、加共享配额并配置身份来源。

镜像以 UID 10001 执行应用，根文件系统只读，临时目录使用有大小限制的 tmpfs。Compose 把模型密钥、业务密钥与身份映射按文件挂载；Agent 无权读取数据库口令或管理员原始凭证文件。数据库管理员 DSN 只给一次性迁移容器；RAG API/worker 使用 `rag_app` 普通角色，只授予 schema 使用权限和表 SELECT/INSERT/UPDATE/DELETE、sequence 使用权限，无建库、建角色、复制或 superuser 权限。公共 schema 对 PUBLIC 的 CREATE 权限会撤销。这里只创建单节点持久化数据库，不构成高可用数据库集群。

## 准备并启动

1. 安装并启动 Docker Engine / Docker Desktop（Linux containers），确保可以拉取基础镜像。默认数据库镜像为 `pgvector/pgvector:0.8.1-pg17`，持久化路径为 `/var/lib/postgresql/data`；`PYTHON_IMAGE`、`PGVECTOR_IMAGE` 可改为企业镜像仓库中的对应镜像/digest。数据库主版本变更必须执行 PostgreSQL 支持的数据升级或备份恢复流程，不能直接把 PG16 数据卷挂到 PG17。
2. 将 `.env.example` 复制为 `.env`；配置真实模型地址和模型名称。模型 `MODEL_BASE_URL` 是包含 `/v1` 的地址前缀，不是完整的 `/chat/completions` URL。问答模型使用 `CHAT_MODEL`，报告和 Agent 路由使用 `MODEL_NAME`。`ASSISTANT_ENABLED` 默认 false；启用时确认供应商支持原生 tool_calls，报告独立受 `REPORTS_ENABLED` 控制。按供应商实际返回的向量长度设置 `EMBEDDING_DIMENSION`，既有索引不允许随意更改模型或维度。
3. 运行交互式工具。它不会打印凭证，也不会覆盖已有文件：

   ```text
   python tools/prepare_secrets.py --tenant your-tenant --user your-admin --kb manuals
   ```

   输入真实模型 API key。暂未对接业务系统时保留 `REPORTS_ENABLED=false`，空的 usage token 文件是停用报告的明确状态，不是可用凭证。如需立即启用报告，运行工具时加 `--reports` 并配置真实 `USAGE_API_BASE_URL`、`REPORTS_ENABLED=true`。工具仅是单机部署辅助；企业环境应使用现有密钥管理系统和只读 secret 挂载。
4. Windows 用目录 ACL 限制 `secrets/` 的读写范围。Linux 工具创建 `0700` 私有目录，供容器单文件挂载的文件设为 `0644`，原始 `client_token` 为 `0600`。其他宿主机用户无法进入私有目录；容器只能读取明确挂载的文件，不能读取 `client_token`。不要把目录改为全员可访问；已有目录权限不安全时工具拒绝运行。生产编排平台优先使用其 secret 权限机制。原始 `client_token` 是初始化管理员凭证，只留给部署管理员。
5. 执行：

   ```text
   docker compose -f compose.integrated.yaml config --quiet
   docker compose -f compose.integrated.yaml up --build -d
   docker compose -f compose.integrated.yaml ps
   ```

   启动顺序为 PostgreSQL 可用 → 一次性普通应用角色配置、迁移及授权成功 → RAG API 与 worker → RAG 就绪检查通过 → Agent。迁移容器执行 RAG 的 `python -m enterprise.bootstrap`，管理员 DSN 不会进入常驻应用容器。数据库数据保存在命名卷 `rag-data`。不要用 `down -v` 做常规升级或停止。
6. 检查 `http://127.0.0.1:8001/health/live`，再携带部署管理员凭证检查 `/health/ready`。ready 只检查本地配置和 RAG 数据库/索引契约，不会发起收费探测；必须另做下一节的真实业务联调。

若只部署 `compose.yaml`，需修改 `.env` 中的 RAG URL 为独立部署且容器能够访问的地址。两个独立 Compose 默认网络互不相通，因此不能原样使用 `http://rag:8000`。联合 Compose 已解决该问题。

## 联调顺序

1. 在 RAG 服务通过 authenticated 文档 API 导入一份允许范围内的 UTF-8 TXT，等待 worker 的入库任务完成。当前生产导入不接受 PDF；旧教学 PDF 需在受控离线流程转换并核对文本后再入库。接口以 RAG 项目的 README 为准。
2. 用相同身份文件中的 `reader` 凭证请求 Agent `/v1/chat`。第一次请求传指定知识库和唯一幂等键，第二轮带回 `conversation_id`，确认答案引用及会话历史只含本人可访问内容。
3. 配置真实使用记录 API，按照 Agent README 的契约返回受认证用户的记录。使用仅 `{"month":"YYYY-MM"}` 的报告请求验证当前用户查询、无记录、跨用户错误响应和超时。跨用户错配必须失败，不能降级为别人的数据。
4. 可选启用 `ASSISTANT_ENABLED=true`，通过 `/v1/assistant` 验证知识问题路由、明确月份的本人月报、缺少月份澄清、关闭报告后不可选月报工具。使用相同身份和幂等请求头；该接口不提供整个助手请求的持久化幂等保证。
5. 在测试租户中测量模型格式稳定性、工具选择正确率、问答引用准确性、维护建议质量、实际延迟和单次成本。容量和数据量以企业真实场景为准；离线 mock 测试不能替代这些检查。
6. 准备低权限用户凭证，确保 tenant/user/KB 错配、旧会话猜测、额外 `user_id` 参数均被拒绝，再开放给试用用户。

生产外部系统地址和凭证没有包含在仓库里，也未替用户执行模型计费请求。未完成上述联调前，不应把本地成功启动宣称为业务正式上线。

## 超时、限流与监控

Agent 默认整个执行时限 120 秒、上游读取时限 110 秒，RAG 自己管理模型调用与检索预算。调试 UI 等待 125 秒。网关时限需覆盖正常服务时限，但应限制慢请求正文。模型超时后不自动重试，避免不受控重复计费；知识问答重试必须使用相同幂等键和完全相同的原始 JSON。月报无持久化幂等缓存，重复提交可能再次计费。

每进程并发和用户每分钟预算用于保护单实例。多个实例或多 worker 必须增加网关/共享存储的全局配额，本 Compose 故意只启动单 worker。监控应聚合请求 ID、状态码、耗时、429/502/503/504 比例、RAG 入库失败与队列积压；不能把原始问题、个人报告、模型 prompt 或 Authorization 导出到日志平台。Docker 日志配置限制单容器本地留存容量。

## 备份、升级与轮换

- PostgreSQL 是知识、版本、任务和会话的持久化权威来源，按企业数据库策略做加密备份并定期恢复演练。单个 Docker 命名卷不是备份。使用受控管理终端执行数据库备份，不把口令写在命令行参数或 shell 历史里。
- 升级前备份数据库，在测试库验证迁移，再更新镜像。若变更 schema，镜像回滚不保证数据回滚兼容；必须先验证恢复方案。示例使用镜像标签，生产发布应记录并固定实际拉取的镜像 digest。
- 身份文件改动后重启 RAG、worker 和 Agent，使两服务使用同一映射；禁用或轮换用户凭证时撤销旧摘要。数据库口令文件更改不会自动修改既有 PostgreSQL 用户口令，必须按数据库轮换流程协调 SQL 变更与所有服务重启。
- 根据企业记录保留政策实施会话和知识删除，验证备份的到期清理。当前服务不自动清理旧教学目录内的日志或数据库；它们未进入生产容器。
