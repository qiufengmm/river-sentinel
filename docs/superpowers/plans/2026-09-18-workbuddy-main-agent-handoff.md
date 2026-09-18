# WorkBuddy 主 Agent 迁移实施计划

> **Required agentic workers:** 当前 Codex 仅执行迁移引导与证据核验；迁移激活后由 WorkBuddy 作为唯一主 Agent。不得并行主控。

**目标：** 将 River Sentinel 的唯一主 Agent 从当前 Codex 可逆地迁移到 WorkBuddy，同时保留中途返回 Codex 的标准入口。

**架构：** 永久规则写入受 Git 跟踪的 `AGENTS.md`、`docs/workflow/workbuddy-main-agent.md` 和校验脚本；动态控制权、活动任务及交接记录放在忽略的 `.codebuddy/` 下；工作树报告继续放在 `.codex/reports/`。初次迁移采用两阶段交接：Codex 准备 `Human/HANDOFF_READY`，WorkBuddy 独立核验后切换为 `WorkBuddy/ACTIVE`。

**技术：** Markdown、PowerShell、Git、WorkBuddy 本地工作区控制。

**设计依据：** [WorkBuddy 主 Agent 可逆交接设计](../specs/2026-09-18-workbuddy-main-agent-handoff-design.md)

## 全局约束

- 任一时刻只能有一个主控；WorkBuddy 与 Codex 不得同时处于 `ACTIVE`。
- 动态状态必须来自当次读取的 Git、文件、报告和测试证据，不得照抄旧摘要。
- WorkBuddy 直接访问现有仓库与 worktree，不复制、重建或重新初始化项目。
- 保留现有 `.codebuddy/plans/` 内容，不覆盖已完成的只读审查计划。
- 不修改 `feiyunjiang-data-baseline` 工作树中正在返工的业务代码、契约和摄取测试。
- 本迁移不改变 API、数据库、数据集、特征、模型、前端或论文结论。
- `.codebuddy/`、`.codex/reports/`、`.codex/state/`、`.codex/handoffs/` 均为本地控制产物，不进入功能提交。
- 不记录密钥或非空 `WENZHOU_DATA_APPSECRET`。
- 不执行 `git push`，不批量删除文件，不扩大既有授权。

## Task 1：落地受 Git 跟踪的永久迁移规则

**文件：**

- 新建：`docs/workflow/workbuddy-main-agent.md`
- 新建：`scripts/validate-workbuddy-workflow.ps1`
- 修改：`AGENTS.md`
- 修改：`.gitignore`

### 1.1 先编写失败的工作流校验器

校验脚本提供参数：

- `ProjectRoot`：默认仓库根目录。
- `RequireLocalState`：要求并校验动态状态文件。

校验器必须：

1. 要求存在 `AGENTS.md`、工作流文档、设计规格和本计划。
2. 检查规则文本包含：
   - `WorkBuddy 是默认且唯一的主 Agent`；
   - `WorkBuddy 与 Codex 不得同时处于 ACTIVE`；
   - `用户人工转发`；
   - `未经用户明确确认不得执行 git push`。
3. 检查工作流文档具有以下标题：`控制权模型`、`标准执行流程`、`返回 Codex`、`返回 WorkBuddy`、`状态文件`、`交接文件`、`当前任务恢复入口`。
4. 使用 Git 忽略检查确认 `.codebuddy/`、`.codex/reports/`、`.codex/state/`、`.codex/handoffs/` 不会进入提交。
5. 启用 `RequireLocalState` 时读取 `.codebuddy/state/active-task.md`，验证 owner、control_state 及允许组合：
   - `Human/HANDOFF_READY`；
   - `WorkBuddy/ACTIVE`；
   - `Codex/ACTIVE`；
   - `WorkBuddy/BLOCKED`；
   - `Codex/BLOCKED`。
6. 拒绝状态文件中出现非空 `WENZHOU_DATA_APPSECRET`。
7. 成功时输出 `WORKBUDDY_WORKFLOW_CHECK=PASS`，失败时返回非零退出码和明确原因。

先运行：

```powershell
pwsh -File scripts/validate-workbuddy-workflow.ps1 -ProjectRoot (Get-Location)
```

预期：因工作流文档或规则尚未齐全而失败，证明校验器有效。

### 1.2 新建 WorkBuddy 主控工作流文档

文档必须明确：

- WorkBuddy 是默认且唯一的主 Agent，Codex 默认待命。
- 控制权的 owner 与 control_state 状态机。
- WorkBuddy 遵循现有人工转发工作树流程，不自动分发编码任务。
- 报告和聊天仅是线索，实际 Git、代码、数据、产物和测试优先。
- WorkBuddy 的任务前说明、审查、返工、验证、提交、合并和推送边界。
- 从 WorkBuddy 返回 Codex、再返回 WorkBuddy 的步骤与禁止并行写入要求。
- 状态文件与交接文件字段、更新时间和证据要求。
- 当前 Task 1～2 返工审查是迁移后的唯一下一动作，Task 3 继续暂停。

### 1.3 追加根协作规则

在 `AGENTS.md` 末尾追加 `## 12. WorkBuddy 主控与可逆交接`，只追加迁移控制规则，不重写原有 1～11 节。应引用工作流文档并规定：

- WorkBuddy 为默认唯一主控。
- Codex 仅在完成交接且状态为 `Codex/ACTIVE` 时恢复写入。
- 用户继续人工转发工作树提示词。
- 控制权变化不等于授权扩大。
- 未经用户确认不得推送。

### 1.4 更新忽略规则

在 `.gitignore` 追加明确规则：

```gitignore
.codebuddy/
.codex/reports/
.codex/state/
.codex/handoffs/
```

不得删除或重排与本任务无关的已有规则。

### 1.5 验证与提交

运行：

```powershell
pwsh -File scripts/validate-workbuddy-workflow.ps1 -ProjectRoot (Get-Location)
git diff --check
git diff -- AGENTS.md .gitignore docs/workflow/workbuddy-main-agent.md scripts/validate-workbuddy-workflow.ps1
git status --short --branch
```

验收：脚本输出 PASS；diff 无空白错误；只出现授权文件。

创建中文提交：

```powershell
git add AGENTS.md .gitignore docs/workflow/workbuddy-main-agent.md scripts/validate-workbuddy-workflow.ps1 docs/superpowers/plans/2026-09-18-workbuddy-main-agent-handoff.md
git commit -m "docs: 配置 WorkBuddy 主控与可逆交接"
```

## Task 2：将迁移配置合并到 main

### 2.1 合并前核验

在设计 worktree 检查：

```powershell
git status --short --branch
git log -3 --oneline
```

在主目录检查：

```powershell
git -C F:\code\engine status --short --branch
git -C F:\code\engine log -3 --oneline
git -C F:\code\engine check-ignore -v .codebuddy/plans/feiyunjiang-task0-2-retrospective-review_f579b6a0.md
```

若主目录唯一未跟踪项为既有 `.codebuddy/`，使用 `apply_patch` 向 `F:\code\engine\.git\info\exclude` 追加字面量 `.codebuddy/`，不得删除或改写 `.codebuddy/` 内容。若还有其他改动，停止合并并报告。

### 2.2 本地合并

确认设计 worktree 和 main 均干净后，在主目录运行：

```powershell
git -C F:\code\engine merge --no-ff codex/workbuddy-handoff-design -m "merge: 合并 WorkBuddy 主控迁移配置"
```

不得在功能 worktree 内切换到 `main`，不得推送远程。

### 2.3 合并后复验

```powershell
pwsh -File F:\code\engine\scripts\validate-workbuddy-workflow.ps1 -ProjectRoot F:\code\engine
git -C F:\code\engine diff --check HEAD^ HEAD
git -C F:\code\engine status --short --branch
git -C F:\code\engine log -5 --oneline --decorate
```

验收：校验器通过；main 无未提交改动；历史中存在功能提交和非快进合并提交。

## Task 3：生成初次动态交接包

**保留文件：**

- `.codebuddy/plans/feiyunjiang-task0-2-retrospective-review_f579b6a0.md`

**新建本地文件：**

- `.codebuddy/state/active-task.md`
- `.codebuddy/handoffs/2026-09-18-codex-to-workbuddy.md`
- `.codebuddy/handoffs/2026-09-18-workbuddy-startup-prompt.md`

### 3.1 采集新鲜证据

读取并记录：

```powershell
git -C F:\code\engine status --short --branch
git -C F:\code\engine rev-parse HEAD
git -C F:\code\engine log -5 --oneline --decorate
git -C F:\code\engine\.worktrees\feiyunjiang-data-baseline status --short --branch
git -C F:\code\engine\.worktrees\feiyunjiang-data-baseline rev-parse HEAD
git -C F:\code\engine\.worktrees\feiyunjiang-data-baseline diff --stat
git -C F:\code\engine\.worktrees\feiyunjiang-data-baseline diff --name-status
```

同时读取 Task 1～2 返工报告、已批准设计和实施计划。所有哈希、文件列表和状态必须填写实际值。

### 3.2 写入活动任务状态

状态文件初值：

- `owner: Human`
- `control_state: HANDOFF_READY`
- `task_id: feiyunjiang-data-baseline-task1-2-rework`
- `task_phase: REVIEWING`

唯一下一动作必须写成：WorkBuddy 读取 Task 1～2 返工报告并检查实际 diff；审查通过前不继续 Task 3。

状态中需包含主目录和功能 worktree 的实际路径、分支、HEAD、工作区摘要、相关报告、已知 P1/P2、测试状态、阻塞项和更新时间。不得包含密钥。

### 3.3 写入交接说明

交接文件至少包含：

- 发送方 Codex、接收方 WorkBuddy、当前所有者 Human。
- 主目录和功能 worktree 的路径、分支、实际提交哈希。
- 已提交改动、未提交改动及其来源。
- 报告路径、验证结果、P0/P1/P2 和尚未复验的内容。
- Task 3 仍暂停。
- `API_AUTH_PENDING` 等真实阻塞项。
- 未授权 push，未扩大业务修改范围。

### 3.4 写入 WorkBuddy 启动提示词

提示词要求 WorkBuddy：

1. 完整读取 `AGENTS.md`、工作流、状态、交接、设计规格和实施计划。
2. 独立运行 Git 状态、diff、日志及报告检查。
3. 保留 `.codebuddy/plans/` 与未跟踪的 ingestion 测试。
4. 只有证据匹配时才将状态切换为 `WorkBuddy/ACTIVE`。
5. 激活后的第一项任务是 Task 1～2 返工审查。
6. 审查通过前不得提交、合并、推送或启动 Task 3。

### 3.5 本地状态验证

```powershell
pwsh -File F:\code\engine\scripts\validate-workbuddy-workflow.ps1 -ProjectRoot F:\code\engine -RequireLocalState
git -C F:\code\engine check-ignore -v .codebuddy/state/active-task.md
git -C F:\code\engine check-ignore -v .codebuddy/handoffs/2026-09-18-codex-to-workbuddy.md
git -C F:\code\engine check-ignore -v .codebuddy/handoffs/2026-09-18-workbuddy-startup-prompt.md
git -C F:\code\engine status --short --branch
```

验收：状态组合合法；三个动态文件均被忽略；main 保持干净；既有 plans 文件未改变。

## Task 4：WorkBuddy 独立核验并激活

### 4.1 人工转发

用户将 `.codebuddy/handoffs/2026-09-18-workbuddy-startup-prompt.md` 的内容复制到 WorkBuddy。自此 Codex 不再写文件，等待 WorkBuddy 返回核验结果。

### 4.2 WorkBuddy 核验

WorkBuddy 必须重新读取：

- main 与功能 worktree 的分支、HEAD、status、diff 和最近提交；
- Task 1～2 报告及当前改动；
- 永久工作流、状态文件和交接说明；
- 现有 `.codebuddy/plans/` 是否完整。

若事实不一致，保持 `Human/HANDOFF_READY` 并报告差异；不得自行猜测或继续开发。

### 4.3 激活唯一主控

证据一致时，WorkBuddy：

1. 将状态从 `Human/HANDOFF_READY` 更新为 `WorkBuddy/ACTIVE`。
2. 更新核验时间、实际 HEAD、工作区摘要和唯一下一动作。
3. 新建 `.codebuddy/handoffs/2026-09-18-workbuddy-activation.md`，记录核验命令和结果。
4. 运行带 `RequireLocalState` 的校验器并确认 main 干净。
5. 向用户返回激活文件路径、owner/state、main/功能分支 HEAD、下一动作和阻塞项。

用户把激活结果转回当前 Codex 后，Codex只读核验并进入待命。以后需要中途返回时，按工作流文档执行 `WorkBuddy/PAUSING -> Human/HANDOFF_READY -> Codex/ACTIVE`，返回 WorkBuddy 时执行对称流程。

## 最终验收清单

1. 永久工作流和校验器已提交并合并到 main。
2. main 上工作流校验通过。
3. 既有 `.codebuddy/plans/` 文件内容完整。
4. 动态状态与交接文件反映新鲜 Git 事实。
5. 所有本地控制产物均被忽略，main 无未提交改动。
6. WorkBuddy 已独立核验仓库和 worktree。
7. 状态为 `WorkBuddy/ACTIVE`，且 Codex 不再写入。
8. 迁移后的唯一下一动作是 Task 1～2 返工审查。
9. Task 3 在审查结论前保持暂停。
10. 全流程没有 push、密钥写入、业务/模型修改或破坏性删除。
