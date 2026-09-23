"""Answer providers for the agent (DS-8A).

``LocalDemoProvider`` 是默认 Provider：**无网络、无凭据**，只把白名单工具已经算好的
结构化结果渲染成中文说明，绝不自行生成水位、预测值或风险等级。

``ExternalLLMProvider`` 只是可选扩展：凭据未配置时只能报告不可用，不阻塞本地模式。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..schemas.common import DISCLAIMER, RESEARCH_THRESHOLD, DataMode

DATA_MODE_LABEL = {"history_replay": "历史回放（非实时数据）", "local_snapshot": "本地快照"}

#: 官方口径用词：任何 Provider 输出一旦出现即被拦截并回退本地模板。
FORBIDDEN_TOKENS: tuple[str, ...] = ("警戒", "超警", "预警")


class ProviderError(Exception):
    """Base provider failure."""


class ProviderUnavailableError(ProviderError):
    """Provider is not configured or cannot be reached."""


class AnswerProvider(Protocol):
    """Renders an explanation from the agent state."""

    name: str

    def answer(self, state: Mapping[str, Any]) -> str: ...


def _level_text(value: Any, unit: str = " m") -> str:
    if value is None:
        return "不可用（不补造）"
    return f"{float(value):.3f}{unit}"


def _result_of(state: Mapping[str, Any], tool: str) -> dict[str, Any]:
    entry = state.get("tool_results", {}).get(tool, {})
    payload = entry.get("payload") or {}
    return dict(payload.get("result") or {})


def _summary_of(state: Mapping[str, Any], tool: str) -> str:
    entry = state.get("tool_results", {}).get(tool, {})
    payload = entry.get("payload") or {}
    return str(payload.get("summary") or entry.get("error") or "无摘要")


class LocalDemoProvider:
    """Deterministic template renderer over whitelisted tool results."""

    name = "local_demo"

    def answer(self, state: Mapping[str, Any]) -> str:
        """Render the answer from structured tool results only."""
        intent = str(state.get("intent", "clarify"))
        mode = str(state.get("data_mode", "history_replay"))
        lines = [
            f"【数据模式】{DATA_MODE_LABEL.get(mode, mode)}（{mode}）",
            f"【数据时间】{self._data_time(state)}",
            f"【模型版本】{self._model_version(state)}",
            f"【工具调用摘要】{self._trace(state)}",
        ]
        lines.extend(self._body(state, intent))
        lines.append(f"【判断依据】{self._basis(state, intent)}")
        lines.append(
            "【数值与等级来源】水位、预测值与风险等级均由白名单工具与确定性规则产生，"
            "本解释不修改任何数值。"
        )
        lines.append(f"【免责声明】{DISCLAIMER}")
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _data_time(state: Mapping[str, Any]) -> str:
        observed = _result_of(state, "query_water_level_history").get("latest_observed_at")
        forecast = _result_of(state, "forecast_water_level")
        anchor = forecast.get("as_of") or forecast.get("anchor_at")
        freshness = forecast.get("freshness") or "unknown"
        parts = []
        if observed:
            parts.append(f"最后观测 {observed}")
        if anchor:
            parts.append(f"预测锚点 {anchor}")
        parts.append(f"新鲜度 {freshness}")
        return "；".join(parts) if parts else "未知（工具未返回数据时间）"

    @staticmethod
    def _model_version(state: Mapping[str, Any]) -> str:
        forecast = _result_of(state, "forecast_water_level")
        if forecast:
            return f"{forecast.get('model')} / {forecast.get('model_version')}"
        risk = _result_of(state, "evaluate_research_risk")
        if risk:
            return f"{risk.get('model')} / {risk.get('model_version')}（风险判定）"
        return "无（本次未调用预测工具）"

    @staticmethod
    def _trace(state: Mapping[str, Any]) -> str:
        trace = list(state.get("tool_calls", []))
        if not trace:
            return "未调用任何工具（意图未识别）"
        return "；".join(f"{name} → {_summary_of(state, name)}" for name in trace)

    @staticmethod
    def _body(state: Mapping[str, Any], intent: str) -> list[str]:
        if intent == "clarify":
            return [
                "【澄清】未能识别你的意图。可以尝试：查询最近水位、预测未来 1/3/6 步、"
                "对比模型指标、判断研究性风险、生成水情简报。"
            ]
        if intent == "water_level_history":
            history = _result_of(state, "query_water_level_history")
            points = history.get("points") or []
            latest = points[-1] if points else None
            lines = [
                f"【历史水位】断面 {history.get('station_id', '未知')}，"
                f"返回 {len(points)} 个观测点。"
            ]
            if latest:
                lines.append(
                    f"最新观测 {latest.get('observed_at')} 水位 "
                    f"{_level_text(latest.get('water_level_m'))}。"
                )
            else:
                lines.append("该时间范围内没有可用观测，不补造数据。")
            return lines
        if intent == "forecast":
            forecast = _result_of(state, "forecast_water_level")
            lines = [
                f"【预测】模型 {forecast.get('model')}/{forecast.get('model_version')}"
                f"（请求 {forecast.get('requested_model')}），状态 {forecast.get('status')}。"
            ]
            if forecast.get("fallback_reason"):
                lines.append(
                    f"回退原因：{forecast.get('fallback_reason')}；基线结果不得标注为原请求模型。"
                )
            for point in forecast.get("predictions", []):
                lines.append(
                    f"未来 h{point.get('horizon')}（{point.get('target_at')}）："
                    f"{_level_text(point.get('water_level_m'))}"
                )
            return lines
        if intent == "comparison":
            comparison = _result_of(state, "compare_forecast_models")
            lines = [
                f"【模型对比】指标来源：{comparison.get('source')}；"
                f"状态：{comparison.get('status')}。",
                f"研究性阈值：{_level_text(comparison.get('threshold'))}"
                f"（{comparison.get('threshold_source')}）。",
                f"主对照：{', '.join(comparison.get('primary_comparison', []))}；"
                f"{comparison.get('primary_note', '')}",
            ]
            for cell in comparison.get("metrics", [])[:4]:
                lines.append(
                    f"test h{cell.get('horizon')} {cell.get('display_name')}：样本 "
                    f"{cell.get('sample_count')}，MAE {cell.get('mae')}，"
                    f"RMSE {cell.get('rmse')}，R² {cell.get('r2')}"
                )
            for limitation in (comparison.get("limitations") or [])[:3]:
                lines.append(f"限制：{limitation}")
            return lines
        if intent == "risk":
            risk = _result_of(state, "evaluate_research_risk")
            lines = [
                f"【研究性风险判定】等级 {risk.get('level')}"
                f"（{risk.get('metric_name')}），阈值 "
                f"{_level_text(risk.get('threshold'))}（{risk.get('threshold_source')}）。",
                f"触发时域：{', '.join(f'h{h}' for h in risk.get('triggered_horizons', [])) or '无'}；"
                f"缺失不参评："
                f"{', '.join(f'h{h}' for h in risk.get('missing_horizons', [])) or '无'}。",
                f"等级来源：{risk.get('basis_source')}（确定性规则，Provider 不得修改）。",
                f"数据龄 {risk.get('data_age_hours')} 小时，陈旧标记 {risk.get('is_stale')}。",
                f"阈值 {RESEARCH_THRESHOLD} m 为研究性阈值，非官方标准。",
            ]
            for limitation in (risk.get("limitations") or [])[:2]:
                lines.append(f"限制：{limitation}")
            return lines
        brief = _result_of(state, "build_water_brief")
        lines = [f"【水情简报】{brief.get('title', '简报')}（状态 {brief.get('status')}）。"]
        for highlight in brief.get("highlights", []):
            lines.append(f"要点：{highlight}")
        for section in brief.get("sections", []):
            lines.append(f"— {section.get('heading')}")
            for line in section.get("lines", [])[:4]:
                lines.append(f"　{line}")
        return lines

    @staticmethod
    def _basis(state: Mapping[str, Any], intent: str) -> str:
        if intent == "clarify":
            return "意图未命中白名单，未调用任何工具，因此没有可引用的证据。"
        names = list(state.get("tool_calls", []))
        bases = [_summary_of(state, name) for name in names]
        errors = [
            f"{name} 失败：{state['tool_results'][name].get('error')}"
            for name in names
            if (state.get("tool_results", {}).get(name) or {}).get("error")
        ]
        text = "；".join(bases)
        if errors:
            text = f"{text}；失败项：{'；'.join(errors)}"
        return text or "工具未返回摘要"


class ExternalLLMProvider:
    """Optional OpenAI-compatible provider; unavailable without credentials."""

    name = "external_llm"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        """``True`` only when base URL, model and API key are all configured."""
        return bool(
            self._settings.external_llm_base_url
            and self._settings.external_llm_model
            and self._settings.external_llm_api_key
        )

    def answer(self, state: Mapping[str, Any]) -> str:
        """Call the configured endpoint with structured evidence only.

        Raises:
            ProviderUnavailableError: 未配置凭据或调用失败（调用方回退本地模板）。
        """
        if not self.available:
            raise ProviderUnavailableError(
                "外部 Provider 未配置 RIVER_SENTINEL_EXTERNAL_LLM_BASE_URL / "
                "RIVER_SENTINEL_EXTERNAL_LLM_MODEL / RIVER_SENTINEL_EXTERNAL_LLM_API_KEY"
            )
        base_url = str(self._settings.external_llm_base_url).rstrip("/")
        payload = {
            "model": self._settings.external_llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你只能解释下面给出的结构化工具结果，不得生成或修改水位、预测值、"
                        "观测时间与风险等级；回答必须标注历史回放/本地快照、数据时间、"
                        f"模型版本和免责声明：{DISCLAIMER}"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "intent": state.get("intent"),
                            "tool_calls": state.get("tool_calls", []),
                            "tool_results": state.get("tool_results", {}),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.external_llm_api_key.get_secret_value()}"
        }
        try:
            response = httpx.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._settings.external_llm_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderUnavailableError(f"外部 Provider 调用失败：{error}") from error
        choices = body.get("choices") if isinstance(body, dict) else None
        if not choices:
            raise ProviderUnavailableError("外部 Provider 返回内容为空")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            raise ProviderUnavailableError("外部 Provider 返回内容为空")
        return str(content)


def build_provider(settings: Settings) -> AnswerProvider:
    """Select the provider: external only when configured, local otherwise."""
    if settings.llm_provider == "external":
        external = ExternalLLMProvider(settings)
        if external.available:
            return external
    return LocalDemoProvider()


def provider_available(settings: Settings) -> bool:
    """Whether the configured external provider can be called."""
    return ExternalLLMProvider(settings).available


def render_mode_label(mode: DataMode) -> str:
    """Human-readable data-mode label."""
    return DATA_MODE_LABEL.get(mode, str(mode))
