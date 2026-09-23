# River Sentinel 前端（DS-8B）

飞云江水位预测与防汛辅助决策智能体系统的前端展示层。使用 Vue 3、TypeScript、Vite 和 ECharts，**只消费 DS-8A 后端接口**，不实现预测、插补、指标公式或风险阈值。

> 本系统为**教学科研辅助**，**不替代官方防汛决策**。所有数据默认来自**历史回放**（`history_replay`）或本地快照（`local_snapshot`），不是实时监测；页面不出现“官方预警”“实时调度指令”等表述。

## 运行

```powershell
# 安装依赖
npm install

# 本地开发（默认 http://127.0.0.1:5173）
npm run dev

# 类型检查
npm run typecheck

# 单元与组件测试
npm run test:run

# 生产构建（含类型检查）
npm run build
```

## 环境变量

复制 `.env.example` 为 `.env.local` 后按需修改（不含任何密钥或凭据）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `/api/v1` | 前端请求的基础路径 |
| `VITE_BACKEND_URL` | `http://127.0.0.1:8000` | 开发代理目标后端地址，仅本地开发使用 |

开发模式下 `/api` 由 Vite 代理到 `VITE_BACKEND_URL`。后端不可用时页面展示统一的不可用/降级状态，不补造任何数值。

## 目录结构

```text
frontend/
├─ src/
│  ├─ api/          # endpoints.ts（路径、请求体构造与时域/枚举净化）、client.ts（fetch 封装与状态映射）
│  │                # adapters.ts（后端 DTO → 展示模型的唯一适配层）
│  ├─ charts/       # waterLevelOption.ts（ECharts option 转换）、renderer.ts（实例初始化）
│  ├─ components/   # DataModeBanner、RiskCard、MetricTable、UnavailableState、WaterLevelChart
│  ├─ stores/       # dashboard.ts（响应、证据、告警与降级状态）
│  ├─ types/        # backend.ts（后端原始 DTO）、api.ts（展示模型）、view.ts（文案与格式化）
│  ├─ utils/        # time.ts（Asia/Shanghai 时区转换）
│  └─ views/        # DashboardView、HistoryView、ForecastView、AgentView
└─ tests/
   ├─ fixtures/backend/  # 后端 TestClient 真实响应快照（契约回归用）
   ├─ helpers/           # 快照加载、展示模型转换与假 API 客户端
   └─ *.test.ts          # 类型、客户端、状态、组件、视图与后端契约测试
```

## 后端契约与适配层

- `src/types/backend.ts` 是后端 `backend/app/schemas/*.py` 的字段镜像，组件不得直接使用；
- `src/api/adapters.ts` 是唯一的 DTO → 展示模型转换点，例如 `water_level_m`、`target_at`、`metrics`、
  `event_metrics[segment][horizon][model]`、`basis` 对象 —— 都只在这里转换一次；
- 关键字段对照：`历史点 water_level_m`、`预测点 target_at / water_level_m`、`对比 metrics[]`、
  `风险 level / basis_source / basis / last_observed_at / anchor_at`、健康取外层 `ApiResponse.status`；
- 请求体由 `src/api/endpoints.ts` 构造并在 `client.ts` 再次净化：枚举字段（`model`、`data_mode`）只发送合法值，
  绝不发送 `null`；风险请求必须带 `water_level` 与 `observed_at`，缺少观测证据时**不发请求**并展示不可用；
- `datetime-local` 输入在发送前附加项目时区 `Asia/Shanghai`（`src/utils/time.ts`），避免浏览器时区漂移。

## 真实响应快照

`tests/fixtures/backend/*.json` 由 `tests/fixtures/backend/capture_contract_snapshots.py` 用后端
`TestClient` 现场抓取（只读，不修改后端、数据与模型产物）：

```powershell
# 在仓库根目录执行（需已安装 fastapi / httpx / river_sentinel_ml）
python frontend/tests/fixtures/backend/capture_contract_snapshots.py
```

脚本不写死机器路径：默认从脚本位置向上查找包含 `backend/app/main.py` 的仓库根目录；
后端尚未合并到本工作树时用环境变量覆盖：

```powershell
$env:RIVER_SENTINEL_BACKEND_ROOT = '<后端仓库根目录>'
python frontend/tests/fixtures/backend/capture_contract_snapshots.py
```

`requests.json` 保存的是前端实际会发送、且已在后端返回 200 的请求体；`tests/backend-contract.test.ts`
会断言前端构造的请求体与其一致。

## 展示口径

- 数据模式只显示“历史回放”或“本地快照”，并标注观测时间、数据新鲜度与模型版本；
- 缺失水位在曲线中保留断点（`connectNulls: false`），**不绘制为 0**；
- 不可用模型显示“不可用/未提供”，不参与排序，不绘制零线；
- `test` 段零事件时事件级指标显示“**事件级指标无法计算**”，**不显示 F1=0**；
- 阈值统一表述为“**研究性高水位阈值（训练集 p90，7.18 m）**”，并标注“**非官方警戒标准**”；
- 后端 503、网络失败、空数据均展示 `UnavailableState`，页面保持可用且不抛未处理异常；
- 每页底部固定显示“**教学科研辅助，不替代官方防汛决策**”。

## 测试范围

`npm run test:run` 覆盖：

1. API 客户端对 `ok`、`degraded`、`unavailable`、`error` 的处理；
2. 503 / 网络失败 / 超时 / 非 JSON 响应的降级映射；
2.1 `tests/backend-contract.test.ts`：以后端真实快照做契约回归（字段映射、事件指标、basis 摘要、请求体）；
3. 陈旧快照与历史回放 banner 的时间、新鲜度、模式展示；
4. 曲线对 `null` 缺失值保留断点；
5. 不可用模型不绘制为零、不参与误导性排序；
6. `test` 段零事件显示“事件级指标无法计算”；
7. 智能体回答保留工具摘要、证据、模型版本与风险依据；
8. 四个视图在空数据和接口失败下仍可渲染。
