# River Sentinel AGENTS Workflow Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the root `AGENTS.md` that governs River Sentinel graduation-project development, worktree collaboration, three read-only review roles, data and model integrity, verification, and Git operations.

**Architecture:** `AGENTS.md` is the single project-level collaboration policy. It adapts the proven `F:\code\mall\AGENTS.md` worktree/report/review loop to a personal AI graduation project and keeps exactly three read-only roles: project exploration, ML/data review, and code review.

**Tech Stack:** Markdown, Git, Codex project agents, PowerShell verification commands

**Spec:** `docs/superpowers/specs/2026-09-16-river-sentinel-project-design.md`

## Global Constraints

- Project root is `F:\code\engine` and the default branch is `main`.
- The project is a Zhejiang University of Water Resources and Electric Power artificial-intelligence undergraduate graduation design.
- Initial research scope is Wenzhou public water-rainfall data, 5–20 representative river stations, and 1/3/6-hour water-level forecasting.
- Use exactly three read-only collaboration roles: `project_explorer`, `ml_reviewer`, and `code_reviewer`.
- Worktree implementation conversations are started manually by the user; do not describe an automatic MCP dispatch workflow.
- Never batch-delete files or directories. Never use `del /s`, `rd /s`, `rmdir /s`, `Remove-Item -Recurse`, or `rm -rf`.
- Real bulk data, model weights, secrets, local environment files, database backups, and personal information must not enter Git.
- Model conclusions require traceable data range, station set, features, parameters, random seed, code version, and evaluation artifacts.
- The agent and system are for teaching and research assistance and must not claim to replace official flood-control decisions.

---

### Task 1: Create and verify the root collaboration policy

**Files:**
- Create: `AGENTS.md`
- Reference: `docs/superpowers/specs/2026-09-16-river-sentinel-project-design.md`
- Reference: `F:\code\mall\AGENTS.md`

**Interfaces:**
- Consumes: the approved design specification and the mall project's worktree/report/review pattern.
- Produces: one root policy file that future main-agent and worktree conversations can follow without consulting the mall repository.

- [ ] **Step 1: Record the pre-change repository state**

Run:

```powershell
git status --short
git log -2 --oneline
```

Expected: `main` contains the approved design-spec commits; no unrelated uncommitted files are present.

- [ ] **Step 2: Create `AGENTS.md` with the exact policy structure**

The file must use this heading order and implement the listed rules without placeholders:

```markdown
# River Sentinel 项目协作规则

## 1. 项目定位与目录
```

State the Chinese and English project names, project root, graduation-design purpose, initial Wenzhou/5–20 stations/1-3-6 hour scope, configurable regional scope, and planned directories for backend, frontend, ML, data, documents, deployment, and scripts. List the approved technology stack: FastAPI, LangGraph, Scikit-learn/XGBoost-or-LightGBM/PyTorch, SQLite/PostgreSQL, Redis, Vue 3/TypeScript/Vite/ECharts, and Docker Compose.

```markdown
## 2. 主 Agent 职责
```

Require the main Agent to analyze requirements, explain business and research flow, design architecture, identify file/API/database/data/model impacts, define acceptance criteria, split worktree tasks, review actual diffs and experiment evidence, run verification, and manage local Git. Prohibit silent modification of core business code, training logic, or data-processing rules.

```markdown
## 3. 标准工作流
```

Include the approved ten-step flow verbatim in substance: explain scope and acceptance; split data/ML, backend/agent, frontend/docs tasks only when boundaries justify it; generate copyable prompts; user manually sends prompts to worktree conversations; require `.codex/reports/<task-slug>-report.md`; inspect report plus actual `git diff` and `git status`; invoke only relevant read-only reviewers; generate precise rework prompts; run tests/data checks/training smoke tests/build/API validation; create Chinese commits; ensure clean worktree and `main`; merge only from the main directory; reverify on `main`; report hashes/files/data-model impact/verification/debt; push only after explicit user confirmation.

```markdown
## 4. 工作树任务提示词与报告
```

Require every prompt to contain the absolute worktree path, objective and flow, allowed and forbidden file ranges, API/database/data/model constraints, error handling, acceptance criteria, verification commands, and prohibitions against commit/push/merge/scope expansion. Require each report to contain completion summary, changed files and key locations, API/configuration effects, data/model effects, commands and results, Git state, remaining issues, and next suggestions. Reports are local collaboration artifacts and are not committed by default.

```markdown
## 5. Codex 协作角色
```

Define exactly three read-only roles:

- `project_explorer`: code, API, module dependency, business-flow, and data-flow mapping.
- `ml_reviewer`: source/licence/quality/time-alignment/leakage checks plus baseline, model, metric, experiment, reproducibility, and conclusion review.
- `code_reviewer`: correctness, security, validation, error handling, interface consistency, regression, and missing-test review.

Require evidence-backed findings with file paths and line numbers. State that irrelevant roles need not be called, reviewers do not edit files, and summaries never replace actual diff, data, model, or test inspection.

```markdown
## 6. 数据与实验规范
```

Require source URL/provider/licence or access conditions/download time/field dictionary/checksum; raw data immutability; chronological train-validation-test split; explicit leakage checks; persistence/statistical, ML, and deep-learning comparison; MAE/RMSE/R²/NSE and warning precision/recall/F1/lead-time reporting; experiment configuration and random-seed capture; failed experiments and limitations retained; no fabricated data, metrics, citations, or conclusions.

```markdown
## 7. 智能体与防汛业务边界
```

Require risk levels to come from prediction services and deterministic warning rules, not free-form LLM generation. Require tool-call traceability, observation/model timestamps, model version, graceful failure, stale-data labels, baseline fallback records, and the teaching/research disclaimer. Prohibit official-warning impersonation and unsupported dispatch recommendations.

```markdown
## 8. 测试与验收
```

Define proportional verification for backend tests, ML preprocessing/time-split/metric/smoke tests, frontend type-check/test/build, data validation, Docker Compose configuration, and end-to-end sample flow. Require fresh command output before completion claims and revalidation after merging to `main`.

```markdown
## 9. 毕业设计过程材料
```

Require continuous updates to topic approval, task book, proposal, literature review, foreign translation, progress records, midterm check, acceptance/test/user guide, thesis, defense, and archive materials. State that current college/advisor templates, deadlines, word counts, and plagiarism thresholds override repository defaults.

```markdown
## 10. Git 与文件安全
```

Require feature branches/worktrees for substantial stages; Chinese logical commits; clean-status checks before and after merge; no push without explicit confirmation; no secrets, `.env`, real bulk data, weights, caches, build products, database backups, personal information, or temporary thesis files. Repeat the explicit single-file-only deletion rule and prohibited recursive deletion commands from Global Constraints.

```markdown
## 11. 汇报要求
```

Require final reports to list branch and merge hashes when applicable, changed files, API/database/data/model impact, exact verification commands and outcomes, known limitations, and next options. Prohibit declaring completion based only on reports, reviewer summaries, or expected outcomes.

- [ ] **Step 3: Verify structural and policy requirements**

Run:

```powershell
$agentFile = 'AGENTS.md'
$requiredHeadings = @(
  '## 1. 项目定位与目录',
  '## 2. 主 Agent 职责',
  '## 3. 标准工作流',
  '## 4. 工作树任务提示词与报告',
  '## 5. Codex 协作角色',
  '## 6. 数据与实验规范',
  '## 7. 智能体与防汛业务边界',
  '## 8. 测试与验收',
  '## 9. 毕业设计过程材料',
  '## 10. Git 与文件安全',
  '## 11. 汇报要求'
)
$missing = $requiredHeadings | Where-Object { -not (Select-String -LiteralPath $agentFile -SimpleMatch $_ -Quiet) }
if ($missing.Count -gt 0) { throw "Missing headings: $($missing -join ', ')" }
$roleCount = (Select-String -LiteralPath $agentFile -Pattern '^### `(?:project_explorer|ml_reviewer|code_reviewer)`').Count
if ($roleCount -ne 3) { throw "Expected 3 role definitions, found $roleCount" }
$forbidden = Select-String -LiteralPath $agentFile -Pattern 'data_auditor|Remove-Item -Recurse.*允许|rm -rf.*允许'
if ($forbidden) { throw "Forbidden or stale policy content found" }
Write-Output 'AGENTS_POLICY_CHECK=PASS'
```

Expected: `AGENTS_POLICY_CHECK=PASS`.

- [ ] **Step 4: Verify the Git patch**

Run:

```powershell
git diff --check
git diff -- AGENTS.md
git status --short
```

Expected: no whitespace errors; the diff contains only the new `AGENTS.md` policy plus the already committed plan if the plan has not yet been committed separately.

- [ ] **Step 5: Commit the policy as one logical unit**

Run:

```powershell
git add AGENTS.md
git commit -m "chore: 配置毕设项目协作工作流"
git status --short
git log -1 --oneline
```

Expected: the policy commit succeeds and no `AGENTS.md` change remains unstaged or uncommitted.
