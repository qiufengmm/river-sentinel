# WorkBuddy 主 Agent 与可逆交接工作流设计规格

## 1. 目标与适用范围

本设计将 River Sentinel 项目的唯一主 Agent 从当前 Codex 会话平移到 WorkBuddy。WorkBuddy 能直接访问 `F:\code\engine`、Git 仓库和项目内工作树，因此采用原地接管，不复制仓库、不重新创建功能分支、不搬运未提交文件。

迁移完成后：

- WorkBuddy 是唯一具备任务编排、审查协调、验证、提交和本地合并职责的主 Agent；
- 工作树对话继续只负责提示词明确授权的实现和报告，不提交、不合并、不推送；
- 当前 Codex 会话作为备用接管点，只在用户明确要求返回时恢复主控；
- 任意时刻只有一个主 Agent 可以执行写文件或 Git 写操作；
- 任务执行到任意中间状态时，均可生成交接包并返回当前 Codex 会话。

迁移只改变协作控制面，不改变毕业设计题目、数据源、模型路线、业务功能和现有Git历史。

## 2. 方案选择

采用“单主控、可逆交接”方案。

不采用双主控方案，因为两个主 Agent 同时生成任务、修改状态或操作Git会造成重复实现、审查结论冲突和未提交改动覆盖。不采用“Codex主控、WorkBuddy仅执行”方案，因为它不满足主 Agent 平移目标。

## 3. 角色与权限

### 3.1 WorkBuddy 主 Agent

WorkBuddy负责：

1. 读取 `AGENTS.md`、已批准规格、实施计划、当前状态和最新报告；
2. 在实施前说明流程、架构、文件、接口、数据库、数据和模型影响以及验收标准；
3. 为工作树对话生成可直接复制的详细提示词；
4. 等待用户人工转发提示词和回传结果；
5. 读取工作树报告，并独立检查真实 `git diff`、`git status` 和实验产物；
6. 按影响范围组织 `project_explorer`、`ml_reviewer`、`code_reviewer` 只读审查；
7. 发现问题时生成精确返工提示词；
8. 审查通过后运行验证、创建中文逻辑提交、从主目录本地合并；
9. 合并后在 `main` 重新验证；
10. 只有用户明确确认后才允许推送远程。

WorkBuddy不得绕过工作树提示词流程直接修改核心数据处理、模型训练、业务规则或前端业务代码。

### 3.2 工作树执行对话

工作树执行对话只能修改提示词授权的路径，必须测试先行并生成 `.codex/reports/<task-slug>-report.md`。不得提交、合并、推送、切换分支、扩大范围或删除未授权文件。

### 3.3 当前 Codex 会话

迁移后当前会话处于 `STANDBY`，允许只读查看仓库和交接状态。只有用户明确要求“返回Codex接管”并完成接管核验后，才能恢复写操作和Git管理。

### 3.4 用户

用户负责：

- 在主 Agent 和工作树对话之间人工转发提示词与完成通知；
- 确认主 Agent 控制权切换；
- 确认本地合并结果；
- 明确授权远程推送；
- 处理必须由本人完成的账号、实名认证、API审批和密钥配置。

## 4. 单一事实来源

可信度从高到低依次为：

1. 实际Git提交、分支、工作树、`git diff`、`git status` 和文件内容；
2. 已提交的设计规格、实施计划、协作规则和决策记录；
3. 数据快照、校验值、测试输出和实验产物；
4. 工作树报告与WorkBuddy交接报告；
5. 聊天摘要。

低等级信息与高等级事实冲突时，以高等级事实为准。任何主 Agent 都不得仅凭聊天中的“已完成”声明创建提交或合并。

## 5. 本地控制面文件

WorkBuddy复用现有 `.codebuddy/` 目录：

```text
.codebuddy/
├─ plans/                  # WorkBuddy本地计划
├─ state/
│  └─ active-task.md       # 唯一当前状态
└─ handoffs/
   └─ YYYY-MM-DD-HHMM-<from>-to-<to>.md
```

工作树报告继续使用：

```text
.codex/reports/<task-slug>-report.md
```

`.codebuddy/`、`.codex/state/`、`.codex/handoffs/` 和 `.codex/reports/` 是本地协作产物，默认不提交Git。已批准的长期规则和重大决策写入 `docs/workflow/` 或现有规格文档并提交。

## 6. 当前任务状态文件

`.codebuddy/state/active-task.md` 使用固定结构：

```markdown
# 当前任务状态

- CONTROL_OWNER: WorkBuddy
- CONTROL_STATE: ACTIVE
- UPDATED_AT: 2026-09-18T00:00:00+08:00
- PROJECT_ROOT: F:\code\engine
- MAIN_BRANCH: main
- MAIN_HEAD: <实际提交>
- WORKTREE_PATH: <实际绝对路径>
- FEATURE_BRANCH: <实际分支>
- FEATURE_HEAD: <实际提交>
- TASK_ID: <任务标识>
- TASK_PHASE: <状态机状态>

## 当前目标

## 已完成

## 未提交改动

## 已运行验证

## 问题与阻塞

## 下一步唯一动作

## 禁止操作
```

状态文件中的提交、路径和工作区状态必须通过只读命令现场取得，不得从旧聊天中复制后直接视为有效。

## 7. 控制权状态机

主控状态只允许：

- `ACTIVE`：当前主 Agent 可执行授权范围内的操作；
- `PAUSING`：停止发起新操作，正在收集现场；
- `HANDOFF_READY`：现场已冻结，等待接管方核验；
- `STANDBY`：只读待命；
- `BLOCKED`：存在需要用户或外部系统解决的阻塞。

控制权持有者只允许：

- `WorkBuddy`
- `Codex`
- `Human`

不得出现WorkBuddy和Codex同时处于 `ACTIVE`。控制权变更必须由用户确认，并在接管方核验仓库后更新状态文件。

## 8. 任务执行状态机

每项实现任务使用以下阶段：

```text
READY
→ DISPATCHED
→ IMPLEMENTING
→ REPORTED
→ REVIEWING
→ REWORK
→ VERIFIED
→ COMMITTED
→ MERGED
```

允许从 `IMPLEMENTING`、`REPORTED`、`REVIEWING` 或 `REWORK` 进入 `PAUSING`。暂停不得要求工作区干净，但必须准确列出未提交、未跟踪和已暂存文件。

## 9. WorkBuddy标准业务流

1. 读取当前状态与计划，现场检查主目录和相关工作树；
2. 若状态记录与Git不一致，先修正状态，不继续任务；
3. 输出任务流程、架构、影响和验收标准；
4. 生成带绝对路径、允许/禁止范围、验证命令和报告路径的工作树提示词；
5. 用户人工转发；
6. 用户回传后，读取报告、实际diff、状态和产物；
7. 只调用与改动有关的只读审查角色；
8. 有问题则生成返工提示词，返回第5步；
9. 无问题则运行新鲜验证；
10. WorkBuddy按逻辑单元创建中文提交；
11. 功能工作树和主目录都满足合并条件后，从主目录本地合并；
12. 在 `main` 重跑关键验证并报告结果；
13. 用户明确确认后才推送。

## 10. 中途返回Codex协议

用户要求返回时，WorkBuddy必须：

1. 停止发送新提示词、写文件和Git写操作；
2. 将控制状态设为 `PAUSING`；
3. 读取实际分支、HEAD、状态、diff、暂存区、最新报告和最近验证；
4. 更新 `.codebuddy/state/active-task.md`；
5. 创建 `.codebuddy/handoffs/<timestamp>-workbuddy-to-codex.md`；
6. 将状态设为 `HANDOFF_READY`；
7. 在聊天中只返回交接文件路径、当前阶段、是否有未提交改动和阻塞。

Codex接管时必须：

1. 读取 `AGENTS.md`、规格、计划、状态和交接文件；
2. 独立运行 `git status`、`git diff`、`git diff --cached`、`git log`；
3. 检查报告与实际文件是否一致；
4. 未经用户授权不清理、回滚或删除WorkBuddy留下的改动；
5. 核验完成后把控制持有者改为 `Codex`、状态改为 `ACTIVE`；
6. 从交接文件记录的“下一步唯一动作”继续，不从头重复已完成工作。

反向返回WorkBuddy使用相同协议，只交换发送方与接收方。

## 11. 交接文件结构

每份交接文件必须包含：

- 发送方、接收方、生成时间和控制状态；
- 主目录与工作树绝对路径；
- 主分支、功能分支、基准提交和当前HEAD；
- 当前目标、任务阶段和验收标准；
- 已完成内容与对应提交；
- 已修改、暂存、未跟踪和忽略文件；
- 已运行命令与实际结果；
- 最新报告和实验产物路径；
- P0/P1/P2问题、阻塞和已尝试方案；
- 下一步唯一动作；
- 禁止操作；
- 密钥、真实数据和推送状态。

交接报告不得包含密钥值、真实个人信息或大规模数据内容。

## 12. 当前现场迁移基线

WorkBuddy首次正式接管前必须重新核验以下已知现场：

- 主目录：`F:\code\engine`；
- 主分支：`main`；
- 已知主分支HEAD：`068fa51`，以接管时实测为准；
- 数据基线工作树：`F:\code\engine\.worktrees\feiyunjiang-data-baseline`；
- 功能分支：`codex/feiyunjiang-data-baseline`；
- 已知功能HEAD：`f21dc6e`，以接管时实测为准；
- 已提交：Task 0范围修订、Task 1项目骨架、Task 2数据契约；
- 本地审查报告：`.codex/reports/data-baseline-task1-2-retrospective-review.md`；
- 工作树已有Task 1～2返工改动，尚未由主 Agent复核和提交；
- `ml/tests/ingestion/` 是暂停中的Task 3测试，返工完成前不得继续；
- 主目录已有 `.codebuddy/plans/feiyunjiang-task0-2-retrospective-review_f579b6a0.md`；
- 未执行远程推送。

WorkBuddy接管后的下一步唯一动作是：完成Task 1～2返工报告，等待主 Agent读取报告并核验实际diff；返工通过后才能提交返工和恢复Task 3。

## 13. 异常处理

- 状态文件缺失：从Git和最新报告重建，禁止猜测；
- 状态文件过期：以实际Git状态为准并记录修正；
- 工作区存在未知改动：停止写操作，确认来源和所有权；
- WorkBuddy不可用：生成当前可获得的交接证据，由用户要求Codex接管；
- 验证失败：保留失败输出，任务进入 `REWORK`，不得提交；
- API密钥缺失：记录 `API_AUTH_PENDING`，不得抓取网页内部接口替代；
- 主目录或工作树不干净：允许中途交接，但在提交或合并前必须按工作流处理；
- 报告与实际diff冲突：以diff为准，报告退回修正。

## 14. 安全与Git边界

- 禁止批量删除文件或目录；
- 未经用户明确授权不执行破坏性Git命令；
- 不覆盖用户、WorkBuddy或其他工作树的未提交改动；
- 不提交 `.env`、密钥、真实大数据、模型权重、本地数据库、缓存或本地交接文件；
- WorkBuddy不得在功能工作树内切换到 `main`；
- 未经用户确认不得推送远程；
- 控制权切换不会自动扩大任务权限。

## 15. 实施交付物

设计批准后，迁移实施产生：

1. `docs/workflow/workbuddy-main-agent.md`：长期有效的WorkBuddy主控规则；
2. `.codebuddy/state/active-task.md`：接管时的现场状态；
3. `.codebuddy/handoffs/<timestamp>-codex-to-workbuddy.md`：首次交接包；
4. WorkBuddy首次启动提示词；
5. 必要的 `.gitignore` 本地协作目录规则；
6. `AGENTS.md` 中WorkBuddy单主控和可逆交接补充规则。

## 16. 验收标准

迁移完成必须满足：

1. WorkBuddy能从同一仓库和现有工作树读取当前现场；
2. 状态文件记录的分支、HEAD和未提交文件与Git实测一致；
3. WorkBuddy被明确设置为唯一 `ACTIVE` 主 Agent；
4. 当前Codex被设置为 `STANDBY`；
5. 首次交接包包含恢复任务所需的全部证据且不含密钥；
6. WorkBuddy的下一步是Task 1～2返工复核，不越过审查继续Task 3；
7. 从WorkBuddy返回Codex的步骤可以在不清理工作区的情况下执行；
8. 本次迁移不修改数据、模型、业务代码、Task 3测试或实验结论；
9. 不执行远程推送；
10. 用户可以仅提供交接文件路径，让任一主 Agent从中间状态继续。
