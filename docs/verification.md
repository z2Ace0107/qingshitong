# 本地验证说明

本文件只定义如何复核当前 checkout，不把旧运行日志、旧截图或第三方结果当作当前证据。

## 快速检查

```powershell
python -m pytest backend/tests -q
npm test -- --run
npm run build
python -m compileall backend
git diff --check
```

## 运行检查

```powershell
docker compose config --quiet
docker compose up --build
```

观察 `/api/health/live`、`/api/health/ready` 和 `/api/health/dependencies`。`ready` 只表示当前本地依赖状态，不表示生产承载、学校授权或真实 Provider 的普遍兼容性。

## 公开面扫描

```powershell
git grep -n -I -E "sk-[A-Za-z0-9_-]{8,}|Bearer |api[_-]?key|Cookie:|[A-Za-z]:\\Users\\|[A-Za-z]:\\[^\" ]+|[[:alnum:]_-]+\.(edu|edu\.cn|gov|gov\.cn)(/|$)"
git grep -n -I -E "官方背书|已接入真实系统|登录后内容|个人办件|真实审批结果|真实责任方"
```

这些命令不是完美的 DLP 工具，也不能替代项目私有资料库中的组织名称 denylist。命中后必须人工判断是否为安全的说明、测试占位符或仍需要清理的真实细节。还要人工检查图片、JSON、构建产物和远程 Issue。

## 证据语言

- “代码存在”不等于“行为通过”；
- “本地测试通过”不等于“学校系统已接入”；
- “兼容配置可运行”不等于“所有 Provider 都兼容”；
- “虚拟场景可演练”不等于“真实审批已完成”；
- 没有当前命令输出时，不在 README 或 Issue 中写固定通过数字。
