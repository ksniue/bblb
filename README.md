# 企业知识库 RAG 与 Agent 助手

本仓库包含两个协同工作的 Python 服务，展示从知识导入、权限控制、检索问答到 Agent 工具调用的完整实现。运行时代码、测试和部署说明均在仓库中；真实业务数据和演示语料不随仓库发布。

## 项目结构

| 目录 | 职责 | 入口 |
| --- | --- | --- |
| [`rag-service`](rag-service/README.md) | FastAPI 知识库 API、文档版本与后台索引、PostgreSQL + pgvector 检索、引用问答 | `enterprise.api:app`、`enterprise.worker` |
| [`agent-service`](agent-service/README.md) | 认证入口、RAG 请求转发、个人报告链与可选的受控工具调用 | `service.main:app` |

联合部署要求两个目录保持当前名称和同级关系，完整步骤见 [部署说明](agent-service/docs/deployment.md)。Agent 默认关闭报告和助手工具；启用报告需要自行配置真实业务 API，启用工具路由需要支持原生工具调用的模型。

## 如何检查

- 从 [RAG 服务](rag-service/enterprise/api.py)、[Agent 服务](agent-service/service/main.py) 和各自的 `service` / `enterprise` 模块检查请求边界、授权、检索、幂等及错误处理。
- 测试代码位于两个项目各自的 `tests/`。单元、API 和 LangChain 测试可独立运行；完整数据库集成测试需要一个可丢弃的 PostgreSQL + pgvector 测试实例。测试可能清空测试表并修改实例级角色，要求和命令见 [验证记录](agent-service/docs/verification.md)。
- 验证记录载明 2026-10-13 的 95 项测试结果，同时列出了未完成项：Python 3.11 容器镜像及 Compose 全栈启动、真实模型与企业业务 API 联调、生产 TLS / SSO / 监控和恢复验收。测试通过不代表这些项目已经上线验收。
- 生产服务需要 Docker、数据库、密钥和外部 HTTPS 入口；GitHub 仓库本身不提供在线服务。两项服务的端口默认只绑定本机回环地址。

本仓库未附加开源许可证，公开用于项目展示和审阅；第三方依赖仍各自遵循其许可证。
