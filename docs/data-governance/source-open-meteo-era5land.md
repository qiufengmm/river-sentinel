# 数据源治理记录：Open-Meteo Historical Weather API（ERA5 / ERA5-Land 再分析）

> 本文只记录数据来源、字段口径、访问状态与校验值存放位置，不记录任何凭据取值（本 API 非商业用途无需密钥）。
> **口径警示：这是网格化再分析降雨，不是文成县地面雨量站实测值。** 多点区域平均降雨只能标注为「文成县区域降雨代理变量」。
> 本系统仅用于教学科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。

## 1. 数据集标识

| 项 | 值 |
| --- | --- |
| 数据集名称 | Open-Meteo Historical Weather API（Historical Weather Data，历史天气再分析） |
| 数据集 ID / 标识 | `archive-api.open-meteo.com/v1/archive` |
| 接口目录 | `/v1/archive`（Historical Weather API） |
| 来源页面 | <https://open-meteo.com/en/docs/historical-weather-api> |
| 提供单位 | Open-Meteo（页面署名 Zippenfenig, P. (2023)）；底层再分析数据来自 ECMWF ERA5 / ERA5-Land 等（文档声明，见第 2 节） |
| 覆盖区域 | 全球网格；本研究取文成县范围内固定网格点（见 `data/metadata/wencheng_grid_points.csv`） |
| 主题类别 | `rainfall` |
| 检索关键词 | Open-Meteo historical weather API / ERA5-Land archive API |
| 登记日期 | 2026-09-21 |

## 2. 接口与字段

接口基础路径：`https://archive-api.open-meteo.com/v1/archive`（GET，无需鉴权头）。

实测请求（2026-09-21，文成县网格点 WC-G1，3 天，72 条小时记录）：

- 完整请求 URL：`https://archive-api.open-meteo.com/v1/archive?latitude=27.79&longitude=120.03&start_date=2022-06-01&end_date=2022-06-03&hourly=precipitation,rain&timezone=Asia%2FShanghai`（本 API 无密钥，URL 无需脱敏）。
- HTTP 状态：200。

响应顶层字段（实测原文）：`latitude`、`longitude`、`generationtime_ms`、`utc_offset_seconds`、`timezone`、`timezone_abbreviation`、`elevation`、`hourly_units`、`hourly`。

| 字段 | 实测值 | 说明 |
| --- | --- | --- |
| `latitude` / `longitude` | `27.732864` / `120.03371` | 请求坐标（27.79, 120.03）被解析到的**实际网格单元中心** |
| `elevation` | `267.0` | 网格单元高程（米，页面文档口径待确认） |
| `timezone` / `timezone_abbreviation` | `Asia/Shanghai` / `GMT+8` | 时区参数生效 |
| `utc_offset_seconds` | `28800` | UTC+8 |
| `hourly.time` | `2022-06-01T00:00` ~ `2022-06-03T23:00`，共 72 条 | **全部整点对齐，无缺失、无 null** |
| `hourly.precipitation` | 单位 `mm`；实测 0.0 ~ 16.4，无 null | 降水量（小时） |
| `hourly.rain` | 单位 `mm`；实测 0.0 ~ 16.4，无 null | 雨量（小时）；实测样本内与 `precipitation` 数值完全相同，更长时程是否一致**待确认** |
| `model` | **响应中无该字段** | 数据模型未在响应中声明（见下） |

数据模型与分辨率（**以文档声明为准，响应原文未声明 `model` 字段**，访问日期 2026-09-21）：

- 文档原文：「Gap-free and consistent historical weather data using weather reanalysis from ERA5 (0.25°, from 1940) and ERA5-Land (0.1°, from 1950) and ECMWF IFS (9 km, from 2017).」
- 文档原文（默认行为）：「Note: The default Best Match combines IFS HRES, ERA5 and ERA5-Land seamlessly.」即**未显式指定 `model` 参数时，默认 Best Match 合并多个模型**，本实测快照即属此类。
- 若 DS-3 需要纯 ERA5-Land（0.1°，1950 年起），须显式传 `model` 参数并重新实测验证，**待确认**。

口径警示：再分析降雨为网格估算值，与地面雨量站实测存在系统差异；禁止表述为「文成县实测降雨」。

## 3. 分页与配额

- 无分页：按 `start_date` / `end_date` 一次返回区间数据。
- 配额（文档声明，访问日期 2026-09-21）：「Only for non-commercial use and less than 10.000 daily API calls.」——非商业且每日调用少于 10000 次免费，无需密钥。
- 实测未触发限流；正式批量取数时仍需控制请求节奏，限流行为**待确认**。

## 4. 许可与访问审批状态

- 开放类型：无条件开放（非商业用途、低于每日调用上限时无需密钥；商业用途需 API Key）。
- 许可协议：页面未标注具体许可协议名称，**待确认**（数据底层来源 ECMWF/Copernicus 的使用条款未逐条核实）。
- 当前状态：**可用（实测成功，2026-09-21）**。
- 是否允许用于论文实验：允许作为「文成县区域降雨代理变量」用于教学科研实验，但须在论文中明确其为网格化再分析数据而非站点实测。
- 无需凭据，无任何密钥写入本仓库。

## 5. 获取时间与校验值的存放位置

- 快照：`data/raw/open-meteo/20260921T065315Z-cff1838f6580.json`（严格合法 JSON，非有限值写 `null`；排他创建，绝不覆盖；`data/raw/` 被 `.gitignore` 忽略，不进入提交）。
- 清单：同目录同名 `.manifest.json`，含 `source_name`、`source_uri`、`request_parameters`、`acquired_at`、`record_count`（72）、`sha256`。
- SHA-256：`cff1838f6580e325d2fbfaaab74fbbd165a7eb639fc3a12bdb2b1b9eb71f6339`；获取时间（UTC）：2026-09-21T06:53:15Z。
- 快照文件大小：见清单 `file_size_bytes`。

## 6. 时间覆盖与更新频率

- 文档声明：ERA5 1940 年起、ERA5-Land 1950 年起、ECMWF IFS 2017 年起；ERA5 与 ERA5-Land「每日更新（延迟 5 天）」（访问日期 2026-09-21）。
- 实测：2022-06-01 ~ 2022-06-03 共 72 条小时记录，全部整点对齐，无缺失、无 null。
- 与目标水位序列（2022-06-02 ~ 2022-10-09，见 `source-cata-12720.md` 第 9 节）时间上可重叠，正式对齐由 DS-2/DS-3 完成。

## 7. 测站身份信息

| 项 | 值 |
| --- | --- |
| 是否含测站编码字段 | 无（非测站数据，为网格化再分析） |
| 是否含测站名称字段 | 无 |
| 是否含经纬度字段 | 有：响应顶层 `latitude` / `longitude` 为实际网格单元中心，非请求坐标原样返回 |
| 与目标断面的上下游关系 | 不适用（非站点）；作为区域降雨代理变量使用 |
| 与目标断面的距离（km） | 不适用 |
| 推荐时滞（步长） | 待确认（须由 MS-5 在训练集上估计后人工复核） |

多点区域平均降雨必须标注为「**文成县区域降雨代理变量**」。

## 8. 待确认项清单

1. 显式 `model=era5_land`（或 `era5`）请求的行为与响应差异（响应是否声明模型、数值是否与 Best Match 不同）；
2. `precipitation` 与 `rain` 在长时程内是否始终一致（实测 3 天样本内完全相同）；
3. ERA5-Land 0.1° 网格在文成县内的实际覆盖单元数与各网格点归属（DS-3 核实）；
4. 限流与重试行为（429/4xx 响应形态）；
5. 底层 ECMWF/Copernicus 数据使用条款对论文发表的适用性。

## 9. 使用限制

- 仅限教学、科研与辅助分析；不得作为防汛调度、预警发布或应急决策依据。
- 不得冒充水行政主管部门发布官方预警。
- 任何产物与论文表述中须注明「网格化再分析降雨（文成县区域降雨代理变量）」，禁止表述为地面站点实测。
