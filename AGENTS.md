# AGENTS.md

面向 AI 编码代理的入口指引。本仓库的 agent 指南以 **[CLAUDE.md](CLAUDE.md)** 为单一事实源——项目结构、开发流程、构建/测试命令、代码规范、配置全表、排错速查都在那里，**改文档只改 CLAUDE.md**。

## 补充文档

- 领域语言（术语表）：[CONTEXT.md](CONTEXT.md)
- 架构决策记录：[docs/adr/](docs/adr/)
- 详细参考：[docs/](docs/)（architecture / api-reference / elasticsearch / deploy）

代理池、出网链路、与 proxy-hub 的交互细节见 **CLAUDE.md §部署架构（代理出网链路）** 与 **§常见错误与排查**；proxy-hub 自身机制以[其仓库](https://github.com/404-error-notfound/Prism-proxy) README 为准。

> 早期 AGENTS.md 与 CLAUDE.md 内容重复导致文档漂移，已收为指针。
