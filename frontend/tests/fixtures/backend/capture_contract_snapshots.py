"""Capture real DS-8A backend responses as frontend contract fixtures.

用途：用后端 ``TestClient`` 真实调用 DS-8A 接口，把响应原文保存为前端契约回归
fixture（``*.json``），并把前端实际会发送的请求体保存为 ``requests.json``。
脚本只读取后端代码与仓库内小型样例，不修改后端、数据或模型产物。

运行（在仓库根目录执行；需已安装 fastapi / httpx / river_sentinel_ml）：

```powershell
python frontend/tests/fixtures/backend/capture_contract_snapshots.py
```

后端根目录的解析顺序：

1. 环境变量 ``RIVER_SENTINEL_BACKEND_ROOT``（用于前端独立工作树在后端尚未合并时抓取快照）；
2. 否则从脚本位置向上逐级查找包含 ``backend/app/main.py`` 的仓库根目录；
3. 都找不到时报错退出，提示先合并后端或设置环境变量，不使用任何机器特定路径。

生成的 fixture 在前端测试 ``tests/backend-contract.test.ts`` 中被消费：前端请求体
必须与 ``requests.json`` 一致，前端展示层必须能正确渲染真实响应字段。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

BACKEND_ROOT_ENV = "RIVER_SENTINEL_BACKEND_ROOT"
BACKEND_MARKER = Path("backend") / "app" / "main.py"
OUTPUT_DIR = Path(__file__).resolve().parent
PREFIX = "/api/v1"


def _default_backend_root() -> Path | None:
    """从脚本所在目录向上逐级查找包含 backend/app/main.py 的仓库根目录。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / BACKEND_MARKER).is_file():
            return parent
    return None


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"[contract-snapshots] {message}")


def resolve_backend_root() -> Path:
    override = os.environ.get(BACKEND_ROOT_ENV, "").strip()
    if override:
        root = Path(override).expanduser()
        if not (root / BACKEND_MARKER).is_file():
            _fail(
                f"环境变量 {BACKEND_ROOT_ENV}={root} 下未找到 {BACKEND_MARKER.as_posix()}，"
                "请指向包含 backend/app/main.py 的仓库根目录。"
            )
        return root.resolve()

    root = _default_backend_root()
    if root is None:
        _fail(
            f"未在脚本所在目录的任何上级目录找到 {BACKEND_MARKER.as_posix()}。\n"
            "  请先合并 DS-8A 后端到本仓库，或设置环境变量覆盖后端根目录：\n"
            "    PowerShell: $env:RIVER_SENTINEL_BACKEND_ROOT = '<后端仓库根目录>'\n"
            "    bash:       export RIVER_SENTINEL_BACKEND_ROOT='<后端仓库根目录>'"
        )
    return root


BACKEND_ROOT = resolve_backend_root()
print(f"[contract-snapshots] backend root: {BACKEND_ROOT}")

sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import create_app  # noqa: E402

STATION_ID = "cata_12720"
HORIZONS = [1, 3, 6]

# 与前端 DashboardStore / ApiClient 实际发送的请求体保持一致。
FORECAST_REQUEST: dict[str, Any] = {
    "station_id": STATION_ID,
    "horizons": HORIZONS,
    "model": "persistence",
    "data_mode": "history_replay",
}
FORECAST_DEGRADED_REQUEST: dict[str, Any] = {
    **FORECAST_REQUEST,
    "model": "xgboost",
}
AGENT_REQUEST: dict[str, Any] = {
    "message": "当前是否达到研究性高水位？",
    "station_id": STATION_ID,
    "horizons": HORIZONS,
    "model": "persistence",
    "data_mode": "history_replay",
}
BRIEF_REQUEST: dict[str, Any] = {
    "station_id": STATION_ID,
    "horizons": HORIZONS,
    "model": "persistence",
    "data_mode": "history_replay",
}


def write_json(name: str, payload: Any) -> None:
    path = OUTPUT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> int:
    with TestClient(create_app()) as client:
        health = client.get(f"{PREFIX}/health")
        assert health.status_code == 200, health.text
        write_json("health.json", health.json())

        history = client.get(f"{PREFIX}/water-level/history", params={"station_id": STATION_ID})
        assert history.status_code == 200, history.text
        write_json("history.json", history.json())

        empty_range = client.get(
            f"{PREFIX}/water-level/history",
            params={
                "station_id": STATION_ID,
                "start": "2000-01-01T00:00:00+08:00",
                "end": "2000-01-02T00:00:00+08:00",
            },
        )
        assert empty_range.status_code == 200, empty_range.text
        write_json("history-empty.json", empty_range.json())

        forecast = client.post(f"{PREFIX}/forecast", json=FORECAST_REQUEST)
        assert forecast.status_code == 200, forecast.text
        write_json("forecast.json", forecast.json())

        degraded = client.post(f"{PREFIX}/forecast", json=FORECAST_DEGRADED_REQUEST)
        assert degraded.status_code == 200, degraded.text
        assert degraded.json()["status"] == "degraded", degraded.text
        write_json("forecast-degraded.json", degraded.json())

        comparison = client.get(f"{PREFIX}/model-comparison", params={"horizons": "1,3,6"})
        assert comparison.status_code == 200, comparison.text
        write_json("comparison.json", comparison.json())

        # 风险请求：水位与观测时间取真实历史最后一点，预测取真实持久性预测结果。
        points = history.json()["data"]["points"]
        assert points, "history fixture has no point to anchor the risk request"
        latest = points[-1]
        predictions = {
            str(point["horizon"]): point["water_level_m"]
            for point in forecast.json()["data"]["predictions"]
            if point["water_level_m"] is not None
        }
        risk_request: dict[str, Any] = {
            "station_id": STATION_ID,
            "water_level": latest["water_level_m"],
            "observed_at": latest["observed_at"],
            "horizons": HORIZONS,
            "predictions": predictions,
            "model": forecast.json()["data"]["model"],
            "model_version": forecast.json()["data"]["model_version"],
            "data_mode": "history_replay",
        }
        risk = client.post(f"{PREFIX}/risk/evaluate", json=risk_request)
        assert risk.status_code == 200, risk.text
        write_json("risk.json", risk.json())

        agent = client.post(f"{PREFIX}/agent/chat", json=AGENT_REQUEST)
        assert agent.status_code == 200, agent.text
        write_json("agent.json", agent.json())

        brief = client.post(f"{PREFIX}/brief", json=BRIEF_REQUEST)
        assert brief.status_code == 200, brief.text
        write_json("brief.json", brief.json())

        write_json(
            "requests.json",
            {
                "forecast": FORECAST_REQUEST,
                "forecast_degraded": FORECAST_DEGRADED_REQUEST,
                "risk": risk_request,
                "agent": AGENT_REQUEST,
                "brief": BRIEF_REQUEST,
                "history_query": {"station_id": STATION_ID},
                "comparison_query": {"horizons": "1,3,6"},
            },
        )
    print("contract snapshots captured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
