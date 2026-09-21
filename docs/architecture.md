# 公开架构说明

## 分层

```text
学生工作台
  -> StudentResponse projection
  -> Runtime Harness
  -> Agent Loop / Provider Gateway
  -> Read-only Tool Registry
  -> RAG / Knowledge + Deterministic Workflow
  -> SQLite facts, events and traces
  -> Evaluation / Governance read projections
```

### RAG / Knowledge

RAG 的输入不是任意网页或原始文件，而是经过本地登记、脱敏/重写、结构化、审核和发布的知识版本。检索先做发布状态、适用范围、可见性、时效和冲突过滤，再结合 FTS5 关键词与可选 Dense 召回。候选结果要绑定 Evidence 和 Claim，最后由 Evidence Gate 阻止无依据结论进入学生投影。

索引与 Publication、Embedding Profile 绑定。更换模型或版本必须重建并复核索引；缺少 Dense 依赖时可以退回关键词路径，但健康状态应标记降级。

### Agent Runtime

Runtime 负责回合预算、上下文整理、截止时间、取消、有限重试、恢复、Trace 脱敏和唯一终态。Agent 只可以选择服务端注册的只读工具，不能直接访问网络、文件系统、浏览器或真实业务写入。

### Provider Gateway

Gateway 把 Responses 主协议和 Chat Completions 备用协议转换为内部模型契约。普通文本、结构化输出、工具调用、SSE 增量、错误和取消都要经过能力声明与事件归一化。已经产生语义事件后不能为了“重试”静默换协议重放，避免重复副作用。

### Workflow

确定性 Workflow 负责字段、材料、条件规则、预检、预览、确认、状态变化和模拟数据库写入。预览是只读的，明确确认才允许推进模拟状态。公告/依据版本变化时，未完成任务进入重新确认，而不是静默覆盖旧依据。

### Evaluation

评测按召回、过程、结果和端到端四层组织。固定案例、运行快照、Trace、Bad Case、回归和发布门共同形成质量闭环。评测数据本身也必须是公开安全的虚拟数据。

## 前端与伴随形象

前端把工程状态投影成业务语言：依据、缺口、下一步、处理中、失败和恢复。桌面、移动和门户嵌入模拟共享同一事实状态。庆小通是可替换的资产适配器，只提供可理解的状态提示，不拥有权限、长期记忆或任务控制权。

## 未来适配器

真实门户、统一身份、企业微信和业务系统只能在获得明确授权后作为独立适配器设计。身份、权限、写入、审批、回执、审计、幂等和回滚都必须由目标系统与责任方确认；当前代码没有这些真实连接。
