# AI Worklog for Obsidian

[English](README.md) | [简体中文](README.zh-CN.md)

AI Worklog 用于跨项目跟踪需求，将 Codex 和 Claude Code 的多个会话整理为
Obsidian 中统一、可检索、可恢复的任务档案。

> 公开预览版本：当前仅支持 macOS。档案格式采用严格校验，项目在 `1.0.0`
> 之前不承诺稳定 API。

## 数据关系

一个需求或任务可以涉及多个项目和多个会话：

```text
需求 T-123
|-- 项目
|   |-- web-api
|   `-- payment-worker
`-- 会话
    |-- web-api 中的 Codex 会话
    |-- payment-worker 中的 Claude Code 会话
    `-- payment-worker 中的 Codex 会话
```

Agent 是会话属性，不是额外层级。项目和会话直接归属于任务，因此 recall 和
query 面向整个需求工作历史，而不是按工具分割。

## 功能

- 将当前 Codex 或 Claude Code 会话绑定到需求、工单、任务或工作项 ID。
- 在一个 Obsidian 档案中聚合多个项目和多个会话。
- 按时间倒序恢复所有相关会话，并显示 Agent 与安全的恢复命令。
- 按项目、仓库、话题、结果、状态等托管字段查询任务历史。
- 保留托管区域外的手工笔记。

AI Worklog 不会自动绑定、搜索托管目录之外的笔记、安装 Obsidian、同步
Vault，也不会把两个 Agent 的更新合并为一个事务。

## 环境要求

- macOS
- Codex CLI/Desktop 或 Claude Code
- Python 3.10 或更高版本
- Obsidian，并至少有一个本地 Vault
- Git，用于 Marketplace 安装和升级

## 安装

安装前请检查插件 manifest 和 Claude SessionStart 命令 Hook。Hook 只把当前
Claude Agent 与 Session ID 提供给共享 Skill，不会写入 Obsidian。

### Codex

```bash
codex plugin marketplace add 4Nz/ai-worklog
codex plugin add ai-worklog@ai-worklog
```

安装后新建一个 Codex 任务。

### Claude Code

```bash
claude plugin marketplace add 4Nz/ai-worklog
claude plugin install ai-worklog@ai-worklog
```

检查并信任同步 SessionStart Hook，然后重启 Claude Code。没有 Hook 时，bind
会因为无法获得可信的 Claude Session ID 而安全失败。

## 快速开始

Codex：

```text
$ai-worklog bind T-123 设计支付回调幂等方案
$ai-worklog recall T-123
$ai-worklog query 幂等
```

Claude Code：

```text
/ai-worklog:ai-worklog bind T-123 设计支付回调幂等方案
/ai-worklog:ai-worklog recall T-123
/ai-worklog:ai-worklog query 幂等
```

`bind` 会先校验 Agent、Session ID、仓库、项目、Vault 和已有档案，再修改
会话名称。只有自动改名成功或用户明确确认手工改名后，才会写入档案。

## 存储位置

共享配置：

```text
~/.config/ai-worklog/config.yaml
```

所选 Vault 中的托管档案：

```text
AI-Coding-Archive/WorkItems/<work-item-id>.md
```

每个档案包含一个全局摘要、去重后的项目投影和按时间倒序的平铺会话列表。
插件版本与档案 `schema_version` 分开管理。`0.1.0` 只读写
`schema_version: 1`，普通插件升级不会重写档案。

托管规则见[档案 Schema](docs/dossier-schema.md)，组件关系见
[架构说明](docs/architecture.md)。

## 隐私与安全

档案可能包含：

- Agent 类型和 Session ID；
- 完整恢复命令；
- 仓库 URL 和项目名称；
- 本地项目绝对路径；
- 话题、结果、下一步和状态。

带凭证的 Git Remote 会在持久化前被拒绝或清理，但其余元数据仍可能敏感。
请保护 Vault，检查同步设置，不要在 Issue 中提交真实档案。AI Worklog 不会把
query 扩展到 Vault 中未托管的笔记。

## 升级

Codex：

```bash
codex plugin marketplace upgrade ai-worklog
```

命令完成后新建任务；Codex Desktop 仍显示旧版本时请重启应用。

Claude Code：

```bash
claude plugin marketplace update ai-worklog
claude plugin update ai-worklog@ai-worklog
```

升级后重启 Claude Code。如果同时安装了两个 Agent，需要分别执行两组命令。
升级一边不会自动升级或回滚另一边。

## 回滚

可以将代码固定到已知 Release Tag；档案数据不会自动降级。

Codex 回滚到 `v0.1.0`：

```bash
codex plugin remove ai-worklog@ai-worklog
codex plugin marketplace remove ai-worklog
codex plugin marketplace add 4Nz/ai-worklog --ref v0.1.0
codex plugin add ai-worklog@ai-worklog
```

Claude Code 回滚到 `v0.1.0`：

```bash
claude plugin uninstall ai-worklog@ai-worklog
claude plugin marketplace remove ai-worklog
claude plugin marketplace add https://github.com/4Nz/ai-worklog.git#v0.1.0
claude plugin install ai-worklog@ai-worklog
```

随后重启对应 Agent。涉及档案 Schema 变更时，回滚前必须阅读 Release Notes。

## 禁用与卸载

Codex 可以在 `/plugins` 中禁用插件，或执行：

```bash
codex plugin remove ai-worklog@ai-worklog
```

Claude Code：

```bash
claude plugin disable ai-worklog@ai-worklog
claude plugin uninstall ai-worklog@ai-worklog
```

禁用、卸载、升级和回滚都不会删除配置或 Obsidian 档案。只有在明确需要清理
数据时，才单独删除 `~/.config/ai-worklog` 或
`AI-Coding-Archive/WorkItems`。

## 开发

```bash
python3 -m unittest discover -s skills/ai-worklog/tests -v
python3 scripts/validate_release.py
python3 scripts/package_release.py --output-dir dist
claude plugin validate .claude-plugin/plugin.json --strict
```

运行时代码只使用 Python 标准库。贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)，
发布步骤见[发布流程](docs/release-process.md)。

## 限制与路线图

- 暂不支持 Linux 和 Windows。
- 目前只内置 Codex 和 Claude Code Adapter。
- 不对多台机器同时写入同一同步 Vault 提供分布式锁。
- 档案迁移、Obsidian MCP 和公开插件目录提交属于后续工作。

项目采用 [Apache-2.0](LICENSE) 许可证。
