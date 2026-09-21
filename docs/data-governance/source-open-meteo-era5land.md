# 数据源治理记录：Open-Meteo Historical Weather API（Best Match 网格化再分析降雨）

> **命名说明**：文件名 `source-open-meteo-era5land.md` 沿用登记初期暂用名（DS-1 分支已建同名文件，
> 改名会加剧合并冲突），**不代表本轮数据口径**。实测口径见第 8 节：本轮数据为 **Open-Meteo
> Best Match** 网格化再分析降雨，**不是 ERA5-Land**，也不是地面雨量站实测值。
>
> 本文件由 DS-3 任务在本工作树新建：基线提交 `682673c` 尚不含该文件（`main` 未合并 DS-1），
> 因此按 DS-3 工作树提示词第 3 节「若不存在则新建同名文件」处理，后续以 DS-1 回执为准合并。
> 无法从响应或来源页面确认的字段一律写 **「待确认」**，禁止推测、禁止把再分析值说成实测值。
> 本系统仅用于教学科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。
>
> **合并说明**：本文件合并 DS-1 登记版与 DS-3b 实测版——第 1～11 节为 DS-3b 实测结论与口径修订（含查询参数名 models 修订、Best Match 口径），第 12 节保留 DS-1 的登记探针、平台文档声明与网格点元数据引用，两部分实测对象不同（3 天探针 vs 2015—2026 全量归档）。

## 1. 数据集标识

| 项 | 值 |
| --- | --- |
| 数据集名称 | Open-Meteo Historical Weather API（Archive / Historical Weather） |
| 数据集 ID / 标识 | `open-meteo-archive-hourly`（本仓库自拟标识；该平台不提供 `iid`） |
| 接口目录 | `v1/archive`（非 `cata_` 前缀，照实记录） |
| 来源页面 | <https://open-meteo.com/en/docs/historical-weather-api> |
| 接口基础地址 | `https://archive-api.open-meteo.com/v1/archive` |
| 提供单位 | Open-Meteo（开源气象 API 服务）；底层再分析数据集由 API 按 **Best Match** 组合，**响应不声明具体构成 → 待确认** |
| 覆盖区域 | 全球网格；文成县以 5 个已冻结网格点代理（见第 7 节） |
| 主题类别 | `rainfall` |
| 检索关键词 | `Open-Meteo`、`Historical Weather API`、`hourly precipitation` |
| 登记日期 | 2026-09-21（2026-09-21 返工修订口径） |

## 2. 接口与字段

请求参数（本仓库 `OpenMeteoClient` 实际发送）：

| 参数 | 含义 | 本轮取值 |
| --- | --- | --- |
| `latitude` / `longitude` | 请求坐标（WGS-84） | 5 个冻结网格点 |
| `start_date` / `end_date` | 闭区间日期，格式 `YYYY-MM-DD` | 按「点 × 年」分块 |
| `hourly` | 小时变量 | `precipitation,rain` |
| `timezone` | 返回时间戳时区 | `Asia/Shanghai` |
| `models` | 数据模型（**复数**；Open-Meteo 实际生效的参数名，见第 8 节） | 本轮不发送该参数（Best Match） |

字段字典（沿用平台官方字段名，本仓库只在规范化帧内做单位后缀重命名）：

| 字段 | 平台标注含义 | 数据类型 | 单位 | 是否含站名/站码 | 下游处理设想 |
| --- | --- | --- | --- | --- | --- |
| `hourly.time` | 本地时间戳 | ISO 8601 字符串（`2015-01-01T00:00`） | — | 否 | 解析为 tz-aware `observed_at`（`Asia/Shanghai`） |
| `hourly.precipitation` | 总降水（含雪等固态） | 浮点 / `null` | mm（小时累计） | 否 | → `precipitation_mm` |
| `hourly.rain` | 液态降水 | 浮点 / `null` | mm（小时累计） | 否 | → `rain_mm` |
| `latitude` / `longitude` / `elevation` | 响应回显的**实际网格单元**中心与高程 | 浮点 | 度 / m | 否 | 写入 `dataset_notes` 作溯源证据 |
| `hourly_units` | 单位声明 | 映射 | — | 否 | 实测为 `precipitation=mm`、`rain=mm`、`time=iso8601` |
| `utc_offset_seconds` | 时区偏移 | 整型 | s | 否 | 实测 `28800`（+08:00） |

口径警示：

- **这是 Open-Meteo Best Match 网格化再分析降雨**（本轮不发送 `models` 查询参数，响应不声明
  具体构成模型 → 待确认），**不是**文成县地面雨量站实测值，不能作为地面雨量站观测的等价替代，
  **也不得**表述为「ERA5-Land 实测」或「ERA5 实测」；
- `precipitation` 与 `rain` 是两个不同口径的变量（总降水 vs 液态降水），本轮**分别落盘、不做合并**，
  实测两者在 513,840 行中有 1,268 行不一致（0.25%），最大绝对差 3.2 mm/小时（详见第 8 节）；
- 响应**不声明** `models` / `resolution`，因此「本次拿到的是不是纯 ERA5-Land」**不可由响应原文确认**（待确认）；
  实测 `models=era5_land` 在本区域**全空**（见第 8 节），故本轮数据更不可能是 ERA5-Land。

## 3. 分页与配额

- 单页上限：不适用（按时间范围一次性返回，非分页接口）；
- 迭代规则：本仓库按「点 × 年」分块，每块一次请求、一份快照；
- 是否限流/配额：公开免费服务，官方声明存在限流；本轮 60 次请求以 1 秒间隔串行执行，
  **实测未触发限流**（无 429、无 5xx、无重试记录，全部 60 块一次成功）；
- 失败处理：脚本最多重试 3 次并按指数退避；**400 / 401 / 403 / 404 / 405 / 422 属于请求本身有问题，
  不重试、立即失败**；失败如实写入 `<root>/open-meteo/fetch-summary.json`，绝不补造数值；
- 断点续跑：只有「小时表存在 **且** 数据行数 > 0 **且** 等于期望小时数 **且** `<stem>.rows.json`
  凭据未过期」才算已完成，否则打印 `[redo]` 并重取（避免空响应被永久判为已完成）。

## 4. 许可与访问审批状态

- 开放类型：**无条件开放**（无需注册、无需审批、**无需任何凭据**）；
- 许可协议：Open-Meteo 官方公开声明其 API 数据可免费用于非商业用途（CC-BY 4.0）；
  **本轮响应体内不含任何许可字段**，条款依据仅为官方站点声明，**待确认**（论文引用前须人工复核官方最新条款）；
- 当前状态：**`API_READY`**（与温州平台 `API_AUTH_PENDING` 无关，互不阻塞）；
- 是否允许用于论文实验：待确认（同上，须以官方最新条款为准）；
- 该源不需要凭据，本仓库亦不接收、不存储、不拼接任何密钥；异常消息只报异常类型与字段名。

## 5. 获取时间与校验值的存放位置

- 原始快照：`<run 目录>/open-meteo/<point_id>/<start_date>_<end_date>/<UTC时间戳>-<sha256前12位>.json`
- 清单：同目录同名 `.manifest.json`，含 `source_name`、`source_uri`、`dataset_id`、`point_id`、
  `acquired_at`、`request_parameters`、`record_count`、`sha256`；`request_parameters` 含
  `models` / `models_explicit` / `model_selection`（区分「未发送 → Best Match」与「显式发送」）
  以及 `sent_query_parameters`（**本次实际进入查询串的键值对原样留痕**）
- 续跑凭据：`<run 目录>/open-meteo/hourly/<point_id>/<start_date>_<end_date>.rows.json`
  （`record_count` / `expected_hours` / `complete`；小时表本身始终是最终权威）
- 快照为严格合法 JSON（非有限值写 `null`；`allow_nan=False`）
- 小时表：`<run 目录>/open-meteo/hourly/<point_id>/<start_date>_<end_date>.csv`（每行一小时，不聚合）
- 运行汇总：`<run 目录>/open-meteo/fetch-summary.json`；质量统计：`<run 目录>/open-meteo/quality-summary.json`
- 实际获取时间：**2026-09-21**（UTC 时间见各清单 `acquired_at` 字段），本次为真实抓取，
  共 60 块（5 点 × 12 个年度块），**成功 60 / 失败 0**；产物 `.gitignore` 已忽略，不入 Git。

## 6. 时间覆盖与更新频率

- 平台声明的更新频率：Historical Weather API 为**历史再分析**，存在发布滞后；
  实测接口错误消息给出的允许范围为 **1940-01-01 至 2026-09-21**；
- 本轮实际覆盖：**2015-01-01T00:00+08:00 至 2026-09-21T23:00+08:00**，5 点各 102,768 行，合计 513,840 行；
- 实时性：**不是实时数据，且不是地面站实测**。末端实测已到当日 23 时，这与 ERA5 通常约 5 天的
  发布滞后不一致，末端小时可能来自 ERA5T 初步再分析或预报补值，**构成待确认**；
  本轮同日重取末端 72 小时完全一致（0 变化行），**是否次日回补变化待跨日复核**；
- 论文与页面必须标注「历史回放 / 再分析」，**不得**表述为实时降雨或地面实测降雨；
- 与目标水位序列的时间重叠情况：**已确认重叠**——DS-2b 实测水位小时序列覆盖 `2022-06-02T18:00+08:00` ~ `2022-10-09T11:00+08:00`（3,090 小时），本降雨数据覆盖 `2015-01-01` ~ `2026-09-21`，完整覆盖水位区间（逐小时对齐由 DS-4b 完成）。

## 7. 网格点身份信息

| 项 | 值 |
| --- | --- |
| 网格点来源 | OSM Nominatim（DS-1 冻结值） |
| 访问日期 | 2026-09-21 |
| 逆向地理编码结果 | **待确认**（本工作树实测 `nominatim.openstreetmap.org` 连接超时，无法自证；以 DS-1 回执为准） |
| 县内确认 | **待确认**（同上） |
| 是否含测站编码 / 名称字段 | 否（该源只有网格点，无测站身份） |
| 是否含经纬度字段 | 是（请求点 + 响应回显网格单元） |
| 与目标断面的上下游关系 | 不适用（降雨为面雨量代理，非河道站点） |
| 推荐时滞（步长） | **待确认**（须由 MS-5 在训练集上估计后人工复核） |

5 个冻结网格点与响应回显的实际网格单元（2015 年度块快照实测）：

| `point_id` | 请求经度 | 请求纬度 | 响应回显经度 | 响应回显纬度 | 响应回显高程 (m) |
| --- | --- | --- | --- | --- | --- |
| `WC-G1` | 120.03 | 27.79 | 120.03371 | 27.732864 | 267.0 |
| `WC-G2` | 119.85 | 27.92 | 119.797295 | 27.87346 | 925.0 |
| `WC-G3` | 120.15 | 27.90 | 120.202705 | 27.87346 | 372.0 |
| `WC-G4` | 120.00 | 27.67 | 120.000000 | 27.662565 | 188.0 |
| `WC-G5` | 120.18 | 27.72 | 120.13483 | 27.732864 | 588.0 |

授权字段（与 DS-3 提示词 4.0 完全一致，未新增）：`point_id`、`longitude`、`latitude`、
`observed_at`、`precipitation_mm`、`rain_mm`。多点区域平均只能作为「文成县区域降雨代理变量」，
**本轮不做任何平均**。

## 8. 模型差异与「毫米/小时精度陷阱」

实测证据（2026-09-21，WC-G1，2020 全年 `hourly=precipitation,rain`）：

| 请求 | 有效降水行数 | 2020 年降水总量 (mm) | 响应回显网格单元 |
| --- | --- | --- | --- |
| 不传模型（Best Match，本轮采用） | 8784 / 8784 | 1341.5 | (27.732864, 120.03371) |
| `models=era5` | 8784 / 8784 | 1186.8 | (27.75, 120.0) |
| `models=era5_land` | **0 / 8784（全空）** | 0（无有效值） | (27.800003, 120.0) |

结论与提示：

1. **契约修订（已于 2026-09-21 返工修复）**：早前冻结的 `model`（单数）查询参数**被 API 静默忽略**
   —— 按契约传 `model=era5_land` 与不传模型的响应完全一致（同一网格单元 27.732864/120.03371、同一数值）。
   Open-Meteo 实际生效的参数名是 **`models`（复数）**。本轮已将
   `OpenMeteoClient.fetch_hourly_rainfall(model=...)` 改为 **`models=...`**，并在单元测试中断言
   发送的键逐字为 `models`、`models=None` 时查询串中 `models` 与 `model` 均不出现。
   修复前：参数被静默忽略（拿到的是 Best Match）；修复后：`models` 被真实发送并生效。
2. **显式 `era5_land` 在本区域无有效降雨数据**（2020 全年 0 有效值；2015、2016-06、2026-09 抽样同样全空）。
   因此「纯 ERA5-Land 实测」在本区域**不可得**，本轮数据（Best Match）不能自称 ERA5-Land。
3. **默认 Best Match 与 `era5` 不同**（2020 年总量 1341.5 vs 1186.8 mm，差约 13%，网格单元也不同），
   说明默认口径不等于 ERA5，更不等于 ERA5-Land；**具体构成响应未声明 → 待确认**。
   论文与页面只能写「Open-Meteo Best Match 网格化再分析降雨」。
4. **`precipitation` 与 `rain` 的差异**：513,840 行中 1,268 行不一致（0.25%），最大绝对差
   **3.2 mm/小时**，首例出现在 `2015-01-13T05:00+08:00`（冬季，与固态降水口径差异一致）；
   逐点不一致行数：`WC-G1` 170、`WC-G2` 523、`WC-G3` 230、`WC-G4` 182、`WC-G5` 163。
5. **毫米/小时精度陷阱**：数值以 mm（小时累计）给出，但来源是约十公里级网格再分析，
   单点小时值的不确定性远大于小数点后一位所暗示的精度。任何页面、论文或智能体回答在展示
   或写入「毫米/小时」数值时，**必须**同时标注来源为「Open-Meteo Best Match 网格化再分析」、
   非地面站实测、**非 ERA5-Land 实测**，且不得把小数位当作有效精度。本轮模块文档字符串、
   清单 `dataset_notes` 与本报告均已固化该口径。

## 9. 质量分析结论（2026-09-21 实测）

| 指标 | 结果 |
| --- | --- |
| 分块数 | 60（5 点 × 12 个年度块），全部成功 |
| 总行数 | 513,840（每点 102,768） |
| 应有小时数 | 513,840（按自然小时计） |
| 缺失小时数 | **0**（缺失率 0.00%） |
| 重复时间戳 | **0** |
| 非有限值（`precipitation_mm` / `rain_mm`） | **0 / 0** |
| 整点对齐 | 全部是 |
| 时区偏移 | 恒为 `+08:00` |
| 覆盖区间 | `2015-01-01T00:00+08:00` → `2026-09-21T23:00+08:00` |

## 10. 待确认项清单

1. 默认（Best Match）响应的数据模型构成与网格分辨率（响应未声明）；
2. 末端日期的滞后构成（ERA5T / 预报补值）与次日是否回补变化；
3. 许可条款是否允许用于论文实验（须人工复核官方最新条款）；
4. 5 个网格点的逆向地理编码结果与县内确认（以 DS-1 回执为准，本工作树无法访问 Nominatim）；
5. 与目标水位序列的时间重叠区间；
6. ~~契约参数名 `model` 是否修订为 `models`~~ —— **已于 2026-09-21 返工修订为 `models`**（见第 8 节第 1 条）；
   仍待确认的是：修订后是否需要**重取**数据（主 Agent 已判定既有 Best Match 数据有效，不必重取）。

## 11. 使用限制

- 仅限教学、科研与辅助分析，不得作为防汛调度、预警发布或应急决策依据；
- 不得冒充水行政主管部门发布官方预警；
- 不得把再分析降雨表述为地面雨量站实测值；
- 不得把 Best Match 再分析降雨表述为「ERA5-Land 实测」或「ERA5 实测」。

## 12. 登记补充（来自 DS-1 登记版，2026-09-21）

> 本节保留 DS-1 任务（开放数据来源核实与登记）在原登记文档中的实测内容；DS-3 / DS-3b 的实测结论与口径修订见第 1～11 节。DS-1 为 3 天登记探针，DS-3 为 2015—2026 全量归档。

### 12.1 登记探针（DS-1 实测，3 天 / 72 条小时记录）

- 完整请求 URL：`https://archive-api.open-meteo.com/v1/archive?latitude=27.79&longitude=120.03&start_date=2022-06-01&end_date=2022-06-03&hourly=precipitation,rain&timezone=Asia%2FShanghai`（本 API 无密钥，URL 无需脱敏）；HTTP 200。
- 响应顶层字段（实测原文）：`latitude`、`longitude`、`generationtime_ms`、`utc_offset_seconds`、`timezone`、`timezone_abbreviation`、`elevation`、`hourly_units`、`hourly`。

| 字段 | 实测值 | 说明 |
| --- | --- | --- |
| `latitude` / `longitude` | `27.732864` / `120.03371` | 请求坐标（27.79, 120.03）被解析到的实际网格单元中心 |
| `elevation` | `267.0` | 网格单元高程（米） |
| `timezone` / `timezone_abbreviation` | `Asia/Shanghai` / `GMT+8` | 时区参数生效 |
| `utc_offset_seconds` | `28800` | UTC+8 |
| `hourly.time` | `2022-06-01T00:00` ~ `2022-06-03T23:00`，共 72 条 | 全部整点对齐，无缺失、无 null |
| `hourly.precipitation` | 单位 `mm`；0.0 ~ 16.4，无 null | 降水量（小时） |
| `hourly.rain` | 单位 `mm`；0.0 ~ 16.4，无 null | 雨量（小时）；3 天探针内与 `precipitation` 相同，长时程差异见第 8 节（1,268 行不一致） |
| `model` | 响应中无该字段 | 数据模型未在响应中声明；实际生效参数名为 `models`（见第 8 节） |

- 探针快照：`data/raw/open-meteo/20260921T065315Z-cff1838f6580.json`（严格合法 JSON，非有限值写 `null`；排他创建；`data/raw/` 被忽略，不进入提交）；同名 `.manifest.json` 含 `record_count`（72）与 `sha256`。
- 探针快照 SHA-256：`cff1838f6580e325d2fbfaaab74fbbd165a7eb639fc3a12bdb2b1b9eb71f6339`；获取时间（UTC）2026-09-21T06:53:15Z。

### 12.2 平台文档声明（DS-1 摘录，访问日期 2026-09-21）

- 覆盖范围：「Gap-free and consistent historical weather data using weather reanalysis from ERA5 (0.25°, from 1940) and ERA5-Land (0.1°, from 1950) and ECMWF IFS (9 km, from 2017).」
- 默认行为：「Note: The default Best Match combines IFS HRES, ERA5 and ERA5-Land seamlessly.」——未显式指定 `models` 时即为默认 Best Match。
- 更新频率：ERA5 与 ERA5-Land「每日更新（延迟 5 天）」。
- 配额：「Only for non-commercial use and less than 10.000 daily API calls.」——非商业且每日调用少于 10000 次免密钥；无分页，按 `start_date` / `end_date` 一次返回区间数据。

### 12.3 网格点元数据文件

- 文成县 5 个固定降雨网格点（OSM Nominatim 核实、冻结）见 `data/metadata/wencheng_grid_points.csv`，与 `data/metadata/README.md` 一并登记；DS-3 归档即以该清单为取值依据（回显表见第 7 节）。

### 12.4 DS-1 遗留待确认项（已由第 8 / 10 节回答者标注）

1. 显式 `models=era5_land`（或 `era5`）请求的行为与响应差异 → **已由第 8 节实测回答（era5_land 全空、Best Match 与 era5 差约 13%）**；
2. `precipitation` 与 `rain` 在长时程内是否始终一致 → **已由第 8 节实测回答（513,840 行中 1,268 行不一致）**；
3. 0.1° 网格在文成县内的实际覆盖单元数与各网格点归属 → 见第 7 节回显表；
4. 限流与重试行为（429/4xx 响应形态）→ 第 3 节已记录本轮实测与处理策略，限流形态仍**待确认**；
5. 底层 ECMWF / Copernicus 数据使用条款对论文发表的适用性 → **待确认**（同第 10 节第 3 项）。
