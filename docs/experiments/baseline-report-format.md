# 基线审计报告字段说明

本文逐个解释 `river-sentinel-data audit-file` / `audit-api` 产出的四类 JSON 与一份 Markdown 报告的字段口径。
所有产物都是**历史回放 / 离线数据**的审计结果，不是实时监测结论，仅用于教学科研辅助，不替代官方防汛决策。

## 1. 产物与位置

每次运行都会在 `--output-dir` 下新建一个 run 子目录（不覆盖历史产物），目录名固定
`run-<YYYYmmddTHHMMSSZ>`（UTC、秒级、Windows 合法）；同一秒内再次运行追加 `-2`、`-3`…。
**run 目录是自包含单元**：原 5 类产物文件名严格保持原名，另有当次抓取的原始快照与来源清单：

| 文件 | 内容 |
| --- | --- |
| `processed.parquet` | 重采样后的完整等间隔序列（含 `is_imputed`），`index=False` |
| `quality-report.json` | 质量统计、频率、断面与每段插补占比 |
| `baseline-metrics.json` | 每段每步长的样本数与指标 |
| `experiment-manifest.json` | 可复现实验所需的最小信息（`ExperimentManifest`） |
| `baseline-report.md` | 人读报告 |
| `wenzhou/<dataset_id>/YYYY/MM/DD/<UTC时间戳>-<sha256前12位>.ndjson` | 不可变原始快照（子层级仍由 provenance 冻结规则决定） |
| `wenzhou/<dataset_id>/YYYY/MM/DD/<UTC时间戳>-<sha256前12位>.manifest.json` | 来源清单（含获取时间与 SHA-256） |

因为 run 目录名唯一（时间戳 + 同秒序号），快照路径随之唯一，同一秒内对同一批数据重复运行不再撞名，两次运行都正常成功。

每次运行都保留当次抓取的原始快照，**不做跨 run 去重**；长期运行需自行清理旧 run 目录。

CLI 成功时会把 run 目录的**绝对路径**打印到 stdout。

## 2. `quality-report.json`

- `quality.*`：直接来自 `QualitySummary`。
  - `total_rows`：源数据总行数。
  - `valid_rows`：通过全部校验并去重后保留的行数。
  - `duplicate_rows`：`source_record_id` 重复的行数（保留**最后一次**出现）。
  - `invalid_timestamp_rows`：`time_d` 不可解析，或本地时间在目标时区中歧义/不存在；`z_id` 不可用的行也计入此项（上游已记录的偏差）。
  - `invalid_level_rows`：水位存在但不是有限数值的行数。
  - `missing_level_rows`：水位缺失或为空的行数。
  - `start_at` / `end_at`：`valid_rows` 的最早与最晚观测时间（tz-aware）。
  - `inferred_interval_minutes`：由观测时间推断的采样间隔（分钟），样本不足或间隔不规则时为 `null`。
- 守恒式：`total_rows == valid_rows + invalid_timestamp_rows + invalid_level_rows + missing_level_rows + duplicate_rows`。
- `frequency`：重采样频率（默认 `1h`）。
- `station_code`：本次实际使用的断面；源数据没有断面标识时为 `null`，实验清单中记为 `unknown`。
- `partitions.<段>`：`rows`（该段行数）、`imputed`（插补行数）、`missing`（仍缺失行数）、`evaluated`（参与评估行数）、`imputed_ratio`（插补占比）。
- `gap_note`：重采样后的缺口说明（插补行数、仍缺失行数、最长连续缺口步数、插补阈值）。

## 3. `baseline-metrics.json`

- `horizons`：本次评估的步长列表（默认 `[1, 3, 6]`）。
- `notes.step_unit`：步长口径声明。
- `notes.bounded_look_ahead`：有界前瞻声明。
- `notes.null_reasons`：`insufficient_samples`（无成对有效样本，指标不计算也不填 0）、`zero_variance`（目标方差为 0，R² 与 NSE 无定义）。
- `partitions.<段>.horizons.<步长>`：
  - `sample_count`：成对有效样本数；样本不足时为 `0` 且其余指标为 `null`。
  - `mae`：平均绝对误差。
  - `rmse`：均方根误差。
  - `r2`：决定系数；目标方差为 0 或样本不足时为 `null`。
  - `nse`：Nash–Sutcliffe 效率系数；与 `r2` 同口径，同条件为 `null`。
  - `peak_absolute_error`：观测峰值与预测峰值之差的绝对值。
  - `reason`：指标未计算的原因；正常计算时为 `null`。
- 只有 `validation` 与 `test` 两个段进入指标报告，训练段不参与评估。
- 评估前已剔除 `is_imputed=True` 与水位缺失的行：插补值是模型产物，不能当标签使用。

### 3.1 预测时域是“步长”，不是小时

1 / 3 / 6 指**预测步长**（网格步数）。只有当选用的采样频率被证实为小时时，下游与论文才可以改称小时。

### 3.2 有界前瞻

重采样时每个网格点取**桶内最后一次观测**，时间戳取**桶左边界**。因此标签 `t` 的值可能实际发生在 `t` 之后，最多晚一个桶宽（默认频率 `1h` 时为 1 小时）。解释 1 / 3 / 6 步长结论时必须同时引用这条声明。

### 3.3 切分与跨段预测

- 切分严格按时间先后，三段连续且非空，**段与段之间没有间隔（no gap）**。
- 因为没有间隔，段首若干步长的预测来自上一段末尾的观测；禁止用跨段窗口构造训练样本。
- 预测在**完整序列**上一次性生成后再按段切片，绝不在段内独立做 `shift`。

## 4. `experiment-manifest.json`

字段与 `ExperimentManifest` 契约一致：

- `experiment_id`：`<模型>-<UTC时间戳>-<数据集 SHA-256 前 8 位>`。
- `created_at` / `started_at` / `finished_at`：UTC 时间。
- `dataset_sha256`：原始快照的 SHA-256（与来源清单一致）。
- `horizons`：步长列表（正整数）。
- `train_range` / `validation_range` / `test_range`：三段的时间区间，tz-aware 且**严格不重叠**（`train[1] < validation[0]`、`validation[1] < test[0]`）。
- `code_commit`：运行时的 `git rev-parse HEAD`；无法捕获时如实写 `unknown`，不编造哈希。
- `station_ids`：断面集合；无断面标识时为 `("unknown",)`。
- `feature_names`：本次固定为 `("water_level_m",)`。
- `model_name`：本次固定为 `persistence`（持久性基线）。
- `parameters`：频率、步长、插补阈值、切分比例、断面、时区等运行参数，另含 `run_id`（run 目录名）与接口运行的 `page_size`；持久性基线无随机性，`random_seed` 固定为 `0`。
- `environment`：Python / 平台 / pandas / numpy 版本。
- `artifact_paths`：run 目录自包含，填**相对 run 目录**的产物路径 —— 5 类产物文件名 + 快照 `*.ndjson` 与来源清单 `*.manifest.json`。
- `status`：`completed` 或 `failed`；`failed` 时 `failure_reason` 必填，`completed` 时必须为空。

### 4.1 切分之前失败：`failure-manifest.json`

切分之前失败（例如取数失败、源文件不可读、数据不足以切分）时，三段时间区间尚未算出，
而 `ExperimentManifest` 的 `train_range` / `validation_range` / `test_range` 是**非 Optional**
字段，任何占位值都会变成伪造的时间区间。此时不生成 `experiment-manifest.json`，
而是在同一个 run 目录里落 `failure-manifest.json`：

| 字段 | 说明 |
| --- | --- |
| `status` | 固定 `"failed"` |
| `failure_stage` | 只允许 `fetch` / `normalize` / `resample` / `split` 之一 |
| `failure_reason` | 非空；已做凭据脱敏，长度上限 300 字符 |
| `run_id` | run 目录名 |
| `code_commit` | `git rev-parse HEAD`；捕获失败时写 `unknown` |
| `started_at` / `finished_at` | UTC 时间 |
| `artifact_paths` | 仅已落盘产物的相对路径，通常为空数组 `[]` |
| `raw_snapshot` | 快照相对 run 目录的路径；未写快照时为 `null` |

同目录不会出现 `experiment-manifest.json`、`baseline-metrics.json` 或 `processed.parquet` 半成品。

## 5. `baseline-report.md`

章节顺序：数据来源与获取状态 → 时间范围与采样间隔 → 断面与切分 → 质量统计 → 插补与缺口 → 基线预测指标 → 有界前瞻声明 → 运行参数、环境与代码版本 → 产物清单与校验值 → 局限与未决问题 → 使用声明。

必读的三处表述：

1. **历史回放 / 离线数据**：本次不是实时推送数据。
2. **步长**：1 / 3 / 6 是步长，采样频率被证实前不称小时。
3. **桶内最后一次观测**：存在至多一个桶宽的有界前瞻。

## 6. 缺失值的两种 `null`

| 情形 | 表现 | 处理 |
| --- | --- | --- |
| 目标方差为 0 | `r2` / `nse` 为 `null`，其余指标有值 | 数学上无定义，不填 0 |
| 样本不足 | `sample_count=0`，全部指标为 `null`，`reason=insufficient_samples` | 不填 0、不跳过该步长、不让管线崩溃 |

## 7. 使用限制

本报告与产物仅用于教学、科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策；不得冒充官方发布预警。
