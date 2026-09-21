# River Sentinel 日尺度预测与开放数据优先方案（历史决策记录，尺度条款已废止）

> **2026-09-21 尺度裁定（用户确认选 A）**：DS-1b 实测目标序列仅 118 个有记录日 / 2026 个有效小时，`iid=6352` 判定为「待确认 / 当前按非同站处理」且全量 BLOCKED，无法延长；日尺度 D+1/D+2/D+3 样本量不足。预测尺度已**改回小时尺度（未来 1/3/6 步）**，现行方案见 `2026-09-21-hourly-scale-redesign.md`。本文件**保留为历史决策记录**：时域、主目标与日尺度命名条款（DS-D-1~5、DS-D-19~21）**不再适用**；数据治理、泄漏约束、指标与风险规则条款继续有效（对应关系见新方案第 6 节）。本文件内容**不作删除或改写**。

- 日期：2026-09-21
- 性质：**已由用户确认（2026-09-21）**。确认前为只读影响分析，未修改任何源码、测试与既有文档；确认后本轮**仅执行 DS-1**，仍未提交、未合并、未推送。
- 本轮范围：只派发 DS-1（开放数据来源核实与登记）；DS-2 及之后待 DS-1 回传后再决定。
- 口径待定项：`min_daily_coverage`、`expected_observations_per_day`、汛期起止**均为待定（TBD）**，本轮不得把 `0.6`、`24`、`04-15~10-15` 写成正式口径，汛期标志特征不启用。
- 触发：原「未来 1/3/6 小时水位预测」改为「未来第 1/2/3 天（D+1/D+2/D+3）每日最高水位预测」，并改为开放数据优先，降低 `API_AUTH_PENDING` 对毕设主线的阻塞。
- 前置事实：`main` = `682673c`，与 `origin/main` 同步；数据基线 Task 1~9 已全部合入；`pytest` 216 passed、覆盖率 96%；MS-1 候选源已登记（4 个）。

## 1. 决策摘要

| 项 | 原方案 | 新方案 |
| --- | --- | --- |
| 预测时域 | 1/3/6 步长（条件性等价小时） | D+1 / D+2 / D+3（自然日） |
| 主预测目标 | 小时网格水位值 | **每日最高水位** `water_level_daily_max` |
| 诊断目标 | — | 日均、日末次、日变幅（仅诊断，不作主目标） |
| 多时域策略 | 持久性 shift | **直接多时域**（D+1/D+2/D+3 各自模型或多输出），禁止递归滚动 |
| 水位数据 | API 分页抓取（`API_AUTH_PENDING` 阻塞） | **平台详情页文件下载**（CSV/JSON/XLS），`file_import`，不可变落 `data/raw/` |
| 降雨数据 | 温州平台雨量站（需凭据） | **Open-Meteo Historical Weather API / ERA5-Land**（无需 appsecret） |
| 流量数据 | 核心增强 | **可选增强**（`cata_12756` 或 GloFAS 模拟流量），不得阻塞主线 |
| `API_AUTH_PENDING` | 主线阻塞 | 降级为「本地官方雨量/流量增强阻塞」，不再阻塞水位+降雨核心闭环 |
| 论文题目 | 不变 | **不变**；正文明确研究目标为 D+1/D+2/D+3 每日最高水位 |

## 2. 当前仓库与 worktree 状态（2026-09-21 实测）

- `main` = `682673c`，工作区**干净**（`git status --short --branch` 仅 `## main...origin/main`），与远端一致；`git stash list` 为空；无未提交改动。
- 本地分支 14 个；worktree 12 个（主目录 + 11 个）。
- **三个 MS 工作树均为空转**：`.worktrees/ms2-stations`（`codex/ms-stations`）、`.worktrees/ms3-open-data`（`codex/ms-open-data`）、`.worktrees/ms4-multisource`（`codex/ms-multisource`），三者 HEAD 均为 `0712215`，**无任何功能提交**（提示词尚未转发）。
- `codex/feiyunjiang-data-baseline`（`a0fa30d`）相对其远端 ahead 2（历史遗留，未推送）。
- 既有基线：`pytest` 216 passed / 96% 覆盖；`ruff check ml`、`ruff format --check ml`、`git diff --check` 全绿。
- 阻塞：`API_AUTH_PENDING`（温州平台接口凭据未获批）。

## 3. 旧小时级口径影响清单（搜索实测）

### 3.1 源码（`ml/src/river_sentinel_ml/`）

| 文件 | 位置 | 旧口径 | 处置建议 |
| --- | --- | --- | --- |
| `pipeline.py` | L51 `DEFAULT_FREQUENCY="1h"`、L53 `DEFAULT_HORIZONS=(1,3,6)`、L107/155/224/546/629 | 小时网格 + 1/3/6 步长持久性 | **保留为小时尺度对照实验**；新增日尺度入口，不动既有默认行为 |
| `baseline.py` | L10 默认 `horizons=(1,3,6)`、L23 `prediction_h{h}` | 小时持久性 | 保留；日尺度另建 `daily_baseline.py`（列名 `prediction_d{k}`） |
| `metrics.py` | `evaluate_forecasts` 按 `prediction_h{h}` 取列 | 小时指标 | 保留；日尺度指标复用其指标定义（R²/NSE 在 `SS_tot==0` 返回 `None`），不动文件 |
| `reporting.py` | L18~28「1/3/6 指步长」文案、L62~83/262~284/363 | 步长文案 | 保留小时报告；新增日尺度报告字段与文案 |
| `contracts.py` | L103/127~131 `ExperimentManifest.horizons` 正整数校验 | 通用 | 日尺度新增字段（如 `lead_days`、`target_variable`），不修改既有校验 |
| `dataset.py` | L51 `DEFAULT_FREQUENCY="1h"`、`resample_water_level` | 小时重采样 | 保留（仍是小时→日的预处理路径），日聚合在其之上 |
| `cli.py` | L143「预测步长列表，单位为步长而非小时」 | 小时 | 新增日尺度子命令，不改写既有帮助文本语义 |

### 3.2 测试（`ml/tests/`，既有 216 项**禁止修改或删除**）

| 文件 | 命中位置 |
| --- | --- |
| `test_pipeline.py` | L202 `manifest.horizons == (1,3,6)`、L228~236 |
| `test_metrics.py` | L13~148（含 `test_default_horizons_cover_one_three_and_six`） |
| `test_contracts.py` | L41 `horizons:(1,3,6)`、L50 `horizon_steps:6`、L184、L191~198 |
| `test_dataset.py` | L512 `persistence_forecast(series, horizons=(1,2))` |

### 3.3 文档

| 文件 | 命中位置 |
| --- | --- |
| `AGENTS.md` | L14「预测目标优先为未来 1、3、6 小时」 |
| `README.md` | 「当前里程碑」与 CLI 用法段落（小时基线描述） |
| `docs/superpowers/specs/2026-09-16-river-sentinel-project-design.md` | L12、L24、L69 |
| `docs/superpowers/plans/2026-09-18-feiyunjiang-data-baseline.md` | L5、L279、L562~567、L598、L639、L683、L721、L765、L795 |
| `docs/superpowers/plans/2026-09-17-agents-workflow-config.md` | L17「1/3/6-hour water-level forecasting」 |
| `docs/superpowers/plans/2026-09-21-multisource-matching.md` | L18、D-9（L72）等 |
| `docs/experiments/data-baseline-acceptance.md` | L52~55、L62~68（裁定 1）、L102 |
| `docs/experiments/baseline-report-format.md` | L47、L51、L83 |

**结论**：小时级管线不废弃，降级为「日内预处理 + 小时尺度对照实验」；日尺度以**新增模块**方式并列，既有 216 项断言零改动（符合「不得修改既有测试」纪律）。

## 4. 与现行 MS 计划的关系（重要冲突提示）

MS 计划（2026-09-21）为**小时级多站融合**设计，与新主线存在语义重叠：

| MS 项 | 冲突/重叠 | 建议处置 |
| --- | --- | --- |
| MS-2 `stations.py` | 依赖候选站身份确认，现仍全为「待确认」 | **暂缓**，降为日尺度可选增强（网格点/站点关系后置） |
| MS-3 `ingestion/open_data.py` | 与 DS 主线的参数化客户端能力重叠，但服务的是需凭据的数据源 | **暂缓**；其「参数化 `dataset_id` + 密钥零回显」契约在需要时并入 |
| MS-4 `multisource.py` 小时对齐 | 与 DS-4 日对齐语义分裂，两套管法会冲突 | **暂缓**；日对齐以 DS-4 为准 |

**建议：暂停转发 MS-2/3/4 三份提示词**，先跑 DS 主线；待 DS 闭环后按新口径重写 MS 计划（时滞改日滞后、准入阈值改日可用性口径）。

**决定（2026-09-21，用户确认）**：MS-2/3/4 **暂停**，保留三棵工作树（`.worktrees/ms2-stations`、`.worktrees/ms3-open-data`、`.worktrees/ms4-multisource`）与三份提示词，等待本地官方雨量/流量凭据到位；不占当前主线，本轮不派发、不清理、不删除。

## 5. 冻结设计决策（DS-D-1 ~ DS-D-20）

- **DS-D-1 时域定义**：D+1/D+2/D+3 指**自然日**（`Asia/Shanghai`），不是 24/48/72 小时滚动；发布时点为当日结束（次日 00:00）之后。
- **DS-D-2 主目标**：`water_level_daily_max`（当日最高有效水位）；`water_level_daily_mean` / `_last` / `_range` 仅作诊断指标，不作主目标、不参与模型选择主指标。
- **DS-D-3 多时域策略**：直接多时域；可为每个 lead day 单独训练，或一个多输出模型；**禁止递归滚动 3 次**。
- **DS-D-4 持久性基线口径固定**：`persistence_anchor` 取 `daily_max`（默认）或 `daily_last`，写入实验配置；理由：与主目标同口径。`D+k` 预测值 = 发布日 anchor 值。
- **DS-D-5 日可用性（阈值待定）**：当日有效观测覆盖率低于阈值 → 标记 `is_day_usable=False`，标签置 NaN，**不插补、不参与训练与评估**。`min_daily_coverage` 与 `expected_observations_per_day` **均为待定（TBD，2026-09-21 决定）**，须由 DS-1/DS-2 的真实文件、实测采样频率与官方依据确定并记录理由；**本轮禁止把 `0.6` / `24` 写成正式口径**，实现上不设默认值、缺参即 `ValueError`。
- **DS-D-6 重复时间**：同一时间戳多条观测取**末次**（与既有「桶内最后观测」一致），并计数 `duplicate_count`；原始行不被删除。
- **DS-D-7 非有限值与异常值**：`NaN`/`±inf` 排除并计数；异常值**只标记不删除**，阈值由**训练集**分位数拟合（默认 IQR 或 1%/99% 分位，配置化），不跨段、不使用全数据集。
- **DS-D-8 水位主源**：`cata_12720` 详情页可下载文件（CSV/JSON/XLS），采集方式 `file_import`，落入 `data/raw/` 即不可变；登记来源 URL、下载时间、文件大小、SHA-256；展示一律标注「历史数据 / 历史回放」，**不得称实时监测**；文件不可得 → **停止正式模型实验并报告阻塞，禁止伪造水位数据**。
- **DS-D-9 降雨主源**：Open-Meteo Historical Weather API（`precipitation`、`rain`），时区 `Asia/Shanghai`；**先落原始小时快照**，再聚合为日尺度；必须记录 Open-Meteo、ERA5-Land、分辨率与「再分析数据」属性；明确标注「网格化再分析降雨，不是文成县地面雨量站实测值」。
- **DS-D-10 坐标不猜测**：目标断面坐标未知时**禁止假定坐标**；使用经核实的**文成县行政边界**内若干固定网格点，计算区域平均，标注「文成县区域降雨代理变量」；网格点清单固定并登记来源与核实依据。
- **DS-D-11 可选源**：NASA GPM IMERG（半小时、约 0.1°）作第二降雨源，仅用于对比/消融，非首期必需；`cata_12756` 流量与 GloFAS 日尺度模拟流量为**可选增强**，GloFAS 不得描述为飞云江本地测站实测流量；缺流量不阻塞水位+降雨核心闭环。
- **DS-D-12 泄漏约束（硬）**：允许使用历史水位滞后 1/2/3/7/14 天、滚动均值/最大值/变化率（仅历史）、截至发布时点的历史降雨、前 1/2/3/7 日累计降雨、月份/季节标志（**汛期标志本轮不启用**，待官方口径依据，见 DS-D-21）。**禁止**：D+1~D+3 实际降雨、全数据集归一化或插补统计量、测试期参与站点/特征/超参选择、未来水位/流量、事后修订数据。
- **DS-D-13 拟合范围**：所有标准化、插补、特征统计、阈值只在**训练集**拟合，以显式 fit/transform 分离实现，禁止先全量后切分。
- **DS-D-14 切分**：按**日**时间顺序划分训练/验证/测试（沿用 0.70/0.15/0.15 口径，可配置），不 shuffle，段间不重叠；特征窗口与标签**不得跨段**。
- **DS-D-15 模型与实验**：至少比较 ① 持久性基线 ② 统计/线性基线 ③ XGBoost 或 LightGBM ④ LSTM 或 TCN；至少两组实验：**仅历史水位**、**历史水位 + 开放降雨**；可选第三组（+IMERG 或 +GloFAS）。
- **DS-D-16 指标**：三个时域分别报告 MAE、RMSE、R²、NSE；高水位事件报告 Precision、Recall、F1、提前天数。**无官方警戒水位时只能用训练集分位数定义「研究性高水位事件」**，并显著标注其不是官方预警标准。
- **DS-D-17 天气预报隔离**：若将来接入真实天气预报，必须作为**独立数据源与独立实验**，不得把历史实况降雨伪装成预测时可得数据。
- **DS-D-18 智能体与风险规则**：风险等级由确定性规则产生，语言模型不得生成或修改；回答须显示历史回放/预测结果、数据来源与获取时间、预测时域、模型版本、是否使用降雨特征、数据新鲜度、判断依据与工具调用摘要，并标注「教学科研辅助，不替代官方防汛决策」。
- **DS-D-19 旧口径保留方式**：1/3/6 步长保留为**小时尺度对照实验**；正文与文档明确主线目标是 D+1/D+2/D+3 每日最高水位；题目不变。
- **DS-D-20 命名隔离**：日尺度列名用 `prediction_d1/d2/d3`、配置项用 `lead_days`，与小时尺度 `prediction_h{h}` / `horizons` **并存不复用**，避免契约混淆。
- **DS-D-21 待定口径（2026-09-21 决定）**：`min_daily_coverage`、`expected_observations_per_day`、汛期起止日期三项**均为待定（TBD）**，须由 DS-1/DS-2 的真实文件与实测采样分布、以及官方口径依据共同确定并记录理由；在此之前禁止写入正式值，汛期标志特征不启用。

## 6. 分层影响

| 层 | 影响 |
| --- | --- |
| 数据 | `data/raw/` 新增官方水位文件与 Open-Meteo 小时快照（不可变，被 `.gitignore` 忽略）；`data/metadata/` 新增网格点清单；日聚合产物写入 `data/interim|processed/` 或 run 目录 |
| API / 外部 | 新增 Open-Meteo 只读客户端（httpx，transport 可注入，便于测试）；温州平台改为**文件下载**路径，接口抓取降为可选 |
| 配置 | 新增日尺度配置：`timezone=Asia/Shanghai`、`lead_days=(1,2,3)`、`target_variable=water_level_daily_max`、`persistence_anchor`、网格点清单路径、降雨变量；`min_daily_coverage`、`expected_observations_per_day`、汛期起止 **待定（TBD）**，见 DS-D-21 |
| 契约 | 新增日聚合契约（DS-2/DS-3）、特征与标签契约（DS-4）、日尺度评估契约（DS-5）；既有 `ExperimentManifest`、`ForecastMetrics` 不修改 |
| 模型 | 阶段 3 引入：持久性、线性/统计、XGBoost/LightGBM、LSTM/TCN；种子固定、实验可复现、失败实验保留 |
| 后端/智能体 | 阶段 3~4：预测服务返回时域与模型版本；风险规则确定性；智能体展示字段按 DS-D-18 |
| 前端 | 每日最高水位时序、D+1/D+2/D+3 预测、三类模型对比、日降雨与累计降雨、误差与高水位事件、来源与「历史回放」标识 |
| 论文 | 题目不变；摘要/绪论/方法论须写明 D+1~D+3 每日最高水位、ERA5-Land 代理变量性质、泄漏控制、无官方阈值时的研究性事件定义 |

## 7. 里程碑与依赖

```text
DS-1 核实并登记开放数据来源（水位文件 + Open-Meteo + 文成网格点）
        ↓
DS-2 官方水位文件导入 + 日聚合   ‖   DS-3 Open-Meteo/ERA5-Land 降雨获取 + 日聚合
        ↓                                   ↓
        └──────────────→ DS-4 时间对齐 + 无泄漏特征 ‖ DS-5 D+1/D+2/D+3 基线（依赖 DS-2）
                                    ↓
                              DS-6 XGBoost/LightGBM 与 LSTM/TCN
                                    ↓
                      DS-7 实验、误差分析、高水位事件与风险规则
                                    ↓
                        DS-8 智能体与前端展示接入
```

| 里程碑 | 允许新增（建议） | 依赖 | 可并行 |
| --- | --- | --- | --- |
| DS-1 | 文档 + `data/metadata/wencheng_grid_points.csv` + `data/metadata/README.md` | 无 | 是（人工/文档） |
| DS-2 | `ingestion/official_file.py`、`daily.py`、对应测试 | DS-1（文件格式） | 与 DS-3 并行 |
| DS-3 | `ingestion/open_meteo.py`、`rainfall_daily.py`、对应测试 | DS-1（网格点） | 与 DS-2 并行 |
| DS-4 | `features.py`、对应测试 | DS-2、DS-3 | 与 DS-5 并行 |
| DS-5 | `daily_baseline.py`、对应测试 | DS-2 | 与 DS-4 并行 |
| DS-6 | `models/`（训练与评估脚本，非权重） | DS-4、DS-5 | 否 |
| DS-7 | 实验产物目录、`docs/experiments/` 报告、风险规则 | DS-6 | 否 |
| DS-8 | `backend/`、`frontend/` 接入 | DS-7 | 否 |

**本轮派发范围（2026-09-21 用户决定）**：**仅 DS-1**。DS-2/DS-3/DS-4 提示词已生成并按 DS-D-21 同步修订（去除 `0.6` / `24` / `04-15~10-15` 默认值），但**暂不派发**，等 DS-1 回传的真实字段、格式与采样事实确认后再决定。DS-5~DS-8 待 DS-2~DS-4 实测契约产出后编制，避免把接口假设写进提示词（沿用既有纪律）。

## 8. 验收命令（每次任务前后新鲜执行）

```powershell
$env:PYTHONPATH='F:\code\engine\ml\src'
python -m pytest -q                 # 必须 216 + N，0 failed / 0 error / 0 skipped
python -m ruff check ml
python -m ruff format --check ml
git diff --check                    # 退出码 0
git status --short --branch         # 仅预期新增文件
```

日尺度专项验收（任务级）：

1. 日聚合手算核对：指定日期的 max/mean/last/range/valid_count 与人工计算一致；
2. 重复时间戳取末次；非有限值被排除并计数；不可用日标签为 NaN 且 `is_day_usable=False`；
3. **泄漏探针**：把 D+1~D+3 当日降雨列整体置 NaN，特征矩阵与预测结果不得改变（证明未使用未来降雨）；
4. 切分严格时序、不重不漏、不 shuffle；窗口不跨段；
5. 归一化/插补统计量仅在训练集拟合（探针：改变测试集取值，训练集拟合结果不变）；
6. 产物确定性：同一输入两次运行产物逐字节一致；
7. 严格 JSON：`python -c "import json;json.load(open(...))"` 通过，非有限值写 `null`。

## 9. 停止条件

1. **`cata_12720` 详情页无法下载到任何文件** → DS-2 立即停止正式模型实验，报告阻塞并给出证据（页面状态、尝试记录），**禁止使用合成或伪造水位数据代替**；
2. **可用日数不足**（可用日 < 30 天或测试段可用日 < 10 天，阈值需据真实分布确定并记录理由）→ 停止建模，报告数据不足；
3. 既有 216 项测试数量下降或断言被修改 → 停止合并，返工；
4. 发现测试期信息参与站点/特征/超参选择 → 整体返工，结果作废重跑；
5. 出现把 ERA5-Land / GloFAS 描述为本地实测、或把历史降雨称为预报 → 文案返工；
6. 官方警戒水位未获得时擅自给出「超警」结论 → 返工并降级为研究性事件表述。

## 10. 风险与遗留

1. `cata_12720` 文件是否可下载、格式与字段未知——**最大不确定项**，DS-1 必须先核实；
2. ERA5-Land 是再分析网格数据，与地面雨量站存在系统性偏差，必须显著标注并写入局限性；
3. 日尺度样本量远小于小时尺度（天数 vs 小时数），深度学习模型易过拟合，需早停与严格验证集；
4. 目标断面坐标未知，降雨为「文成县区域代理变量」，断面代表性有限；
5. `min_daily_coverage`、`expected_observations_per_day`、汛期起止均为**待定**（DS-D-21），须由 DS-1/DS-2 的真实数据与官方依据确定；在此之前不得写入正式结论，汛期标志特征不启用；
6. 三个 MS 工作树空转，需在新口径下重写计划后再决定去留；
7. 毕业材料目录（阶段 5）仍未建立，时间敏感。
