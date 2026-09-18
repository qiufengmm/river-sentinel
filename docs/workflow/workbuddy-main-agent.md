# WorkBuddy 主 Agent 工作流

本文件定义 River Sentinel 由 WorkBuddy 统一编排并可逆交接到 Codex 的长期方式。它补充根目录 `AGENTS.md`，不削弱其中的数据、实验、审查、Git 和文件安全要求。

## 控制权模型

WorkBuddy 是默认且唯一的主 Agent，负责需求分析、任务拆解、工作树提示词、审查、验证、中文提交和 `main` 本地合并。WorkBuddy 与 Codex 不得同时处于 `ACTIVE`。

`owner` 可以是 `WorkBuddy`、`Codex` 或交接期间的 `Human`。`control_state` 包括：

- `ACTIVE`：唯一可执行授权内写操作与 Git 管理的主控；
- `PAUSING`：停止新操作并固化现场；
- `HANDOFF_READY`：现场已记录，等待接管方核验；
- `STANDBY`：非主控只做用户要求的只读核验；
- `BLOCKED`：等待用户或外部条件变化。

控制权变化只表示由谁继续执行既有授权，不扩大文件、业务、数据、模型、Git 或外部系统权限。

## 标准执行流程

1. WorkBuddy 读取 `AGENTS.md`、本文件和活动状态，并核验主目录与目标 worktree 的分支、HEAD、status、diff、报告及测试证据。
2. 开始任务前说明业务或研究流程、架构、关键文件、接口、数据库、数据集、特征、模型和文档影响，并给出验收命令。
3. 按真实边界生成工作树提示词，写明绝对路径、允许与禁止范围、异常处理、验收标准和报告路径。
4. 工作树任务继续由用户人工转发；WorkBuddy 不自动分发编码任务，也不绕过工作树修改核心业务代码、数据规则、特征、训练或风险规则。
5. 回传后先读 `.codex/reports/<task-slug>-report.md`，再读实际 Git、代码、数据、实验产物和测试结果。
6. 按影响调用相关只读角色；报告、聊天和审查摘要不能代替主 Agent 的实际核验。
7. 有问题则生成含文件位置、证据、风险、授权范围和验收标准的返工提示词，由用户人工转发。
8. 审查通过后运行新鲜验证，按逻辑单元创建中文提交；两个工作区都干净后才在主目录本地合并。
9. 合并后在 `main` 重新验证并检查 diff、历史、实验索引和状态。
10. 汇报提交、文件、影响、验证及遗留问题；未经用户明确确认不得执行 `git push`。

风险等级仍只能来自预测服务和确定性规则。控制权迁移不改变系统的教学科研辅助定位。

## 返回 Codex

用户可在实现、报告、审查或返工中的任何时刻要求返回 Codex，不要求工作区先变干净。WorkBuddy 必须：

1. 停止新任务，进入 `WorkBuddy/PAUSING`；
2. 采集主目录和相关 worktree 的分支、HEAD、staged、unstaged、untracked、报告、测试和阻塞；
3. 在 `.codebuddy/handoffs/` 创建 WorkBuddy 到 Codex 的交接文件；
4. 将状态改为 `Human/HANDOFF_READY` 后停止写入；
5. 只向用户返回交接路径、阶段、未提交状态和阻塞。

Codex 必须独立核验实际现场。证据一致后才切换为 `Codex/ACTIVE`；否则保持等待并报告差异。Codex 不得擅自清理、回滚、提交或覆盖 WorkBuddy 的改动。

## 返回 WorkBuddy

从 Codex 返回 WorkBuddy 使用对称协议：Codex 进入 `PAUSING`、重新采集证据、创建交接文件、切换为 `Human/HANDOFF_READY` 并停止写入；用户人工转发交接路径；WorkBuddy 独立核验后才能切换为 `WorkBuddy/ACTIVE`。

任何返回流程都不能自动推送、扩大任务范围、清理未提交改动或跳过原有审查门禁。

## 状态文件

动态状态固定使用 `F:\code\engine\.codebuddy\state\active-task.md`，不纳入 Git。至少记录：

- `owner`、`control_state`、`task_id`、`task_phase` 和更新时间；
- 主目录及相关 worktree 的路径、分支、HEAD 和工作区摘要；
- 报告、计划、审查发现、验证证据、阻塞和唯一下一动作；
- 提交、合并与推送的授权状态。

每次接管、暂停、阻塞、提交、合并或关键验证后刷新状态。内容必须来自当次事实，不得写入密钥、Token 或非空 `WENZHOU_DATA_APPSECRET`。

## 交接文件

交接文件固定放在 `F:\code\engine\.codebuddy\handoffs\`，文件名包含日期和方向。每份至少记录：

- 发送方、接收方、owner/state 和生成时间；
- 主目录与 worktree 的路径、分支、HEAD、staged、unstaged 和 untracked；
- 已完成、未完成、唯一下一动作、报告、计划、测试与实验产物；
- P0/P1/P2、失败项、阻塞、风险和 Git/外部操作授权。

`.codebuddy/plans/` 中已有计划必须保留；工作树报告继续写入 `.codex/reports/`。交接与聊天只是恢复线索，不能覆盖 Git 和实际文件事实。

## 当前任务恢复入口

- 主目录：`F:\code\engine`；
- 功能 worktree：`F:\code\engine\.worktrees\feiyunjiang-data-baseline`；
- 任务：`feiyunjiang-data-baseline-task1-2-rework`；
- 阶段：Task 1～2 返工后的主 Agent 审查；
- 报告：`F:\code\engine\.worktrees\feiyunjiang-data-baseline\.codex\reports\data-baseline-task1-2-retrospective-review.md`；
- 唯一下一动作：读取最新返工报告并核验实际 `git diff`、`git status`、代码与测试；
- 门禁：审查通过前不提交返工、不合并、不恢复 Task 3；
- 阻塞：真实 API 凭据可用前保持 `API_AUTH_PENDING`，不得伪造实时数据。

首次迁移由 Codex 生成 `Human/HANDOFF_READY` 状态和启动提示词，WorkBuddy 独立核验后才切换为 `WorkBuddy/ACTIVE`。核验失败时不得猜测或越过当前任务。
