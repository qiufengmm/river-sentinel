"""LangGraph 智能体：白名单、工具轨迹、Provider 回退与风险不可覆盖（DS-8A）。

覆盖点：工具白名单、tool trace、Provider 回退、风险等级不可被 Provider 覆盖。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph.state import CompiledStateGraph

from backend.app.agent.graph import (
    CLARIFY,
    INTENT_PLANS,
    TOOL_ARGUMENTS,
    build_graph,
    classify,
    run_agent,
)
from backend.app.agent.providers import LocalDemoProvider, ProviderUnavailableError
from backend.app.agent.tools import TOOL_REGISTRY, WHITELISTED_TOOLS
from backend.app.services.container import ServiceContainer


class OverridingProvider:
    """尝试改写风险等级并使用官方口径的 Provider（必须被拦截）。"""

    name = "override_demo"

    def answer(self, state: dict[str, Any]) -> str:
        return "经我判断，风险等级提升为 elevated，请立即发布红色预警并转移群众。"


class FailingProvider:
    """直接抛异常的 Provider（必须回退本地模板）。"""

    name = "failing_demo"

    def answer(self, state: dict[str, Any]) -> str:
        raise RuntimeError("provider boom")


def test_whitelist_is_fixed_and_registered() -> None:
    assert set(WHITELISTED_TOOLS) == {
        "query_water_level_history",
        "forecast_water_level",
        "compare_forecast_models",
        "evaluate_research_risk",
        "build_water_brief",
    }
    assert set(TOOL_REGISTRY) == set(WHITELISTED_TOOLS)
    assert set(TOOL_ARGUMENTS) == set(WHITELISTED_TOOLS)


def test_every_intent_only_uses_whitelisted_tools() -> None:
    for intent, plan in INTENT_PLANS.items():
        for tool in plan:
            assert tool in WHITELISTED_TOOLS, f"{intent} 使用了非白名单工具 {tool}"
    assert "unknown_tool" not in TOOL_REGISTRY


def test_intent_classification() -> None:
    assert classify("查询最近水位") == "water_level_history"
    assert classify("请预测未来 3 小时水位") == "forecast"
    assert classify("对比模型指标") == "comparison"
    assert classify("判断研究性风险") == "risk"
    assert classify("生成水情简报") == "brief"
    assert classify("今天天气怎么样") == CLARIFY


def test_build_graph_returns_compiled_graph(container: ServiceContainer) -> None:
    graph = build_graph(container)
    assert isinstance(graph, CompiledStateGraph)


def test_unrecognized_intent_returns_clarification(container: ServiceContainer) -> None:
    response = run_agent(container, "今天天气怎么样")
    assert response.intent == CLARIFY
    assert response.tool_trace == []
    assert "未能识别" in response.answer
    assert any("CLARIFY_INTENT" in item for item in response.warnings)


def test_risk_intent_runs_whitelisted_tools(container: ServiceContainer) -> None:
    response = run_agent(container, "请判断研究性风险")
    assert response.intent == "risk"
    assert response.tool_trace == ["forecast_water_level", "evaluate_research_risk"]
    assert set(response.tool_trace) <= set(WHITELISTED_TOOLS)


def test_answer_contains_required_evidence_fields(container: ServiceContainer) -> None:
    response = run_agent(container, "请判断研究性风险")
    for fragment in ("历史回放", "数据时间", "模型版本", "工具调用摘要", "判断依据"):
        assert fragment in response.answer, fragment
    assert "不替代官方防汛决策" in response.answer
    assert response.provider == "local_demo"


def test_risk_level_cannot_be_overridden_by_provider(container: ServiceContainer) -> None:
    baseline = run_agent(container, "请判断研究性风险")
    overridden = run_agent(container, "请判断研究性风险", provider=OverridingProvider())

    assert baseline.risk_verdict is not None and overridden.risk_verdict is not None
    assert overridden.risk_verdict.level == baseline.risk_verdict.level
    assert overridden.risk_verdict.basis_source == "deterministic_rule"
    assert any("PROVIDER_OVERRIDE_BLOCKED" in item for item in overridden.warnings)
    assert "elevated" not in overridden.answer
    assert "红色预警" not in overridden.answer
    assert overridden.answer.startswith("【数据模式】")


def test_provider_failure_falls_back_to_local_template(container: ServiceContainer) -> None:
    response = run_agent(container, "请预测未来 3 小时水位", provider=FailingProvider())
    assert any("PROVIDER_UNAVAILABLE" in item for item in response.warnings)
    assert response.provider == "local_demo"
    assert "不替代官方防汛决策" in response.answer
    assert "【预测】" in response.answer


def test_local_provider_works_without_credentials(container: ServiceContainer) -> None:
    provider = LocalDemoProvider()
    answer = provider.answer({"data_mode": "history_replay", "tool_calls": []})
    assert "历史回放" in answer
    assert "不替代官方防汛决策" in answer


def test_brief_intent_runs_full_chain(container: ServiceContainer) -> None:
    response = run_agent(container, "生成水情简报")
    assert response.intent == "brief"
    assert response.tool_trace == [
        "query_water_level_history",
        "forecast_water_level",
        "evaluate_research_risk",
        "build_water_brief",
    ]
    assert response.risk_verdict is not None


def test_comparison_intent_reports_published_source(container: ServiceContainer) -> None:
    response = run_agent(container, "对比模型指标")
    assert response.tool_trace == ["compare_forecast_models"]
    assert "hourly-error-and-events.md" in response.answer
    assert response.status == "degraded"


def test_external_provider_unavailable_without_credentials(settings) -> None:
    from backend.app.agent.providers import ExternalLLMProvider  # noqa: PLC0415

    provider = ExternalLLMProvider(settings.model_copy(update={"llm_provider": "external"}))
    assert provider.available is False
    try:
        provider.answer({})
    except ProviderUnavailableError as error:
        assert "RIVER_SENTINEL_EXTERNAL_LLM" in str(error)
    else:  # pragma: no cover
        raise AssertionError("外部 Provider 未配置时应抛出 ProviderUnavailableError")
