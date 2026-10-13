# LangChain 改造验证记录

验证日期：2026-10-13。本次两个项目共 **95 项测试全部通过，数据库和进程用例没有跳过**。测试覆盖改造后的真实 LangChain 执行流程和隔离 PostgreSQL 数据存储；不代表已完成企业生产上线验收。

## 本次交付与验证范围

- RAG 使用 LangChain `Document`、递归分块器、`Embeddings`、`BaseRetriever`、`ChatPromptTemplate` 和 LCEL 问答链；数据库继续管理权限、任务租约、版本发布、引用、会话及幂等。旧固定字符索引和待处理任务在兼容配置范围内继续工作，新文档使用递归分块。
- Agent 使用 LangChain 异步模型适配器与严格输出解析生成报告建议；新增 `/v1/assistant` 通过原生模型工具调用选择一个 `StructuredTool`。知识问答沿用原始问题与可信会话，报告仅能读取当前认证用户指定月份的数据，工具结果不经过模型二次改写。
- 原有身份验证、知识库授权、请求和响应大小限制、超时、并发、限流及错误脱敏仍生效。报告和智能助手默认关闭，分别通过 `REPORTS_ENABLED`、`ASSISTANT_ENABLED` 显式启用。
- 核心链调用显式关闭外部 LangSmith tracing；模型与工具配置不包含可序列化的密钥字段。旧教学入口没有重新启用，原 Chroma、CSV 和历史文件不会自动导入新服务。

## 已通过的测试

| 项目 / 范围 | 数量 | 实际验证内容 |
| --- | ---: | --- |
| RAG 基础单元 | 9 | 权限、分块、引用、输入与配置约束 |
| RAG HTTP API | 5 | 认证、角色、上传与字段限制及接口语义 |
| RAG LangChain | 8 | 真实链执行、上下文和历史、检索权限隔离、截止时间、递归分块、旧任务兼容及配置序列化 |
| RAG PostgreSQL 集成 | 26 | 真实事务与行锁、版本发布、租约恢复、幂等、配额、删除/更新后失效、受限角色及新旧索引兼容 |
| **RAG 小计** | **48** | **全部通过** |
| Agent 基础单元 / HTTP | 24 | 认证与身份伪造、业务记录归属、默认关闭、超时取消、并发、限流、输出筛选及异常脱敏 |
| Agent LangChain | 15 | 真实报告链与工具调用、原问题转发、非法/多工具拒绝、月份澄清、严格 JSON、事实直接返回及共享预算取消 |
| 两服务集成 | 7 | 真实 RAG API 和 PostgreSQL 的上传、索引、问答、会话、重试、跨租户拒绝与删除撤权；Agent 工具入口的知识与报告路径 |
| 实际进程启动 | 1 | 启动两个 Uvicorn 服务和一个索引 worker，使用普通数据库账号，通过本机真实 HTTP 验证存活、就绪、认证、禁用报告及输入拒绝 |
| **Agent 小计** | **47** | **全部通过** |
| **总计** | **95** | **无跳过用例** |

两项目顺序使用本次临时创建的独立 PostgreSQL 17 / pgvector 0.8.1 容器，数据库名为 `rag_test`。跨服务测试通过 ASGI 传输连接真实服务代码并访问真实数据库；进程测试另外启动真实服务进程和 worker，验证 TCP 连接与受限数据库身份。

模型回答、Embedding 向量和业务使用记录均为合成数据。LangChain 编排、工具参数校验、HTTP 契约处理及数据库行为实际执行；本次未向真实模型供应商或企业业务 API 发出计费或业务请求。模拟输出通过并不等于模型在真实问题上一定正确选择工具、正确回答或生成合格建议。

验证使用 Windows / Python 3.10.11，随后在 RAG 项目的 `.langchain-venv` 中完成不继承全局包的依赖安装和复验。核心版本为 `langchain-core==1.6.3`、`langchain-text-splitters==1.1.2`、`langsmith==0.14.8`，`pip check` 未发现依赖冲突。`requirements.txt` 自动引用 `constraints.txt`，固定本次验证使用的间接依赖。干净环境复验发现 Windows 测试子进程退出后的日志文件句柄释放存在短暂竞态，已在进程测试中增加有上限的精确文件清理重试。

三个 Compose 文件通过静态配置验证；Agent 单服务配置用临时目录中的 `.env.example` 副本验证，未创建正式 `.env`。Dockerfile 目标为 Python 3.11；**本次没有重新构建应用容器镜像，也没有完成 Compose 全套应用容器启动验证**。

## 重复执行测试

安装两项目当前 requirements，使用独立 Python 环境，避免全局依赖冲突。测试基于标准库 unittest。

不访问数据库的测试：

```text
# RAG 项目目录
python -m unittest discover -s tests -p test_unit.py -v
python -m unittest discover -s tests -p test_api.py -v
python -m unittest discover -s tests -p test_langchain.py -v

# Agent 项目目录
python -m unittest discover -s tests -p test_service.py -v
python -m unittest discover -s tests -p test_langchain_service.py -v
```

完整测试需要独立、可丢弃的 PostgreSQL 实例，已安装 pgvector，数据库名必须为 `rag_test`。测试连接账号需要初始化 schema 和测试角色的权限。通过受控方式设置 `RAG_TEST_DATABASE_URL` 和 `RAG_TEST_ALLOW_RESET=1`；不要把真实口令写入脚本、仓库或命令历史。

**测试会清空业务表，并修改实例级 `rag_app` 角色的测试口令与权限。必须使用独立实例，不能只在共享生产实例中另建一个测试数据库。** 两项目完整测试必须顺序执行，避免相互重置数据。

```text
# RAG 项目目录：48 项
python -m unittest discover -s tests -v

# Agent 项目目录：47 项
python -m unittest discover -s tests -v
```

未设置测试地址或没有显式允许重置时，数据库和进程集成用例会跳过，这种运行不能记为完整验证。Agent 默认通过同级 `rag-service` 导入 RAG 服务；目录不同时设置 `RAG_PROJECT_PATH`。

## 尚未完成的企业环境验收

1. 应用容器镜像构建、Python 3.11 容器内运行和 Compose 全套启动尚未完成本次复验。需要在企业可达的镜像源构建，并使用实际 secret 挂载和网关配置验证。
2. 真实模型供应商的聊天、Embedding、原生 `tools` 兼容性未验收。还需校准向量维度与检索阈值，评估工具选择、引用正确性、无答案拒答、报告建议质量、延迟和费用。当前没有正式 RAG 效果评测指标或用户反馈数据。
3. 真实企业使用记录接口未完成联调。月报默认关闭，必须接通 README 定义的业务接口并验证身份及月份归属后再启用；智能助手也需要配置支持原生工具调用的模型。助手整体没有持久化幂等缓存，重试可能重复消耗路由和报告模型额度。
4. 身份仍为服务端静态 Bearer 摘要映射，尚未对接企业 SSO/OIDC。TLS 入口、共享配额、正式审计、监控告警、数据保留和备份恢复需在企业环境配置并验收。
5. 文档上传仅支持 UTF-8 TXT，PDF 需要可信转换流程。接口返回完整 JSON，无 SSE/token 流式输出；多轮检索未实现完整查询改写，混合检索和 reranker 尚未实现。
6. Compose 使用单节点 PostgreSQL 与精确向量扫描；没有生产规模数据量和并发压测结果，不承诺特定 QPS、高可用或灾备指标。Agent 的并发与用户预算仍是进程内保护，多实例必须配置共享限流。

文件备份位于两个项目各自的 `.backups/` 目录，备份清单包含逐文件校验记录；项目文件副本不替代外部 PostgreSQL 备份。部署和联调步骤见 [部署说明](deployment.md)，RAG 旧索引兼容及升级要求以知识库项目 README 为准。
