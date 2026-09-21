# 贡献指南

## 提交前检查

```powershell
python -m pytest backend/tests -q
npm test -- --run
npm run build
python -m compileall backend
```

提交前还要检查：

- `git diff --check` 没有空白错误；
- `git grep` 没有真实域名、密钥样式、个人路径或未脱敏学校细节；
- 新增场景标记为 `virtual_design` 或等价的公开安全状态；
- README、页面文案和测试没有暗示官方背书或真实系统接入；
- 对外 Issue 只写抽象结论和可公开的研究方法，不粘贴来源原文。

## 代码变更

业务规则应放在确定性服务中，并为字段、状态和失败路径添加测试。Agent 只能通过服务端注册工具获得能力；不要在提示词中偷偷扩大权限，也不要让模型直接写业务状态。

前端变更需要覆盖加载、空状态、错误、降级、移动布局、键盘操作和减少动效。任何模拟提交都必须有明显边界，并在预览和确认之间保留人工确认。

## Issue 与外部发布

Issue、Release、Push、真实 Provider 联调和第三方系统接入都属于外部动作。提交前先给出目标、精确文本、事实依据、影响范围和验证结果，由维护者确认。
