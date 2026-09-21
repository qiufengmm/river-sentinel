# 多源降雨/流量匹配与准入审计实施计划

- 计划日期：2026-09-21
- 前置：数据基线计划（`docs/superpowers/plans/2026-09-18-feiyunjiang-data-baseline.md`）Task 1~9 已全部合入 `main`（HEAD `0712215`，已推送 `origin/main`）
- 授权依据：Task 9 验收裁定 3「降雨候选发现授权开始，附条件」（`docs/experiments/data-baseline-acceptance.md` 第 76~82 行）
- 适用阶段：项目设计规格（`docs/superpowers/specs/2026-09-16-river-sentinel-project-design.md`）阶段 2 收尾，并为阶段 3 消融实验提供数据准入证据
- 当前阻塞：`API_AUTH_PENDING`（温州公共数据开放平台凭据未获批）

## 1. 背景与现状（均为实测事实）

| 项 | 现状 |
| --- | --- |
| 目标数据集 | `cata_12720`「文成县飞云江二期治理工程水位信息」，来源页 `https://data.wenzhou.gov.cn/jdop_front/detail/data.do?iid=12720` |
| 目标站身份 | 样例不含站名字段，管线以 `station_ids=("unknown",)` 兜底；**真实断面身份待 `audit-api` 获批后经平台元数据确认** |
| 实测频率 | 样例推断 60.0 分钟（合成数据，不构成真实频率证据） |
| 现有能力 | `ingestion`（文件 + 水位 API）→ `normalize` → `quality` → `dataset`（重采样 + 时间顺序切分）→ `baseline`/`metrics` → `pipeline`/`reporting`/`provenance` → `cli`；`pytest` 216 passed，覆盖率 96% |
| 缺口 | 无测站关系表；无雨量/流量适配器；无多源时间对齐与聚合；无时滞分析；无准入审计 |
| 1/3/6 口径 | 统一称「步长」（Task 9 裁定 1），真实频率确认后方可称「小时」 |

## 2. 目标与非目标

### 目标

1. 建立可人工复核的**测站关系表**，记录目标断面、候选雨量/流量站、编码、名称、经纬度、上下游关系、距离、推荐时滞、匹配依据与启用状态。
2. 建立**多源时间对齐与聚合**能力：统一小时网格上，水位取桶内最后观测、降雨按窗求和、流量取均值（可配末次）。
3. 建立**时滞分析**能力：仅在训练集上估计候选时滞与相关量，输出供人工复核，代码不得自动改写关系表。
4. 建立**准入审计**能力：时间重叠率、缺失率、最长连续缺口、涨水过程覆盖，逐站给出准入结论与证据。
5. 产出**多源准入裁定文档**，通过则支撑「多源水雨情融合」题目，未通过则按规格降级题名并保留证据。

### 非目标

- 不训练 XGBoost / LSTM，不做消融实验（属阶段 3）；
- 不实现官方警戒水位判定与预警（无官方阈值时禁止产出）；
- 不引入后端、前端、部署改动；
- 不为保留多源题目而伪造、不合理插补或放宽阈值；
- 不修改既有 `wenzhou_api.py`、`normalize.py`、`dataset.py`、`pipeline.py` 的对外行为与既有断言。

## 3. 业务流程

```text
平台目录检索候选雨量/流量数据集（人工，凭据非必需）
        ↓
逐源登记来源、许可条件、字段字典（docs/data-governance/）
        ↓
人工复核测站关系表（data/metadata/station_relations.csv）
        ↓
stations.py：加载 + 结构校验（唯一目标断面、必填依据、时滞非负）
        ↓
open_data.py：参数化 dataset_id 的只读客户端（复用分页与密钥零回显契约）
        ↓
multisource.py：左连接到目标网格 → 雨量求和 / 流量均值 → 缺失标记
        ↓
lag.py：仅训练集搜索候选时滞与相关量（人工复核后才写入关系表）
        ↓
admission.py：重叠率 / 缺失率 / 连续缺口 / 涨水覆盖 → 逐站准入结论
        ↓
多源准入裁定文档（通过 → 进入阶段 3；未通过 → 题名降级）
```

真实接口抓取必须在 `API_AUTH_PENDING` 解除后进行；在此之前所有步骤用合成样例实现与验证，任何真实数据结论一律标注为「未获取」。

## 4. 冻结设计决策（D-1 ~ D-9）

- **D-1 时滞只能在训练集上估计**。测试集、验证集不得参与时滞选择、相关量计算、阈值拟合；输出仅作为辅助证据，最终时滞由人工复核确定。
- **D-2 聚合口径固定**：水位 = 桶内最后一次观测（与既有 `resample_water_level` 一致，保留至多一个桶宽的有界前瞻并在文档中声明）；降雨 = 时间窗求和；流量 = 均值（可显式配置为末次有效值）。**新增源列采用因果区间 `[t - frequency, t)`（左闭右开）聚合到标签 `t`**，即 `t` 时刻的源特征只使用 `t` 之前的观测，杜绝未来信息泄漏；水位桶内最后观测的有界前瞻与源列因果口径的差异必须在模块 docstring、验收文档与论文中分别声明。
- **D-3 降雨缺失不得填 0**。缺失率超阈值的候选站直接判为不入围；水位与流量的短缺口可受限线性插补并写 `is_imputed=True`，长缺口保持 NaN。
- **D-4 多源合并为左连接**，不得改变目标序列行数与时间戳；新增变量列缺省为 NaN，并同时产出 `<var>_is_missing` 布尔标记列。
- **D-5 准入阈值配置化**（默认值与理由写入文档，可依据真实数据调整但必须先记录理由）：与目标水位有效时间重叠率 ≥ 70%；核心时段总体缺失率 ≤ 20%；测试期最长连续缺口 ≤ 6 步长；覆盖明显涨水过程 ≥ 3 次。
- **D-6 涨水过程定义配置化**：观察窗内水位涨幅阈值与起涨分位数由真实数据统计后确定；样例阶段只用「相对涨幅」定义，不得把样例阈值写入正式结论。
- **D-7 多源产物继续走来源追溯**：每个源单独登记 `source_name`、`source_uri`、`dataset_id`、获取时间、请求参数、记录数、SHA-256；密钥零回显，失败信息不含 URL。
- **D-8 降级路径明确**：无候选站通过准入时，产出降级裁定文档，题名改为「基于飞云江二期治理工程数据的水位预测与防汛辅助决策智能体系统」，并保留全部审计证据。
- **D-9 时域表述沿用「步长」**，仅在真实频率确认为小时级后才可称「1/3/6 小时」。

## 5. 任务拆分

依赖与并行：`MS-2`、`MS-3`、`MS-4` 文件零重叠，可并行；`MS-5` 依赖 `MS-4`；`MS-6` 依赖 `MS-4`、`MS-5`；`MS-1` 由人工检索，与实现任务并行推进；`MS-7` 在所有实现完成后由主 Agent 裁定。

### MS-1 候选数据源发现与登记（人工检索 + 文档）

- 目标：在温州市公共数据开放平台检索雨量、流量（水文）候选数据集，逐源登记来源页面 URL、提供单位、数据集标识、许可或审批条件、获取时间、字段字典、是否可用于论文实验。
- 产出：`docs/data-governance/source-*.md`（每源一份，沿用 `source-cata-12720.md` 结构）、`docs/data-governance/README.md` 索引更新。
- 约束：无法确认的字段含义写「待确认」，禁止推测；需要账号或审批的如实记录状态；不得把候选数据集的字段假定写进代码契约。
- 回执：检索结论以文档形式提交，不生成 `.codex/reports`。

### MS-2 测站关系表与加载校验（`stations.py`）

- 允许新增：`ml/src/river_sentinel_ml/stations.py`、`ml/tests/test_stations.py`、`data/metadata/station_relations.csv`、`data/metadata/README.md`。
- 禁止：修改 `ml/src/river_sentinel_ml/` 下任何既有源文件；修改既有测试；引入除 pandas / pydantic 之外的新依赖。
- 契约：`load_station_relations(path) -> StationRelationTable`，每行字段至少含 `target_station_code`、`station_code`、`station_name`、`station_type`（`rainfall` / `flow`）、`longitude`、`latitude`、`upstream`（布尔）、`distance_km`、`recommended_lag_steps`（非负整数）、`match_basis`（非空，自由文本）、`enabled`（布尔）。
- 校验规则：表非空；`target_station_code` 唯一；`station_code` 无重复（目标站除外）；`station_type` 取值受限；经纬度落在合法范围；`distance_km` 非负；`recommended_lag_steps` 为非负整数；`match_basis` 非空；`enabled=True` 的行不得缺关键字段。所有非法输入抛 `ValueError` 且消息含字段名。
- 测试要求（测试先行，红灯必须由「模块不存在」触发）：至少 10 条用例，覆盖正常加载、各字段非法取值、目标断面不唯一、空表、缺失列、`enabled` 过滤语义、加载不修改入参。
- 验收：`pytest ml/tests/test_stations.py -q` 全绿；既有 216 项不减少；`ruff check ml` / `ruff format --check ml` 双绿。
- 回执：`.codex/reports/multisource-ms2-report.md`。

### MS-3 参数化开放平台只读客户端（`ingestion/open_data.py`）

- 允许新增：`ml/src/river_sentinel_ml/ingestion/open_data.py`、`ml/tests/ingestion/test_open_data.py`。
- 禁止：修改 `ingestion/wenzhou_api.py`、`ingestion/files.py` 及既有测试；新增第三方依赖。
- 契约：`WenzhouOpenDataClient(dataset_id: str, appsecret: str, transport: httpx.BaseTransport | None = None)`，基类 URL 为 `https://data.wenzhou.gov.cn/jdop_front/interfaces/{dataset_id}`；提供 `get_total()`、`get_update_date()`、`iter_rows(page_size=200)`。
- 复用既有契约：`MAX_PAGE_SIZE = 200`；`iter_rows` 产出数不得超过 `get_total()` 声明值（超出页截断）；空页提前停止；响应必须 `status == 1`，否则抛 `SourceAccessError`；`appsecret` 仅作为查询参数传递，**异常消息只报异常类型，绝不拼接 URL 或凭据**；`page_size` 越界抛 `ValueError`。
- `dataset_id` 校验：非空、仅允许 `[A-Za-z0-9_]`，合法前缀不作假定（不同源可能不是 `cata_` 前缀），非法抛 `ValueError`。
- 测试要求：使用 `httpx.MockTransport`，覆盖分页截断、空页停止、非列表数据页、非对象行、`status != 1`、非 JSON、超时或 HTTP 错误不泄漏 URL、多 `dataset_id` 共用一个类。
- 验收：`pytest ml/tests/ingestion/test_open_data.py -q` 全绿；全量 216 + N 全绿；ruff 双绿。
- 回执：`.codex/reports/multisource-ms3-report.md`。

### MS-4 多源时间对齐与聚合（`multisource.py`）

- 允许新增：`ml/src/river_sentinel_ml/multisource.py`、`ml/tests/test_multisource.py`。
- 禁止：修改 `dataset.py`、`normalize.py`、`quality.py`、`pipeline.py` 及既有测试。
- 契约：`align_sources(target: pd.DataFrame, sources: Mapping[str, pd.DataFrame], frequency: str = "1h", flow_aggregation: Literal["mean", "last"] = "mean") -> pd.DataFrame`。
- 行为：以目标序列的时间网格为基准做**左连接**，输出行数与时间戳与目标序列完全一致；`rainfall` 类型源按窗**求和**，`flow` 类型按 `flow_aggregation` 聚合；目标序列外的源时间点被聚合到所属网格；某网格无源数据 → 变量列 NaN 且 `<var>_is_missing = True`；**降雨缺失绝不填 0**；输出列顺序确定，新增列命名 `<station_code>_<var>`；不改入参。
- 校验：目标时间戳必须 tz-aware 且严格递增；源时间戳 tz-aware；源 `station_type` 未知或聚合方式非法抛 `ValueError`；空源帧返回全 NaN 且缺失标记全 True。
- 测试要求（至少 12 条）：雨量求和（含多观测落入同一网格）、流量均值与末次两种口径、目标行数不变、跨时区对齐、空源、未知类型、非递增时间戳、naive 时间戳、入参不被修改、缺失标记正确性、零降雨与缺失降雨区分（0 值是有效观测，`is_missing=False`）。
- 验收：单文件全绿；全量 216 + N 全绿；ruff 双绿。
- 回执：`.codex/reports/multisource-ms4-report.md`。

### MS-5 时滞分析（仅训练集，`lag.py`）

- 依赖：`MS-4`。允许新增：`ml/src/river_sentinel_ml/lag.py`、`ml/tests/test_lag.py`。
- 契约：`estimate_lag(train_target: pd.Series, train_source: pd.Series, max_lag_steps: int) -> LagEstimate`（返回候选时滞步数、互相关系数、有效样本数、搜索范围）。
- 行为：只在**训练集**上计算；`max_lag_steps` 为正整数，越界抛 `ValueError`；样本不足（有效成对样本 < 3）时返回 `None` 系数并标记 `insufficient_samples`，**禁止返回 0 系数冒充结果**；源序列滞后于目标为负相关的情形不做符号翻转；输出不得写回测站关系表。
- 文档要求：模块 docstring 写明「相关量是辅助证据，不能单独作为水文上下游关系依据，最终时滞须人工复核」。
- 测试要求（至少 8 条）：已知滞后序列能找回时滞；互相关系数手算可核对；样本不足返回 `None`；`max_lag_steps` 非法；含 NaN 成对剔除；时区不一致处理；不修改入参；确定性。
- 验收：单文件全绿；全量不回退；ruff 双绿。
- 回执：`.codex/reports/multisource-ms5-report.md`。

### MS-6 准入审计（`admission.py`）

- 依赖：`MS-4`、`MS-5`。允许新增：`ml/src/river_sentinel_ml/admission.py`、`ml/tests/test_admission.py`。
- 契约：`audit_candidate(candidate, target, thresholds) -> CandidateAdmission`（逐站结论：`admitted: bool`、`overlap_ratio`、`missing_ratio`、`max_gap_steps`、`flood_event_count`、`reasons: tuple[str, ...]`）。
- 阈值默认（D-5）：重叠率 ≥ 0.70、缺失率 ≤ 0.20、测试期最长连续缺口 ≤ 6 步长、涨水过程覆盖 ≥ 3 次；阈值对象可配置，非法（比例越界、负数）抛 `ValueError`。
- 规则：任一阈值不满足 → `admitted=False` 并在 `reasons` 中逐条写明未达标项与实测值；**不得**为通过而修改阈值、插补缺失或改口径；涨水过程计数使用 D-6 的配置化定义，样例阶段使用相对涨幅并明确标注。
- 输出：生成 `admission-report.json`（严格 JSON，非有限值写 `null`）与 `admission-report.md`；沿用 `provenance` 的快照与来源清单机制登记参与审计的每个源。
- 测试要求（至少 10 条）：达标与不达标各一条、边界值（恰好 0.70 / 0.20）、连续缺口统计、涨水过程计数、阈值非法、空候选序列、全缺失候选、时间范围无重叠（重叠率 0）、JSON 严格可解析、报告确定性。
- 验收：单文件全绿；全量不回退；ruff 双绿；`python -c "json.load(...)"` 严格解析通过。
- 回执：`.codex/reports/multisource-ms6-report.md`。

### MS-7 多源准入裁定（主 Agent）

- 输入：`MS-1` 来源登记、`MS-2` 关系表、`MS-6` 审计报告、真实数据（若凭据已获批）。
- 产出：`docs/experiments/multisource-admission.md`，逐站给出通过/未通过结论、实测值与依据；无站通过则写明题名降级路径（D-8）。
- 约束：结论必须能对应到具体产物文件路径与数据快照；`API_AUTH_PENDING` 未解除时，真实数据结论一律写「未获取」，不得用样例数据推断真实水文结论。

## 6. 关键文件影响

| 类别 | 影响 |
| --- | --- |
| 新增源码 | `stations.py`、`ingestion/open_data.py`、`multisource.py`、`lag.py`、`admission.py` |
| 新增测试 | `test_stations.py`、`ingestion/test_open_data.py`、`test_multisource.py`、`test_lag.py`、`test_admission.py` |
| 新增数据 | `data/metadata/station_relations.csv`（人工复核，小体量可入库）、`data/metadata/README.md` |
| 新增文档 | `docs/data-governance/source-*.md`（每源）、`docs/experiments/multisource-admission.md` |
| 既有代码 | **零修改**（既定契约与 216 项断言必须保持） |
| 数据库 | 无 |
| 模型 | 不涉及（阶段 3 再引入 XGBoost / LSTM） |
| 论文 | 提供多源准入证据、测站关系表与降级依据，是「多源水雨情融合」题目成立的支撑 |

## 7. 验收标准（计划级）

1. `pytest -q` 全量通过且不低于 216 项（新增用例后为 216 + N，0 failed / 0 error / 0 skipped）；
2. `ruff check ml` 与 `ruff format --check ml` 双绿，`git diff --check` 退出码 0；
3. 既有 `wenzhou_api.py`、`normalize.py`、`dataset.py`、`pipeline.py`、`cli.py` 与 `data/samples/water_level_sample.csv` 未被修改（以 `git diff --stat` 为空为证）；
4. 合成样例上多源聚合、时滞分析、准入审计结果可重复生成（同一输入两次运行产物确定性一致）；
5. 测站关系表字段齐全、来源登记逐源完整，无法确认的字段标注「待确认」；
6. 所有产物为严格合法 JSON（非有限值写 `null`），密钥零回显；
7. 真实数据结论在凭据到位前一律为空并标注 `API_AUTH_PENDING`；
8. 无候选站通过时，题名降级路径已写入 `docs/experiments/multisource-admission.md`。

## 8. 验证命令（每次提交前后必须新鲜执行）

```powershell
$env:PYTHONPATH='F:\code\engine\ml\src'
python -m pytest -q
python -m ruff check ml
python -m ruff format --check ml
git diff --check
git status --short --branch
```

端到端（凭据到位后）：`river-sentinel-data audit-api --output-dir build/...`（水位真实频率确认）与新增的多源审计子命令。

## 9. 风险、限制与遗留

1. `API_AUTH_PENDING`：真实候选数据集抓取、真实测站身份确认、真实频率确认全部阻塞；计划内所有实现仅以合成样例证明正确性。
2. 候选数据集字段未知：字段字典在凭据到位前无法确认，禁止把假定字段写进代码契约。
3. 样例频率不构成真实频率证据（Task 9 裁定 1），时域表述继续用「步长」。
4. 桶内最后观测存在至多一个桶宽的有界前瞻，聚合与对齐步骤必须在文档与论文中声明（既有约束 C-6 扩展到多源列）。
5. 标签时刻 `is_imputed=True` 的样本必须剔除（既有约束 C-5），多源特征同样禁止跨时间顺序切分段构造窗口（既有 P2-3）。
6. 涨水过程阈值需真实数据统计后确定，样例阶段阈值不得写入正式结论。
7. 仓库不含真实批量数据与模型权重；多源原始快照写入被忽略的 `data/raw/` 或 `build/` 目录。
