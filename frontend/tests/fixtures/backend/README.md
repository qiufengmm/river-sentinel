# 后端真实响应快照（契约回归 fixture）

本目录下的 JSON 由后端 `TestClient` 现场抓取，**代表 DS-8A 后端实际契约**，不使用自造字段。

生成方式（只读后端代码与仓库内小型样例，不修改后端、数据、模型产物）：

```powershell
# 在仓库根目录执行；需已安装 fastapi / httpx 与 river_sentinel_ml
python frontend/tests/fixtures/backend/capture_contract_snapshots.py
```

脚本不含任何机器特定路径，后端根目录按以下顺序解析：

1. 环境变量 `RIVER_SENTINEL_BACKEND_ROOT`（指向包含 `backend/app/main.py` 的仓库根目录），
   用于前端独立工作树在后端尚未合并时抓取快照：

   ```powershell
   $env:RIVER_SENTINEL_BACKEND_ROOT = '<后端仓库根目录>'
   python frontend/tests/fixtures/backend/capture_contract_snapshots.py
   ```

2. 未设置环境变量时，从脚本位置（`frontend/tests/fixtures/backend`）向上逐级查找
   包含 `backend/app/main.py` 的仓库根目录 —— 后端合并进 `main` 后无需任何额外配置；
3. 两者都不可用时脚本报错退出并说明原因，不会静默回落到个人路径。

## 文件清单

| 文件 | 来源接口 | 说明 |
| --- | --- | --- |
| `health.json` | `GET /api/v1/health` | 外层 `status=ok`，数据体为 `sample_available` 等可用性字段 |
| `history.json` | `GET /api/v1/water-level/history` | 历史点字段为 `water_level_m` |
| `history-empty.json` | 同上（2000 年区间） | 外层 `status=unavailable`，用于空数据分支 |
| `forecast.json` | `POST /api/v1/forecast`（persistence） | 预测点字段为 `target_at` / `water_level_m` |
| `forecast-degraded.json` | 同上（请求 xgboost） | 外层 `status=degraded`，回退 `persistence/ds5b-hourly-baseline-1` |
| `comparison.json` | `GET /api/v1/model-comparison` | 回归指标为 `metrics[]`；事件指标为 `event_metrics[segment][h{n}][model]` |
| `risk.json` | `POST /api/v1/risk/evaluate` | `basis` 为对象；**没有** `computable` 字段 |
| `agent.json` | `POST /api/v1/agent/chat` | 保留 `tool_trace`、`evidence_summaries`、`risk_verdict` |
| `brief.json` | `POST /api/v1/brief` | 简报 sections |
| `requests.json` | 前端实际发送的请求体 | 这些请求体在后端真实接口上返回 200，供 `backend-contract.test.ts` 对比 |

## 使用约定

- 快照只经 `tests/helpers/backend-snapshots.ts` 加载，并由 `src/api/adapters.ts` 转换为展示模型；
- 缺失值、陈旧快照、不可用模型等展示分支通过 `cloneSnapshot()` / `staleHistoryResponse()` 在真实快照上做
  **明确标注的脱敏变体**，不得新增另一套自造 fixture；
- 字段变更时先重跑抓取脚本，再修适配层与测试，禁止改后端迁就前端。
