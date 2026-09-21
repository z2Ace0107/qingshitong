# 庆事通：校园事务可信服务层

> 面向校园服务场景的证据优先 Agent 工作台：把“我该怎么办”整理成依据、条件、材料和可执行的下一步。

庆事通不是在校园门户旁边再放一个聊天框。它面向的是校园服务中最费时间、最容易出错的一段：信息分散在公告、手册、规则、表单和多个入口里，用户知道“想做什么”，却不确定“应该办理什么、依据是什么、材料是否齐、下一步是什么”。

庆事通用版本化知识、证据链、确定性 Workflow 和受控 Agent Runtime，把“找到答案”推进到“按边界完成正确的下一步”。当前基线围绕 A1 活动场地申请构建深度业务主线，形成可运行、可回放、可验证的本地交付；后续目标是沿同一架构逐步扩展资料、业务事项和服务入口。

项目以高校校园服务为应用背景，重点研究信息分散、规则理解、材料准备和任务跟踪如何被组织成可核验的服务流程。

庆事通不是“接一个模型、加一个聊天框”就完成的校园 Agent。它要证明的是：在一个低风险、可回放的连续任务中，系统能否把用户目标整理成适用事项，把已发布依据转换成可核验证据，把材料准备变成确定性预检，并在用户明确确认后推进可恢复的模拟状态。

[项目上下文](CONTEXT.md) · [产品简报](PRODUCT.md) · [架构说明](docs/architecture.md) · [本地验证](docs/verification.md) · [贡献指南](CONTRIBUTING.md)

## 一眼看懂

| 能力 | 庆事通的做法 | 直接价值 |
|---|---|---|
| 资料可信 | 来源登记、版本化 Publication、Claim-to-Evidence、时效与冲突边界 | 用户知道答案依据什么，资料变化不会静默覆盖已确认事项 |
| RAG | 元数据前置过滤 + FTS5/Dense 混合召回 + 关系扩展 + Evidence Gate | 检索不止返回相似文本，还要满足适用范围、时效和证据支持 |
| Agent | 有界 Agent Loop、服务端 Tool Registry、结构化 AnswerDraft | 模型负责理解和表达，事实、权限、规则和副作用由程序掌控 |
| 业务闭环 | 事项识别 -> 材料准备 -> 预检 -> 预览 -> 确认 -> 模拟状态 | 从“问到了”走向“准备好了下一步” |
| 质量治理 | Case -> Run -> Evaluation -> Bad Case -> Fix -> Regression | 错误能定位到召回、规则、工具、模型或交互，而不是只看一个总分 |
| 多 Provider | Responses 主协议、Chat Completions 显式备用协议、能力矩阵和方言适配 | 内部契约不被单一上游协议或中转服务写死 |
| 前端体验 | React/Vite 工作台、桌面/移动布局、流式状态、恢复和庆小通伴随形象 | 把工程复杂度投影成清楚的业务语言和下一步 |
| 可复核交付 | Docker Compose、Secret 文件、非 root、只读根文件系统、健康分层 | 开发者可以启动，评审者可以沿验证入口复核 |

## 解决什么问题

校园事务中常见的断点不是“没有一个聊天入口”，而是用户在连续任务中反复丢失上下文：

- 找不到正确事项：知道想做什么，却不知道应该进入哪个流程；
- 看不懂规则：看到了通知或手册，却不确定条款是否适用自己；
- 准备不完整：材料清单、字段要求和办理顺序分散在不同页面；
- 出错难追踪：回答看起来合理，却无法判断问题来自资料、召回、规则、流程还是模型；
- 状态不连续：已经准备过的信息无法安全地带到下一步，依据变化后也不知道是否需要重新确认。

庆事通把这些断点组织成一条服务路径：

~~~text
用户目标
  -> 事项识别与必要澄清
  -> 依据与适用范围
  -> 材料清单与缺口
  -> 确定性预检
  -> 预览与解释
  -> 明确确认
  -> 可恢复的业务演练
  -> 下一步与责任边界
~~~

普通公告浏览、列表筛选和固定表单仍然是更合适的工具。只有当用户需要跨来源理解、适用性判断、材料准备、连续状态或依据变化影响时，Agent 才有明确的增量价值。融合门户或企业微信未来可以作为入口，庆事通承担的是跨来源的信息组织、办事准备、状态连续性和质量治理；身份、权限、真实写入和审批仍属于授权业务系统。

## 为什么不是另一个聊天框

普通问答可以生成一段看起来合理的文字，但校园事务通常还需要判断适用对象、对应事项、时间窗口、材料缺口、责任方和下一步。庆事通把这些环节拆开：

```text
用户目标
  -> 事项识别与必要澄清
  -> RAG 召回已发布的依据
  -> Evidence Gate 检查结论是否有证据
  -> 事项卡与材料预检
  -> 预览
  -> 用户明确确认
  -> 虚拟状态流转
```

模型负责理解语言、组织候选和表达结果；事实、权限、字段校验、状态变化和副作用由程序控制。没有足够依据时，系统应澄清、降级或引导核验，而不是补写一条看似官方的规则。

## 当前可运行交付

当前公开基线围绕 A1 活动场地申请完成一条深度演示主线：

1. 用户用自然语言描述目标，系统把诉求整理为业务化事项卡。
2. RAG 从当前 Publication 中选择适用的虚拟依据、条件、材料和办理步骤。
3. 用户在结构化工作区补齐事项字段和材料内容。
4. 后端执行确定性的材料预检和规则判断，明确缺口、风险和下一步。
5. 预览与确认分离；预览不产生副作用，明确确认后才推进本地演练状态。
6. 任务支持补正、重新确认、依据变化后的重评估和刷新恢复。
7. 学生端展示业务状态；Trace、Publication、索引和评测字段留在受控治理投影。

当前种子数据还包含四类用于横向检验的演示场景：

- 活动场地申请：自然语言诉求、字段收集、材料预检、虚拟档期、预览、确认和补正；
- 选课公告与时间窗口：公开信息解释与个人结果回到官方系统核验的边界；
- 勤工助学岗位：岗位筛选、资格提示、模拟申请和虚拟发布方状态；
- 学生请假与销假：条件性材料、模拟责任方处理和状态闭环。

它们用于横向验证同一业务结构。所有状态变化只写入本地演示数据库，不能产生真实提交、审批或回执。

## 产品预览

本地启动后，读者可以沿着“发现事项 -> 准备材料 -> 预检 -> 预览 -> 确认 -> 查看状态”的路径体验产品：

- `/student`：学生事项服务入口，展示事项、依据、适用范围和下一步；
- `/student/tasks/:taskId/materials`：填写事项字段和结构化材料，运行确定性预检；
- `/student/tasks/:taskId/preview`：查看提交前的完整预览和风险边界；
- `/student/tasks/:taskId/status`：查看本地演练状态、补正和重新确认；
- `/public/summary`：项目说明与演示范围；
- `/governance`：读取脱敏的运行、评测和知识治理投影。

桌面、移动视图和未来的门户嵌入外壳共享同一套业务事实与任务状态，但不共享学校身份或真实业务权限。界面不会把 Provider、模型、索引分数或内部 Trace 直接暴露给学生。

## 三条核心链路

### 知识链：RAG 从资料到依据

```text
本地研究资料
  -> 授权登记与脱敏重写
  -> 虚拟 Fixture / Publication
  -> 结构化 Claim / Evidence / Relation
  -> 元数据硬过滤
  -> FTS5 关键词召回 + 可选 Dense 召回
  -> 混合排序与关系扩展
  -> Evidence Gate
  -> 学生可读事项卡
```

RAG 不只是把文本切片后交给向量模型。发布状态、适用范围、时效、冲突和来源版本先经过硬过滤；召回结果再绑定到 Claim 和 Evidence；证据不足或存在冲突时，回答必须显示边界。Dense Embedding 是可替换的 Profile，索引、Publication 和 Profile 必须整体绑定，不能静默混用。

### 执行链：Agent 在 Runtime 内工作

```text
用户意图
  -> Agent Loop
  -> Runtime 预算/超时/取消/恢复
  -> Provider Gateway
  -> 只读 Tool Registry
  -> Response Validator
  -> 确定性 Workflow
  -> 学生端业务投影
```

Runtime 限制回合数、工具数量、输入输出结构和可观察状态。Agent 没有任意网络、文件系统、浏览器或真实业务写入权限。材料预检、预览、确认和任务状态由确定性代码负责。

Provider Gateway 以 Responses 为主协议，以 Chat Completions 为显式备用协议，并把不同上游的文本、工具调用、结构化结果、流式事件、错误和取消归一化到内部契约。Provider Secret 只从服务端环境或仓库外的 Secret 文件读取。

### 质量链：评测驱动修复

```text
固定案例
  -> Run / Attempt / Trace
  -> 召回、过程、结果、端到端四层检查
  -> Bad Case 分类
  -> 知识/规则/契约修复
  -> Regression
  -> Release Gate
```

程序断言优先检查引用、权限、字段、状态机和模拟边界；语义 Judge 只能作为结构化辅助，不能替代来源审核。评测结果要区分 `pass`、`fail`、`error`、`skip` 和 `unscored`。

## 系统架构

~~~text
                         学生 Web / 移动工作台
                                   |
                         StudentResponse projection
                   事项卡 / 材料 / 流式进度 / 任务状态
                                   |
             +---------------------+---------------------+
             |                                           |
       Runtime Harness                              Delivery Harness
       - Run / Turn / Event                          - Docker Compose
       - 预算与上下文装配                             - Secret 文件边界
       - 取消、超时、恢复、幂等                       - 非 root / 只读根文件系统
       - Tool Registry 白名单                        - live / ready / degraded
       - Trace 与学生/治理投影                         - 数据卷与恢复检查
             |
       Agent Loop（有界回合）
       ModelTurn -> ToolCall -> ToolObservation -> AnswerDraft
            ^             |              |              |
            |             v              v              v
       IntentProposal   Router       Validator       唯一终态
             |
       Provider Gateway
       Responses primary | Chat fallback | SSE 事件归一化
       Provider Profile | 能力矩阵 | 方言 | 错误/重试/取消边界
             |
       +----------------------+----------------------+
       |                                             |
   RAG / Knowledge                               Workflow
   Source -> Redaction -> Publication             A1 预检 -> 预览 -> 确认
   FTS5 + Dense -> RRF -> Evidence Gate            -> 模拟审核/补正 -> 结束
   Claim -> Evidence -> Relation
             |                                             |
             +----------------------+----------------------+
                                    |
                         SQLite facts / events / traces
                                    |
                      Evaluation & Governance Harness
                Case / Run / Attempt / Judge / Bad Case / GateDecision
~~~

前端是 React + TypeScript + Vite 工作台，后端是 Python + FastAPI。模型不是事实数据库、权限中心或业务审批人：它可以理解自然语言、选择服务端白名单工具和生成结构化回答草稿；Runtime、RAG、Workflow、Validator 和未来的授权业务适配器分别掌握运行纪律、事实、状态、证据和副作用。庆小通是可替换的表现层伴随形象，只表达结构化状态，不读取个人数据、不调用工具、不改变任务状态。

## RAG：从资料到可核验答案

### 为什么不是“接一个向量模型”

校园资料的难点不只是相似度。公告有发布时间和有效期，手册有适用对象和条款层级，流程有材料、条件、责任方和入口。只把长文本切片后塞进向量库，会丢掉版本、权限、关系和适用范围。

RAG 的职责也不是把任何来源都塞进上下文。它必须先回答：

- 这条资料是否被登记、允许使用并处于可发布状态；
- 它适用于哪个角色、事项、时间窗口和渠道；
- 它与其他版本是否冲突或已经过期；
- 最终回答中的条件、材料和下一步是否真的能回到证据。

### 从来源到 Publication

公开或授权资料在进入检索前经过独立的资料治理阶段：

~~~text
本地研究资料
  -> 来源登记、授权状态、版本、定位和内容哈希
  -> 脱敏、重写、结构化切分
  -> Claim / Evidence / Relation 建模
  -> 冲突、时效和适用范围检查
  -> Publication 审核与发布门
  -> 可供检索消费的知识版本
~~~

### 知识分层和血缘

RAG 的知识不是一个无法解释的文本集合，而是从来源到消费版本的可追踪链路：

- 来源层保留授权、版本、定位和观察记录，作为后续审核与回放的依据；
- 事实层保存 Evidence、Claim、Relation 和业务实体，避免用聚合摘要替代原始证据位置；
- 消费层保存面向事项检索的聚合知识，便于按场景、角色、时效和发布状态过滤；
- Publication 负责把候选资料推进到可消费版本，索引必须与 Publication 和 Embedding Profile 整体绑定；
- 内容哈希、来源版本和更新时间让同一结论可以被回放和解释；
- 新资料不会无条件覆盖旧资料，冲突、过期和证据缺失会进入澄清、降级或人工核验。

### 复合检索和证据门

当前公开基线以 SQLite/FTS5 和结构化过滤提供确定性检索路径，并保留 Dense Profile、关系扩展和后续重排的接缝。检索流程遵守以下顺序：

1. 查询整理：把用户口语目标整理为事项、角色、时间和材料相关的检索意图；
2. 范围过滤：先按 Publication、场景、可见性、时效、状态和版本做硬过滤；
3. 多路召回：并行执行关键词/FTS5 和可选 Dense 召回，保留各路分数和来源；
4. 候选融合：用 RRF 或关系加权合并候选，避免单一相似度主导；
5. 证据装配：把候选转换为 Claim/Evidence 上下文，而不是直接拼接整篇文档；
6. 证据校验：检查事项、条件、材料和下一步是否有可定位依据；
7. 业务投影：只把学生需要的结论、缺口和核验边界投影到事项卡。

证据不足、来源冲突或内容过期时，系统应澄清、降级、拒答或引导核验，而不是补写一条看似官方的规则。

### Embedding Profile

Dense Embedding 是可替换的 Profile，不是 RAG 的全部。每个 Profile 必须固定模型版本、维度、归一化策略、输入限制、索引参数和运行依赖；Publication、索引和 Profile 必须整体绑定，不能静默混用。

公开代码登记两个可选的本地 Profile：

| Profile | 公共模型标识 | 向量维度 | 默认用途 |
|---|---|---:|---|
| `bge-base-zh-v1.5` | `BAAI/bge-base-zh-v1.5` | 768 | 默认的质量/成本平衡档 |
| `qwen3-embedding-0.6b` | `Qwen/Qwen3-Embedding-0.6B` | 1024 | 可选的质量验证档 |

Profile 名称、模型 revision、维度和 CPU 运行策略由代码契约固定。这个表说明的是公开实现的配置形状，不等于任何读者机器已经准备好模型文件；模型是否就绪必须由当前环境单独预检。

模型文件只在本地缓存或部署卷中准备，不提交到 Git，也不在服务启动时偷偷联网下载。没有 Dense 依赖时，系统仍可通过 FTS5 和结构化过滤运行，但健康状态必须明确标记为降级；不能把关键词路径伪装成 Dense 已通过。

显式准备入口（会下载固定 revision 的公开模型到本地缓存，不写入仓库）：

~~~powershell
python scripts/prepare_embeddings.py --profile bge-base-zh-v1.5 --cache-dir models/embeddings
python scripts/prepare_embeddings.py --profile qwen3-embedding-0.6b --cache-dir models/embeddings
~~~

只读预检入口：

~~~powershell
python scripts/embedding_preflight.py --profile bge-base-zh-v1.5 --cache-dir models/embeddings
python scripts/embedding_preflight.py --profile qwen3-embedding-0.6b --cache-dir models/embeddings
~~~

准备或切换 Profile 后，还要重新构建对应索引并验证查询召回、维度、归一化、健康状态和恢复行为。预检只说明当前机器具备或不具备该 Profile，不会把模型权重写入仓库，也不会把本机路径、缓存位置或密钥写入公开文档。

## Agent Loop、Runtime Harness 和 Provider Gateway

### Agent Loop：让模型只做需要模型的工作

庆事通采用有界的两阶段模型链路：

1. 第一阶段把用户诉求整理为 IntentProposal，只能选择服务端声明的只读检索工具；
2. Runtime 校验工具名称、Schema、参数、调用次数、预算和权限；
3. `search_service_items` 返回当前 Publication 的压缩观察结果；
4. 第二阶段生成结构化 AnswerDraft；
5. ResponseValidator 检查事项、Claim、Evidence、字段和学生投影；
6. 确定性 Workflow 再处理材料、预检、预览、确认和模拟状态。

当前交付是“受控 Agent + 确定性业务服务”：模型参与理解、检索和表达；工具权限、事实、状态和副作用由服务端收敛。

### Runtime Harness：把 Agent 包装成可控业务运行

Runtime Harness 负责模型之外的执行纪律：

- 运行预算、上下文预算、截止时间、超时和取消；
- Run / Turn / Event 生命周期与唯一终态；
- Tool Registry、参数 Schema、调用次数和最小权限；
- 观察结果压缩、Evidence 上下文装配和 AnswerDraft 校验；
- 重启、失败、恢复、幂等和版本冲突；
- 脱敏 Trace、学生投影与治理只读投影；
- 把模型错误归一化为学生可理解的下一步，而不是透传上游错误。

未来增加更多能力时，新增能力必须先进入工具契约、权限、错误、重试、恢复、审计和评测，而不是直接开放任意网络、文件系统、浏览器或业务写入。

### Provider Gateway：Responses 主协议，多 Provider 兼容

Provider Gateway 把不同上游的协议和方言收敛为统一的内部请求/响应契约：

- Responses：服务端主协议，承载文本、结构化输出、工具调用和原生流式事件；
- Chat Completions：显式备用协议，用于兼容仍以 Chat 接口为主的上游；
- SSE 事件归一化：把文本片段、工具参数增量、完成、错误和取消映射为内部事件；
- 能力矩阵：分别记录文本、工具、结构化输出、流式、取消、错误语义和 fallback 的 declared / observed 状态；
- 方言适配：模型字段、token 参数、推理开关、错误映射和请求变体按 Provider Profile 管理；
- 密钥隔离：Provider Secret 只从服务端环境或仓库外的 Secret 文件读取，学生端不暴露配置入口。

降级只在主协议尚未产生语义事件、且明确判断为能力不支持时发生。如果已经产生工具或文本语义事件后失败，Gateway 保持当前协议并返回明确错误，不静默重放或跨协议制造重复副作用。

## A1 Workflow：从问题到下一步

A1 活动场地申请是当前虚拟深度演示主线。它把自然语言入口与确定性业务状态机结合起来：

~~~text
用户诉求
  -> 事项识别与适用依据
  -> 活动信息与材料准备
  -> 确定性预检
  -> 预览结果与风险解释
  -> 用户明确确认
  -> 模拟审核 / 补正 / 状态推进
  -> 任务结束或进入下一步
~~~

设计原则：

- 自然语言适合表达意图，结构化表单适合确认字段；
- 预检只读，预览只展示，未确认不能产生模拟副作用；
- 只有明确确认后才推进本地演练状态；
- 依据版本变化时，任务要求重新确认，不静默覆盖用户已经确认的内容；
- 学生角色、虚拟办理角色和治理角色分开投影；
- 缺材料、规则冲突和系统失败都要给出可操作的下一步。

后续增加其他事项时，复用的是事项、依据、材料、状态和证据契约；每个新事项仍需单独完成资料核验、规则实现、错误恢复和黄金路径验收。

## Evaluation Harness：把质量变成工程闭环

评测不是一次性给模型打分，而是一条可回放的修复链：

~~~text
Case
  -> Run
  -> Attempt / Trace
  -> Validator + Judge + 人工审核
  -> Bad Case 分类
  -> 知识/规则/契约修复
  -> Regression
  -> GateDecision
~~~

质量体系按层定位问题：

- L1：输入与路由，检查意图分类、工具选择、Schema 和结构化输出；
- L2：知识与证据，检查召回、过滤、版本、引用和冲突；
- L3：业务结果，检查事项卡、材料预检、状态和下一步契约；
- L4：端到端交付，检查恢复、越权、脱敏、前端交互和发布门。

程序断言优先检查引用、权限、字段、状态机和模拟边界；语义 Judge 只能作为结构化辅助，不能替代来源审核。人工审核处理高风险、争议和评测器分歧。Hard Gate 独立于综合分数，不能用平均分掩盖工具越权、证据缺失、错误状态或 Secret 泄露。

Bad Case 记录来源、分类、修复动作、回归案例和版本差异。长期目标是形成：

~~~text
用户反馈 / 自动评测
  -> Bad Case
  -> 知识、规则、回答契约修复
  -> 召回与端到端回归
  -> 发布门
~~~

不未经人工审核自动修改公开知识，也不让被优化的 Agent 修改自己的评判标准。

## 前端工作台和庆小通

前端使用 React、TypeScript、Vite、React Router、TanStack Query、Zod 和 Lucide。目标不是把后端字段原样搬到页面，而是把复杂业务投影成稳定、可理解的学生工作台：

- 桌面与移动布局共享业务事实，但根据屏幕约束调整信息密度；
- `qst.stream.v1` 把检索、工具、预检、回答和终态增量投影成可理解的进度；
- 正常完成、失败、半截响应、取消和恢复都有明确界面状态；
- 表单、材料和预览保留阶段感，避免把长流程变成无边界长页面；
- 学生端不显示 Provider、模型、Trace、索引分数、内部角色和上游错误；
- 键盘提交、减少动效、长材料、焦点顺序和无横向溢出有独立验收。

前端打磨采用三阶段方法：

1. Discover：先探索多种信息架构、视觉方向和交互方案，不把第一版模型默认模板当成答案；
2. Define：用清晰的质量标准、截图和独立评审检查层级、密度、可读性、响应式和真实任务完成度；
3. Deliver：删掉不增加价值的装饰、容器和文案，保留真正帮助用户判断和行动的元素，再做浏览器和可访问性回归。

图像、动效和生成资产只有在能解释状态、强化任务或降低理解成本时才进入产品；它们不能用来遮盖流程不清、证据不足或错误状态。

庆小通是可替换的资产包和表现层伴随形象：

- 可以拖动，靠近网页边缘后半隐藏，点击露出部分恢复；
- 弹出位置跟随用户最后停留位置，不强制复位；
- 只表达待命、处理中、需要补充和完成等结构化状态；
- 不读取个人数据、不调用工具、不改变任务状态；
- 不成为权限绕过、真实提交或第二套状态机。

资产通过 `web/mascot/manifest.json` 描述来源、许可证/署名、静态回退、状态和内容指纹。渲染器只接受同源相对路径，不接受远程 URL、脚本协议或令牌；素材加载失败、清单损坏或用户启用减少动效时，文字等价物仍然可用。Live2D、Spine 等更复杂形态属于未来独立适配器，必须先完成许可证、供应链、包大小和浏览器性能核验。

未来会继续参考成熟的桌宠和 Agent 伴随形象，打造更亲和、更有沉浸感、同时对真实业务有帮助的引导角色。形象的价值不在于漂浮装饰，而在于帮助用户理解当前阶段和下一步。

## 本地运行

要求：Python 3.11+、Node.js 18+。Dense 检索和模型调用是可选依赖；没有这些依赖时，关键词检索和确定性演示仍可运行，但健康状态会诚实标记为降级。

### Python 与前端本地运行

~~~powershell
Copy-Item .env.example .env
python -m pip install -r backend/requirements.txt
npm install
npm run build
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
~~~

打开 <http://127.0.0.1:8000/>。Windows 也可以使用 `start-local.cmd` 或 `start-local.ps1`。前端开发服务器只适合开发调试；提交验证应同时检查生产构建产物和后端服务。

### Docker Compose 一键启动

要求：Docker Desktop 已启动。

~~~powershell
docker compose config --quiet
docker compose up --build
~~~

交付容器采用非 root、只读根文件系统、丢弃 Linux capabilities、`no-new-privileges` 和独立数据卷。模型目录以只读方式挂载；数据库和演练状态写入数据卷。Provider Secret 从仓库外文件或部署环境注入。

### Provider 配置边界

学生端不能填写或覆盖 Provider、Base URL、模型和 API Key。部署者可以在本地使用自己的兼容 Provider，下面只展示非敏感的配置形状，示例域名不可解析：

~~~dotenv
QST_APP_ENV=development
QST_LLM_PROVIDER=compatible
QST_LLM_BASE_URL=https://provider.example.invalid/v1
QST_LLM_MODEL=example-model
QST_LLM_API_KEY_FILE=REPLACE_WITH_OUTSIDE_SECRET_PATH
QST_LLM_RESPONSES_REQUEST_VARIANT=standard
QST_LLM_CHAT_REASONING_EFFORT=
~~~

Responses 是主协议，Chat Completions 是显式备用协议；是否支持工具、结构化输出、流式、取消和 fallback，要按 Provider Profile 逐项验证。API Key 不要写入 `.env.example`、源码、数据库、截图、Trace 或日志。

## 推荐演示路线

### A1 黄金路径

可以用下面的虚拟诉求开始演示：

~~~text
我想在 A1 广场办迎新活动，应该怎么办？
~~~

演示事项识别、适用依据和材料卡片，补齐活动信息，运行材料预检，打开提交预览，明确确认，推进模拟审核和补正状态，再刷新页面验证恢复。重点不是模型说了什么，而是用户能否看到“依据是什么、还缺什么、下一步是谁处理、什么时候需要重新确认”。这里的地点和角色是虚拟设计，不对应现实校园地点或真实责任方。

### 评测与治理

运行固定的公开安全案例集，展示 `pass`、`fail`、`error`、`skip` 和 `unscored` 的分层结果，再查看 Attempt、Bad Case、人工审核和发布门。治理投影可以读取脱敏 Trace、Publication 和评测摘要；学生页面只保留办事所需的业务语言。

## API 入口

| 路由 | 用途 |
|---|---|
| `GET /api/health/live`、`GET /api/health/ready` | 存活与就绪/降级状态 |
| `GET /api/health/dependencies` | 脱敏依赖状态 |
| `GET /api/bootstrap`、`GET /api/student/bootstrap` | 发布版本、事项和学生端目录 |
| `POST /api/query`、`POST /api/query/stream` | 事项回答和流式事件 |
| `POST /api/runs/{run_id}/cancel` | 幂等取消运行 |
| `/api/student/tasks/...` | 学生端任务、材料、预检、预览、确认和恢复 |
| `/api/governance/runs/{run_id}` | 脱敏治理 Trace |
| `/api/evaluations/...`、`/api/governance/evaluations/...` | 评测运行、结果和人工审核 |
| `/api/bad-cases/...` | Bad Case、回放、修复、回归和关闭 |
| `/api/publications/...`、`/api/governance/releases/...` | Publication 审核、发布、回滚和发布门 |

学生入口和治理入口分离。学生不能读取工程 Trace、Publication 内部字段、Provider 配置、索引分数或评测角色；治理路由也不代表学校管理员权限。

## 验证

```powershell
python -m pytest backend/tests -q
npm test -- --run
npm run build
python -m compileall backend
```

验证范围、当前环境差异和公开发布前的扫描步骤见 [`docs/verification.md`](docs/verification.md)。命令通过只证明当前代码和当前本地环境，不代表学校生产接入、所有 Provider 兼容或生产级承载能力。

建议按以下层次留存证据：

| 检查层 | 关注内容 | 入口 |
|---|---|---|
| 后端 | 单元、契约、状态机、恢复和公开边界 | `python -m pytest backend/tests -q` |
| 前端 | 组件状态、流式投影、路由和构建 | `npm test -- --run`、`npm run build` |
| 运行时 | Python 编译、健康端点、依赖降级和 Secret 隔离 | `python -m compileall backend`、`/api/health/*` |
| 资产 | 吉祥物 manifest、来源、哈希、同源路径和静态回退 | `python scripts/validate_mascot_manifest.py` |
| 浏览器 | A1 桌面/移动路径、贴边交互、键盘、减少动效和恢复 | `scripts/r0_frontend_acceptance.py`、`scripts/frontend_accessibility_acceptance.py` |
| 发布面 | 文档、代码、图片、JSON、构建产物和 Git 历史中的敏感信息 | `docs/verification.md` 中的扫描命令 + 人工复核 |

验证结果必须绑定当前 checkout、当前数据、当前 Provider Profile 和当前运行环境。旧截图、Mock、单个上游 Smoke 或历史日志不能自动证明当前行为。

## 目录

```text
backend/              FastAPI、RAG、Runtime、Workflow、评测与治理投影
frontend/             React/Vite 学生工作台源码
web/                  可交付前端与原创庆小通资产
scripts/              本地预检和浏览器验收脚本
docs/architecture.md  公开架构与模块边界
docs/verification.md  可复核的本地验证流程
compose.yaml          Docker Compose 运行入口
Dockerfile            非 root、只读根文件系统运行镜像
```

## 参考资料与开发方法

本项目的技术叙事来自“资料库召回 -> 回读原文 -> 项目映射 -> 自己验证”的过程。资料库文章中的企业案例、规模、指标和“生产级”自述不会直接变成庆事通的事实；只有经过当前项目代码、测试、评测或人工验收支撑的内容，才会写成“当前实现”。

| 参考 | 原文提供的视角 | 庆事通如何吸收 |
|---|---|---|
| [How to turn your AI into a world-class designer](https://www.lennysnewsletter.com/p/how-to-turn-your-ai-into-a-world)（资料库 ID：WEB-20260901-LENNY-01） | Discover / Define / Deliver；多方向探索、独立截图评审、资产使用和删减无价值元素 | 前端先探索方向，再做独立视觉与任务评审，最后删除不能帮助用户判断或行动的元素 |
| 得物技术《RAG 核心概念与原理：Chunking、Embedding、相似度、HNSW 与多路召回》（DW-20260722-01） | Query Rewrite、Metadata Filter、Sparse/Dense、多路召回、RRF、Rerank 和统一评测 | 采用前置范围过滤、FTS5 + 可选 Dense 的复合检索；不把文章示例阈值或规模当作项目结论 |
| 得物技术《得物知识问答：复合检索 Agent 的系统设计实践》（DW-20260812-01） | 多源检索、检索质量分层、来源特征和可回放恢复 | 将不同来源、召回质量、流式状态和恢复拆成可测试契约；文章自述结果仍需项目独立验证 |
| 阿里技术《Loop、Graph、Harness：What Should Be Chosen》（ALI-20260915-01） | Harness、Loop、Graph 的嵌套关系；状态 owner/readers/mutable；完成断言、Evidence、预算、停止和降级 | 将 Delivery、Runtime、Evaluation 分开；把状态、验证和停止条件放进代码和 Schema，而不是只写在 Prompt 里 |
| 阿里技术《垂类业务如何落地生产级 Agent》（ALI-20260916-02） | 从场景分级、动作目录和调用前策略，到知识编译、版本、漂移和评测门 | 先判断是否值得 Agent 化，再用只读工具、规则、版本化知识、人工确认和发布门形成小闭环 |
| [awesome-readme](https://github.com/matiassingers/awesome-readme) | 项目身份、快速理解、功能/架构、安装、使用、验证、贡献和 License 的信息层级 | README 按读者路径组织：先说明价值，再说明实现，再给运行和验证入口，最后说明边界、参考和贡献方式 |

在后续模块开发中，参考资料不会一次性全部塞进上下文。每个决策点按以下流程处理：

~~~text
项目问题
  -> 索引/卡片召回候选资料
  -> 回到对应原文、官方文档或源码
  -> 记录采用、改造、不采用和未验证原因
  -> 写入项目契约与测试
  -> 形成运行、评测、浏览器和安全证据
  -> 再更新 README 和公开叙事
~~~

这保证 README 既有企业级技术叙事，又不会把参考文章的观点冒充成项目已经完成的能力。

## 从可核验首版走向真实服务层

当前 R0 是一个可复核的模拟基线。下一阶段不会因为“有 Agent”就覆盖所有校园业务。每个方向都要先证明三件事：普通搜索、筛选或固定表单哪里不够；Agent 能减少什么可测成本或错误；失败风险是否可控。

### 1. 建设可治理的校园知识底座

在公开且获得授权的前提下，逐步整理校园官网、公告、学生手册、办事指南和规则资料。资料进入系统前保留来源、版本、定位、时效和授权记录，再经过脱敏、重写、结构化切分、冲突检查、审核和 Publication 发布。资料库的目标不是“收集越多越好”，而是让每一个进入检索的结论都能说明来源类型、适用范围、版本和责任边界。

未来知识库可以覆盖公告、条款、材料、评奖、学分和流程等问题，并通过来源、版本、适用范围和责任边界保持可追溯。

### 2. 从“查到资料”扩展到“完成业务准备”

围绕学生真正的时间成本，继续扩展适合 Agent 的连续任务，把事项识别、材料预检、入口命中、状态跟踪和依据变化传播连起来。普通公告浏览仍然交给普通信息中心；只有跨来源理解、个性化适用判断和连续准备存在明确增量时，才进入 Agent 主线。候选事项需要通过匿名化问题、流程摩擦、可量化指标和失败影响的审查，不能因为“可以接模型”就被纳入。

### 3. 让 Agent 在 Harness 内承担更多真实工作

未来可以加入更丰富的 Tool/Capability Registry、可恢复 Worker、上下文压缩、成本与延迟观测、任务重新评估和人机协作。每个新能力都通过 Schema、权限、预算、错误、重试、幂等、恢复、审计和评测进入系统；先完成原子决策、规格、票据和契约测试，再进入实现。

模型提供动态判断，程序负责不变量、写入时机和责任边界。新名词不会自动成为架构，只有能解决明确工程问题并通过项目证据的能力才会进入正式路线。

### 4. 融合真实入口和服务系统

经授权后，庆事通可以通过独立适配器逐步连接统一身份、融合门户、企业微信和真实业务系统：

~~~text
学生入口 / 门户 / 企业微信
  -> 身份与会话适配
  -> StudentResponse / Runtime
  -> 资料与 Workflow
  -> 真实业务系统适配器
  -> 预览、确认、审批和回执
~~~

入口不会自动获得学校权限，Agent 不直接绕过业务系统。真实写入、审批和敏感数据访问仍由授权系统、服务端策略和责任人控制，并通过独立契约、灰度、回滚和恢复验收。

### 5. 持续打磨产品呈现

展示落地页负责项目叙事和能力引导，工作台负责高频办事，门户/企业微信模拟负责展示嵌入式场景，多端布局负责不同设备上的真实使用。庆小通会继续成为一个能解释状态、引导下一步、具有亲和力的业务角色，而不是装饰性的漂浮图片。前端优化以任务完成、响应式、可访问性、状态反馈和业务边界为验收对象，而不是只追求视觉效果。

每一项演进都遵循同一节奏：技术资料库原文和官方文档定向核对 -> 记录采用、改造、不采用和未验证原因 -> 形成规格和票据 -> 契约实现与测试 -> 浏览器、评测、恢复和安全验收 -> Standards/Spec 双轴 Review -> 更新公开叙事。未来目标不会提前写成当前成绩，外部文章的自述指标也不会直接变成庆事通的验证结论。

## 贡献

请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 和 [`SECURITY.md`](SECURITY.md)，再提交代码、文档或示例。

## License

当前仓库暂未声明开源许可证。除非仓库后续明确添加许可证，否则不要把代码或资产当作可自由再分发的开源组件。
