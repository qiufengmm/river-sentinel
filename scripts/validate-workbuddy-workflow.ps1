[CmdletBinding()]
param(
    [Parameter()]
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),

    [Parameter()]
    [switch]$RequireLocalState
)

$ErrorActionPreference = "Stop"

function Stop-WorkflowValidation {
    param([Parameter(Mandatory)][string]$Message)

    Write-Error "WORKBUDDY_WORKFLOW_CHECK=FAIL: $Message"
    exit 1
}

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$requiredFiles = @(
    "AGENTS.md",
    "docs/workflow/workbuddy-main-agent.md",
    "docs/superpowers/specs/2026-09-18-workbuddy-main-agent-handoff-design.md",
    "docs/superpowers/plans/2026-09-18-workbuddy-main-agent-handoff.md"
)

foreach ($relativePath in $requiredFiles) {
    $fullPath = Join-Path $resolvedRoot $relativePath
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        Stop-WorkflowValidation "缺少必需文件: $relativePath"
    }
}

$agentsText = Get-Content -LiteralPath (Join-Path $resolvedRoot "AGENTS.md") -Raw
$workflowPath = Join-Path $resolvedRoot "docs/workflow/workbuddy-main-agent.md"
$workflowText = Get-Content -LiteralPath $workflowPath -Raw
$combinedRules = "$agentsText`n$workflowText"

$requiredPhrases = @(
    "WorkBuddy 是默认且唯一的主 Agent",
    "WorkBuddy 与 Codex 不得同时处于 ``ACTIVE``",
    "用户人工转发",
    "未经用户明确确认不得执行 ``git push``"
)

foreach ($phrase in $requiredPhrases) {
    if (-not $combinedRules.Contains($phrase)) {
        Stop-WorkflowValidation "缺少规则文本: $phrase"
    }
}

$requiredHeadings = @(
    "控制权模型",
    "标准执行流程",
    "返回 Codex",
    "返回 WorkBuddy",
    "状态文件",
    "交接文件",
    "当前任务恢复入口"
)

foreach ($heading in $requiredHeadings) {
    if ($workflowText -notmatch "(?m)^#{1,6}\s+$([regex]::Escape($heading))\s*$") {
        Stop-WorkflowValidation "工作流文档缺少标题: $heading"
    }
}

$ignoreProbes = @(
    ".codebuddy/state/active-task.md",
    ".codex/reports/workbuddy-validation-probe.md",
    ".codex/state/active-task.md",
    ".codex/handoffs/workbuddy-validation-probe.md"
)

foreach ($probe in $ignoreProbes) {
    & git -C $resolvedRoot check-ignore --quiet --no-index -- $probe
    if ($LASTEXITCODE -ne 0) {
        Stop-WorkflowValidation "本地控制路径未被 Git 忽略: $probe"
    }
}

if ($RequireLocalState) {
    $statePath = Join-Path $resolvedRoot ".codebuddy/state/active-task.md"
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        Stop-WorkflowValidation "缺少动态状态文件: .codebuddy/state/active-task.md"
    }

    $stateText = Get-Content -LiteralPath $statePath -Raw
    $ownerMatch = [regex]::Match($stateText, "(?m)^owner:\s*(\S+)\s*$")
    $controlStateMatch = [regex]::Match($stateText, "(?m)^control_state:\s*(\S+)\s*$")

    if (-not $ownerMatch.Success) {
        Stop-WorkflowValidation "动态状态文件缺少 owner 字段"
    }
    if (-not $controlStateMatch.Success) {
        Stop-WorkflowValidation "动态状态文件缺少 control_state 字段"
    }

    $owner = $ownerMatch.Groups[1].Value
    $controlState = $controlStateMatch.Groups[1].Value
    $allowedPairs = @(
        "Human/HANDOFF_READY",
        "WorkBuddy/ACTIVE",
        "Codex/ACTIVE",
        "WorkBuddy/BLOCKED",
        "Codex/BLOCKED"
    )
    $pair = "$owner/$controlState"

    if ($pair -notin $allowedPairs) {
        Stop-WorkflowValidation "非法控制权组合: $pair"
    }

    $secretMatch = [regex]::Match(
        $stateText,
        "(?m)^\s*WENZHOU_DATA_APPSECRET\s*:\s*(\S.*?)\s*$"
    )
    if ($secretMatch.Success -and -not [string]::IsNullOrWhiteSpace($secretMatch.Groups[1].Value)) {
        Stop-WorkflowValidation "动态状态文件不得包含非空 WENZHOU_DATA_APPSECRET"
    }
}

Write-Output "WORKBUDDY_WORKFLOW_CHECK=PASS"
exit 0
