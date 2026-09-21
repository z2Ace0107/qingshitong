# 庆事通

> 面向校园服务场景的证据优先 Agent 工作台：把“我该怎么办”整理成依据、条件、材料和可执行的下一步。

庆事通是一个独立的 AI 应用原型。它研究校园事务中常见的信息分散、规则难懂、材料准备不确定和状态难追踪问题，把检索增强生成（RAG）、确定性 Workflow、受控 Agent Runtime 和可回放评测组合成一条可运行的演示闭环。

本项目可以用“广州大学校园场景研究”作为比赛叙事背景，但它不是广州大学官方项目，不代表学校背书，不代表学校当前规则，也没有接入统一身份、融合门户、企业微信、教务系统或任何真实审批系统。仓库中的地点、责任方、规则、入口、岗位和办理状态均为脱敏改写或虚拟设计。

## 公开数据边界

这是本仓库最重要的约束：

- 真实公开页面、公告、手册、办事指南、登录后页面、截图和接口观察只允许留在项目维护者控制的本地研究资料库；
- 任何个人信息、账号、密码、Cookie、令牌、验证码、私有页面、真实业务记录和未处理接口响应都不得进入 Git；
- 公开仓库只保留通用业务机制、抽象后的领域模型、脱敏重写结果和明确标注的虚拟演示数据；
- 演示入口使用 `example.invalid` 等不可解析域名，不指向学校真实站点；
- “官方入口”“业务责任方”“审核状态”在演示中只是结构化字段或虚拟角色，不是现实授权的证明。

详细规则见 [`docs/data-boundary.md`](docs/data-boundary.md)。提交前应运行仓库内的敏感信息扫描，并检查新增文本是否仍能反推出真实页面、地点、人员、编号或内部规则。

## 为什么不是另一个聊天框

普通问答可以生成一段看起来合理的文字，但校园事务通常还需要判断适用对象、对应事项、时间窗口、材料缺口、责任方和下一步。庆事通把这些环节拆开：

```text
用户目标
  -> 事项识别与必要澄清
  -> RAG 召回已发布的虚拟依据
  -> Evidence Gate 检查结论是否有证据
  -> 事项卡与材料预检
  -> 预览
  -> 用户明确确认
  -> 虚拟状态流转
```

模型负责理解语言、组织候选和表达结果；事实、权限、字段校验、状态变化和副作用由程序控制。没有足够依据时，系统应澄清、降级或引导核验，而不是补写一条看似官方的规则。

## 当前演示范围

种子数据包含四类经过重写的虚拟场景：

- 活动场地申请：自然语言诉求、字段收集、材料预检、虚拟档期、预览、确认和补正；
- 选课公告与时间窗口：公开信息解释与个人结果回到官方系统核验的边界；
- 勤工助学岗位：岗位筛选、资格提示、模拟申请和虚拟发布方状态；
- 学生请假与销假：条件性材料、模拟责任方处理和状态闭环。

它们用于验证业务结构，不用于证明任何学校当前办理规则。所有状态变化只写入本地演示数据库，不能产生真实提交、审批或回执。

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

## 系统结构

```text
学生 Web / 移动布局
          |
StudentResponse projection
          |
Runtime Harness
  - 预算、上下文、取消、超时、恢复
  - 只读 Tool Registry
  - 脱敏 Trace 与投影
          |
Agent Loop ---- Provider Gateway
          |
RAG / Knowledge ---- Deterministic Workflow
          |
SQLite facts, events and traces
          |
Evaluation / Governance read projections
```

前端是 React + TypeScript + Vite 工作台，后端是 Python + FastAPI。桌面、移动和门户嵌入外壳共享同一套业务事实与任务状态；渠道外壳不拥有学校身份或业务权限。庆小通是可替换的表现层伴随形象，只表达待命、处理中、需要补充和完成等状态，不读取个人数据、不调用工具、不改变任务状态。

## 本地运行

要求：Python 3.11+、Node.js 18+。Dense 检索和模型调用是可选依赖；没有这些依赖时，关键词检索和确定性演示仍可运行，但健康状态会诚实标记为降级。

```powershell
Copy-Item .env.example .env
python -m pip install -r backend/requirements.txt
npm install
npm run build
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>。也可以使用：

```powershell
docker compose config --quiet
docker compose up --build
```

API Key 不要写入 `.env.example`、源码、数据库、截图或日志。Provider 配置由部署环境提供，学生端不暴露 Provider、模型、Base URL 或 API Key 输入框。

## 验证

```powershell
python -m pytest backend/tests -q
npm test -- --run
npm run build
python -m compileall backend
```

验证范围、当前环境差异和公开发布前的扫描步骤见 [`docs/verification.md`](docs/verification.md)。命令通过只证明当前代码和当前本地环境，不代表学校生产接入、所有 Provider 兼容或生产级承载能力。

## 目录

```text
backend/              FastAPI、RAG、Runtime、Workflow、评测与治理投影
frontend/             React/Vite 学生工作台源码
web/                  可交付前端与原创庆小通资产
scripts/              本地预检和浏览器验收脚本
docs/architecture.md  公开架构与模块边界
docs/data-boundary.md 真实资料、本地研究与公开数据规则
docs/verification.md  可复核的本地验证流程
compose.yaml          Docker Compose 运行入口
Dockerfile            非 root、只读根文件系统运行镜像
```

## 后续方向

下一阶段不会因为“有 Agent”就覆盖所有校园业务。方向选择要先证明三件事：普通搜索/筛选/表单哪里不够，Agent 能减少什么可测成本或错误，以及失败风险是否可控。真实校园资料仍只在本地研究；公开产品资料继续使用脱敏、重写和虚拟化内容。

经过授权后，未来才可能增加门户、企业微信或真实业务系统适配器。身份、权限、真实写入、审批和回执仍属于授权系统及责任人，不能由 Agent 绕过。比赛叙事可以面向真实校园问题，但当前仓库不会把研究背景写成官方接入事实。

## 贡献与安全

请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 和 [`SECURITY.md`](SECURITY.md)。提交 Issue、代码或文档时，不要粘贴真实学校页面、个人记录、截图、接口响应、凭据或未脱敏业务细节。发现疑似敏感信息时，先停止传播并按安全说明处理。

## License

当前仓库暂未声明开源许可证。除非仓库后续明确添加许可证，否则不要把代码或资产当作可自由再分发的开源组件。
