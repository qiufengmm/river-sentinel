"""LangGraph orchestration: 意图路由 → 白名单工具 → 证据检查 → Provider 解释（DS-8A）.

状态图是**编排层**：数值与风险等级全部来自白名单工具，Provider 只能解释；
Provider 输出一旦出现官方预警口径或与确定性等级冲突，即被拦截并回退本地模板。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..schemas.common import (
    AGENT_TOOL_FAILED,
    CLARIFY_INTENT,
    DISCLAIMER,
    PROVIDER_OVERRIDE_BLOCKED,
    PROVIDER_UNAVAILABLE,
    DataMode,
    ResponseStatus,
    warning_of,
)
from ..schemas.domain import AgentResponse, RiskResult
from ..services.container import ServiceContainer
from .providers import (
    FORBIDDEN_TOKENS,
    AnswerProvider,
    LocalDemoProvider,
    build_provider,
)
from .tools import TOOL_REGISTRY, WHITELISTED_TOOLS

#: 意图识别关键词（顺序即优先级；简报优先于风险，避免"简报"被"等级"误命中）。
INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("brief", ("简报", "汇总", "总结", "报告", "通报")),
    ("risk", ("风险", "判定", "等级", "高水位", "阈值")),
    ("comparison", ("对比", "比较", "指标", "精度", "mae", "rmse", "nse", "r2")),
    ("forecast", ("预测", "预报", "未来", "h1", "h3", "h6", "1 小时", "3 小时", "6 小时")),
    ("water_level_history", ("水位", "历史", "观测", "查询", "最近", "水情")),
)

#: 无法识别意图时的澄清意图。
CLARIFY = "clarify"

#: 每个意图对应的白名单工具执行计划。
INTENT_PLANS: dict[str, tuple[str, ...]] = {
    "water_level_history": ("query_water_level_history",),
    "forecast": ("forecast_water_level",),
    "comparison": ("compare_forecast_models",),
    "risk": ("forecast_water_level", "evaluate_research_risk"),
    "brief": (
        "query_water_level_history",
        "forecast_water_level",
        "evaluate_research_risk",
        "build_water_brief",
    ),
    CLARIFY: (),
}

#: 每个工具接受的入参（其余状态字段不会传入，避免签名漂移）。
TOOL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "query_water_level_history": ("station_id", "data_mode"),
    "forecast_water_level": ("station_id", "as_of", "horizons", "model", "data_mode"),
    "compare_forecast_models": ("horizons",),
    "evaluate_research_risk": ("station_id", "as_of", "horizons", "model", "data_mode"),
    "build_water_brief": ("station_id", "as_of", "horizons", "model", "data_mode"),
}

CLARIFICATION = (
    "未能识别你的意图。可尝试：查询最近水位、预测未来 1/3/6 步、对比模型指标、"
    "判断研究性风险、生成水情简报。"
)


class AgentState(TypedDict, total=False):
    """Graph state; every value is produced by tools or the router."""

    user_message: str
    station_id: str
    as_of: datetime | None
    horizons: list[int]
    model: str
    data_mode: DataMode
    intent: str
    tool_calls: list[str]
    tool_results: dict[str, dict[str, Any]]
    risk_verdict: dict[str, Any] | None
    warnings: list[str]
    evidence: list[str]
    final_answer: str
    status: ResponseStatus
    provider: str


def classify(message: str) -> str:
    """Route the user message to one intent (``CLARIFY`` when unmatched)."""
    text = message.lower()
    for intent, keywords in INTENT_KEYWORDS:
        if any(keyword.lower() in text for keyword in keywords):
            return intent
    return CLARIFY


def _route(state: AgentState) -> dict[str, Any]:
    intent = classify(state.get("user_message", ""))
    plan = [tool for tool in INTENT_PLANS.get(intent, ()) if tool in WHITELISTED_TOOLS]
    warnings: list[str] = []
    if intent == CLARIFY:
        warnings.append(warning_of(CLARIFY_INTENT, CLARIFICATION))
    return {"intent": intent, "tool_calls": plan, "warnings": warnings}


def _run_tools(services: ServiceContainer) -> Callable[[AgentState], dict[str, Any]]:
    def node(state: AgentState) -> dict[str, Any]:
        results: dict[str, dict[str, Any]] = {}
        for name in state.get("tool_calls", []):
            tool = TOOL_REGISTRY[name]
            kwargs = {key: state.get(key) for key in TOOL_ARGUMENTS.get(name, ())}
            results[name] = asdict(tool(services, **kwargs))
        risk_verdict = None
        risk_entry = results.get("evaluate_research_risk")
        if risk_entry and not risk_entry.get("error"):
            payload = risk_entry.get("payload") or {}
            risk_verdict = payload.get("result")
        return {"tool_results": results, "risk_verdict": risk_verdict}

    return node


def _check_evidence(state: AgentState) -> dict[str, Any]:
    warnings = list(state.get("warnings", []))
    statuses = [status for status in _tool_statuses(state)]
    status: ResponseStatus = "ok"
    if "error" in statuses:
        status = "error"
    elif "unavailable" in statuses:
        status = "unavailable"
    elif "degraded" in statuses:
        status = "degraded"

    for name, entry in state.get("tool_results", {}).items():
        error = entry.get("error")
        if error:
            warnings.append(warning_of(AGENT_TOOL_FAILED, f"工具 {name}：{error}"))
    evidence = [
        f"{name}:{entry.get('status')}" for name, entry in state.get("tool_results", {}).items()
    ]
    return {"status": status, "warnings": warnings, "evidence": evidence}


def _tool_statuses(state: AgentState) -> list[ResponseStatus]:
    return [
        str(entry.get("status", "ok"))
        for entry in state.get("tool_results", {}).values()
        if isinstance(entry, dict)
    ]


def _block_reason(answer: str, risk_verdict: dict[str, Any] | None) -> str | None:
    """Reject provider output that uses official wording or contradicts the verdict."""
    for token in FORBIDDEN_TOKENS:
        if token in answer:
            return f"回答出现官方口径用词「{token}」，已回退本地模板"
    if risk_verdict:
        level = str(risk_verdict.get("level"))
        lowered = answer.lower()
        for candidate in ("normal", "watch", "elevated"):
            if candidate != level and candidate in lowered:
                return f"回答出现与确定性等级 {level} 不一致的 {candidate}，已回退本地模板"
    return None


def _explain(provider: AnswerProvider) -> Callable[[AgentState], dict[str, Any]]:
    def node(state: AgentState) -> dict[str, Any]:
        warnings = list(state.get("warnings", []))
        fallback = LocalDemoProvider()
        try:
            answer = provider.answer(state)
            used = provider.name
        except Exception as error:  # noqa: BLE001 - 任何 Provider 异常都回退本地模板
            warnings.append(warning_of(PROVIDER_UNAVAILABLE, f"{provider.name}：{error}"))
            answer = fallback.answer(state)
            used = fallback.name
        blocked = _block_reason(answer, state.get("risk_verdict"))
        if blocked:
            warnings.append(warning_of(PROVIDER_OVERRIDE_BLOCKED, blocked))
            answer = fallback.answer(state)
            used = fallback.name
        return {"final_answer": answer, "provider": used, "warnings": warnings}

    return node


def build_graph(
    services: ServiceContainer,
    provider: AnswerProvider | None = None,
) -> CompiledStateGraph:
    """Compile the LangGraph state graph over the whitelisted tools.

    Returns:
        ``CompiledStateGraph``：``START → route_intent → run_tools →
        check_evidence → explain → END``。
    """
    resolved_provider = provider or build_provider(services.settings)
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("route_intent", _route)
    graph.add_node("run_tools", _run_tools(services))
    graph.add_node("check_evidence", _check_evidence)
    graph.add_node("explain", _explain(resolved_provider))
    graph.add_edge(START, "route_intent")
    graph.add_edge("route_intent", "run_tools")
    graph.add_edge("run_tools", "check_evidence")
    graph.add_edge("check_evidence", "explain")
    graph.add_edge("explain", END)
    return graph.compile()


def run_agent(
    services: ServiceContainer,
    message: str,
    *,
    provider: AnswerProvider | None = None,
    station_id: str | None = None,
    as_of: datetime | None = None,
    horizons: list[int] | None = None,
    model: str = "persistence",
    data_mode: DataMode | None = None,
) -> AgentResponse:
    """Run one agent turn and return the answer with trace, evidence and verdict."""
    graph = build_graph(services, provider)
    state: AgentState = {
        "user_message": message,
        "station_id": station_id or services.settings.station_id,
        "as_of": as_of,
        "horizons": horizons or [1, 3, 6],
        "model": model,
        "data_mode": data_mode or services.settings.data_mode,
    }
    final = graph.invoke(state)
    return _to_response(final, services)


def _to_response(state: AgentState, services: ServiceContainer) -> AgentResponse:
    results = state.get("tool_results", {})
    forecast = (results.get("forecast_water_level") or {}).get("payload", {}).get("result", {})
    history = (results.get("query_water_level_history") or {}).get("payload", {}).get("result", {})
    observed_at = history.get("latest_observed_at") or forecast.get("anchor_at")
    risk_verdict = state.get("risk_verdict")
    evidence_summaries = [
        f"{name}（{entry.get('status')}）："
        f"{(entry.get('payload') or {}).get('summary') or entry.get('error')}"
        for name, entry in results.items()
    ]
    return AgentResponse(
        answer=state.get("final_answer", ""),
        intent=state.get("intent", CLARIFY),
        tool_trace=list(state.get("tool_calls", [])),
        status=state.get("status", "ok"),
        station_id=state.get("station_id", services.settings.station_id),
        data_mode=state.get("data_mode", services.settings.data_mode),
        observed_at=_parse_dt(observed_at),
        model_version=forecast.get("model_version"),
        provider=state.get("provider", "local_demo"),
        risk_verdict=RiskResult.model_validate(risk_verdict) if risk_verdict else None,
        evidence_summaries=evidence_summaries,
        warnings=list(state.get("warnings", [])),
        disclaimer=DISCLAIMER,
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
