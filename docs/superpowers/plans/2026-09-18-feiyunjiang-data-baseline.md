# 飞云江数据基线与项目基础设施实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可运行、可测试、可追溯的 Python 数据基线，完成飞云江二期治理工程水位数据的文件/API导入、原始快照、质量审计、小时级序列、时间切分、持久性预测和审计报告闭环。

**Architecture:** 第一里程碑只建设数据与算法基础包，不提前实现多源建模、智能体或前端。外部数据先通过适配器转换为统一记录，再依次经过不可变原始快照、规范化、质量审计、时间重采样和按时间切分；持久性模型消费统一数据集并生成可追溯指标。真实采样频率、测站范围和缺失情况由本里程碑产出的审计报告决定后续计划。

**Tech Stack:** Python 3.11+、Pandas、NumPy、Pydantic 2、pydantic-settings、HTTPX、PyArrow、Scikit-learn、Pytest、Ruff、Git

**Spec:** `docs/superpowers/specs/2026-09-16-river-sentinel-project-design.md`

## Global Constraints

- 核心数据源固定为温州市公共数据开放平台数据集 `12720`，API目录为 `cata_12720`。
- 水位单源预测是保底闭环；降雨融合是后续核心提升；流量融合是数据满足准入条件时的加分项。
- 本里程碑只使用预测时刻及以前的数据，训练、验证、测试必须按时间顺序切分。
- 原始数据不可覆盖；每次导入必须记录来源、获取时间、请求参数、行数和 SHA-256。
- `appsecret` 只从环境变量读取，禁止写入代码、日志、报告、测试快照和 Git。
- 真实批量数据、模型权重、缓存、数据库、密钥和本地环境文件不得进入 Git。
- “实时水位”是源字段名称，不代表平台提供持续实时推送；历史演示必须标注“历史回放”。
- 不得使用统计阈值冒充官方警戒水位。
- 禁止批量删除文件或目录，禁止使用 `del /s`、`rd /s`、`rmdir /s`、`Remove-Item -Recurse` 和 `rm -rf`。
- 每个任务遵循测试先行、失败确认、最小实现、完整验证和中文逻辑提交。

## 里程碑拆分

本规格后续拆为独立计划，只有前一里程碑的真实产物满足准入条件后才编写下一份详细计划：

1. 本计划：项目基础设施、飞云江水位导入、数据审计和持久性基线；
2. 多源数据计划：雨量/流量候选站、人工复核关系、时滞和消融数据集；
3. 模型计划：XGBoost、LSTM、确定性融合和典型涨水过程评价；
4. 后端与智能体计划：预测服务、规则引擎、LangGraph工具调用和简报；
5. 前端与验收计划：Vue监测页面、历史回放、模型对比、系统测试和答辩材料。

---

### Task 1: 建立可安装的算法包和安全仓库骨架

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `.env.example`
- Modify: `.gitignore`
- Create: `ml/src/river_sentinel_ml/__init__.py`
- Create: `ml/tests/test_package_metadata.py`
- Create: `data/README.md`
- Create: `data/samples/README.md`
- Create: `docs/data-governance/README.md`
- Create: `docs/experiments/README.md`

**Interfaces:**
- Consumes: Python 3.11+ and the approved design specification.
- Produces: importable package `river_sentinel_ml`, project metadata, dependency groups, safe environment template, and directories used by later tasks.

- [ ] **Step 1: Write the failing package metadata test**

`ml/tests/test_package_metadata.py`:

```python
from river_sentinel_ml import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"
```

- [ ] **Step 2: Run the test and confirm the package is absent**

Run:

```powershell
python -m pytest ml/tests/test_package_metadata.py -v
```

Expected: FAIL during import because `river_sentinel_ml` is not installed.

- [ ] **Step 3: Create packaging and dependency configuration**

`pyproject.toml` must define:

```toml
[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "river-sentinel-ml"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27,<1",
  "numpy>=2,<3",
  "pandas>=2.2,<3",
  "pyarrow>=17,<20",
  "pydantic>=2.9,<3",
  "pydantic-settings>=2.5,<3",
  "scikit-learn>=1.5,<2",
]

[project.optional-dependencies]
dev = [
  "pytest>=8,<9",
  "pytest-cov>=5,<7",
  "ruff>=0.6,<1",
]

[project.scripts]
river-sentinel-data = "river_sentinel_ml.cli:main"

[tool.setuptools]
package-dir = {"" = "ml/src"}

[tool.setuptools.packages.find]
where = ["ml/src"]

[tool.pytest.ini_options]
testpaths = ["ml/tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py311"
```

`ml/src/river_sentinel_ml/__init__.py`:

```python
__version__ = "0.1.0"
```

`.env.example` contains only:

```dotenv
WENZHOU_DATA_APPSECRET=
RIVER_SENTINEL_TIMEZONE=Asia/Shanghai
```

Extend `.gitignore` with explicit entries for `.env`, `.venv/`, `data/raw/`, `data/interim/`, `data/processed/`, `ml/artifacts/`, `*.db`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` and coverage output. Keep the existing `.worktrees/` rule.

`README.md` must state the project title, teaching/research disclaimer, current milestone, Python setup commands and the distinction between source field “实时水位” and actual live streaming. Directory README files must explain what is allowed in Git; only small synthetic or legally redistributable samples may enter `data/samples`.

- [ ] **Step 4: Install the package and run the metadata test**

Run:

```powershell
python -m pip install -e ".[dev]"
python -m pytest ml/tests/test_package_metadata.py -v
```

Expected: PASS.

- [ ] **Step 5: Run formatting/static checks and commit**

Run:

```powershell
python -m ruff check ml
git diff --check
git add pyproject.toml README.md .env.example .gitignore ml data/README.md data/samples/README.md docs/data-governance/README.md docs/experiments/README.md
git commit -m "chore: 初始化飞云江数据工程骨架"
```

Expected: Ruff and diff checks pass; the commit contains no environment secret or bulk data.

---

### Task 2: 定义配置、统一水位记录和来源清单契约

**Files:**
- Create: `ml/src/river_sentinel_ml/config.py`
- Create: `ml/src/river_sentinel_ml/contracts.py`
- Create: `ml/tests/test_config.py`
- Create: `ml/tests/test_contracts.py`

**Interfaces:**
- Consumes: environment variables `WENZHOU_DATA_APPSECRET` and `RIVER_SENTINEL_TIMEZONE`.
- Produces: `Settings`, `WaterLevelRecord`, `SourceManifest`, `QualitySummary`, and `ExperimentManifest`.

- [ ] **Step 1: Write failing configuration and contract tests**

`ml/tests/test_config.py`:

```python
from river_sentinel_ml.config import Settings


def test_secret_is_optional_for_file_import(monkeypatch) -> None:
    monkeypatch.delenv("WENZHOU_DATA_APPSECRET", raising=False)
    settings = Settings(_env_file=None)
    assert settings.wenzhou_data_appsecret is None
    assert settings.timezone == "Asia/Shanghai"
```

`ml/tests/test_contracts.py`:

```python
from datetime import datetime

import pytest
from pydantic import ValidationError

from river_sentinel_ml.contracts import WaterLevelRecord


def test_water_level_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        WaterLevelRecord(
            source_record_id="1",
            observed_at=datetime(2026, 1, 1, 8, 0),
            water_level_m=3.2,
        )
```

- [ ] **Step 2: Run tests and confirm missing modules**

Run:

```powershell
python -m pytest ml/tests/test_config.py ml/tests/test_contracts.py -v
```

Expected: FAIL because `config` and `contracts` do not exist.

- [ ] **Step 3: Implement exact settings and Pydantic models**

`Settings` fields:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    wenzhou_data_appsecret: SecretStr | None = None
    timezone: str = Field(default="Asia/Shanghai", alias="RIVER_SENTINEL_TIMEZONE")
```

`WaterLevelRecord` fields:

```python
class WaterLevelRecord(BaseModel):
    source_record_id: str
    observed_at: datetime
    water_level_m: float
    station_code: str | None = None
    station_name: str | None = None
    is_imputed: bool = False
    quality_flag: Literal["normal", "suspected", "invalid", "imputed"] = "normal"
```

Add a validator rejecting timezone-naive `observed_at`. Define:

```python
class SourceManifest(BaseModel):
    source_name: str
    source_uri: str = Field(min_length=1)
    dataset_id: str
    acquired_at: datetime
    request_parameters: dict[str, str | int]
    record_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class QualitySummary(BaseModel):
    total_rows: int
    valid_rows: int
    duplicate_rows: int
    invalid_timestamp_rows: int
    invalid_level_rows: int
    missing_level_rows: int
    start_at: datetime | None
    end_at: datetime | None
    inferred_interval_minutes: float | None


class ExperimentManifest(BaseModel):
    experiment_id: str
    created_at: datetime
    dataset_sha256: str
    horizons: tuple[int, ...]
    train_range: tuple[datetime, datetime]
    validation_range: tuple[datetime, datetime]
    test_range: tuple[datetime, datetime]
    code_commit: str
```

- [ ] **Step 4: Run contract tests**

Run:

```powershell
python -m pytest ml/tests/test_config.py ml/tests/test_contracts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit the contracts**

Run:

```powershell
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/config.py ml/src/river_sentinel_ml/contracts.py ml/tests/test_config.py ml/tests/test_contracts.py
git commit -m "feat: 定义水位数据与实验追溯契约"
```

---

### Task 3: 实现温州开放数据API与本地文件适配器

**Files:**
- Create: `ml/src/river_sentinel_ml/ingestion/__init__.py`
- Create: `ml/src/river_sentinel_ml/ingestion/wenzhou_api.py`
- Create: `ml/src/river_sentinel_ml/ingestion/files.py`
- Create: `ml/tests/ingestion/test_wenzhou_api.py`
- Create: `ml/tests/ingestion/test_files.py`
- Create: `data/samples/water_level_sample.csv`

**Interfaces:**
- Consumes: official keys `time_d`, `up_water_level`, `z_id`; optional station fields `station_code` and `station_name`.
- Produces: `WenzhouWaterLevelClient.get_total() -> int`, `WenzhouWaterLevelClient.get_update_date() -> date`, `WenzhouWaterLevelClient.iter_rows(page_size: int = 200) -> Iterator[dict[str, object]]`, and `load_water_level_file(path: Path) -> list[dict[str, object]]`.

- [ ] **Step 1: Write failing API pagination tests**

Use `httpx.MockTransport` so tests never call the government platform. Verify:

```python
def test_page_size_cannot_exceed_platform_limit() -> None:
    client = WenzhouWaterLevelClient("secret", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="page_size"):
        list(client.iter_rows(page_size=201))
```

Also test two pages, the platform update-date response, `status != 1`, malformed `data`, request timeout conversion to `SourceAccessError`, and that exception messages never contain the supplied secret.

- [ ] **Step 2: Run adapter tests and confirm failure**

Run:

```powershell
python -m pytest ml/tests/ingestion -v
```

Expected: FAIL because adapters are absent.

- [ ] **Step 3: Implement the API client**

Use constants:

```python
BASE_URL = "https://data.wenzhou.gov.cn/jdop_front/interfaces/cata_12720"
TOTAL_PATH = "/get_total.do"
DATA_PATH = "/get_data.do"
UPDATE_PATH = "/get_dataupdate_date.do"
MAX_PAGE_SIZE = 200
```

The client constructor accepts a secret and optional `httpx.BaseTransport`. Requests use query parameters, a 30-second timeout, and `response.raise_for_status()`. Validate the platform envelope `{"status": 1, "data": ...}`; convert HTTP, timeout, JSON and envelope failures into `SourceAccessError` with a sanitized message. Pagination stops after the known total count or an empty page.

- [ ] **Step 4: Implement file loading and a safe sample**

`load_water_level_file` accepts only `.csv`, `.json` and `.parquet`; unsupported extensions raise `ValueError`. The committed sample contains 12 synthetic hourly rows with the exact official column names, a visible comment in `data/samples/README.md` that values are synthetic, one duplicate ID, one missing level and one invalid timestamp so audit behavior is reproducible.

- [ ] **Step 5: Verify adapters and commit**

Run:

```powershell
python -m pytest ml/tests/ingestion -v
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/ingestion ml/tests/ingestion data/samples
git commit -m "feat: 接入飞云江水位API与文件数据"
```

Expected: tests pass without network access and no secret appears in `git diff`.

---

### Task 4: 建立不可变原始快照和来源追溯

**Files:**
- Create: `ml/src/river_sentinel_ml/provenance.py`
- Create: `ml/tests/test_provenance.py`

**Interfaces:**
- Consumes: iterable raw rows, source URI, request parameters and destination root.
- Produces: `write_raw_snapshot(rows, metadata, root) -> tuple[Path, Path, SourceManifest]`; the two paths are an NDJSON snapshot and adjacent manifest JSON.

- [ ] **Step 1: Write failing immutability and checksum tests**

Test that the function:

- writes deterministic UTF-8 NDJSON with sorted keys;
- calculates SHA-256 from exact snapshot bytes;
- stores no `appsecret` key in manifest request parameters;
- refuses to overwrite an existing snapshot path;
- returns an aware UTC `acquired_at`.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```powershell
python -m pytest ml/tests/test_provenance.py -v
```

Expected: FAIL because `provenance.py` is absent.

- [ ] **Step 3: Implement deterministic snapshot naming**

Use:

```text
data/raw/wenzhou/cata_12720/YYYY/MM/DD/
  YYYYMMDDTHHMMSSZ-<first12sha>.ndjson
  YYYYMMDDTHHMMSSZ-<first12sha>.manifest.json
```

Serialize one JSON object per line with `ensure_ascii=False`, `sort_keys=True`, and a final newline. Write with exclusive creation mode so an existing path raises `FileExistsError`. Remove keys whose lowercase names equal `appsecret`, `token`, `authorization` or `password` before building `SourceManifest`.

- [ ] **Step 4: Verify provenance behavior and commit**

Run:

```powershell
python -m pytest ml/tests/test_provenance.py -v
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/provenance.py ml/tests/test_provenance.py
git commit -m "feat: 增加原始数据快照与来源追溯"
```

---

### Task 5: 规范化飞云江水位字段并生成质量审计

**Files:**
- Create: `ml/src/river_sentinel_ml/quality.py`
- Create: `ml/src/river_sentinel_ml/normalize.py`
- Create: `ml/tests/test_normalize.py`
- Create: `ml/tests/test_quality.py`

**Interfaces:**
- Consumes: raw rows using official fields and timezone name.
- Produces: `normalize_water_level_rows(rows, timezone) -> tuple[pd.DataFrame, QualitySummary]` with columns `source_record_id`, `observed_at`, `water_level_m`, `station_code`, `station_name`, `quality_flag`.

- [ ] **Step 1: Write failing normalization tests**

Tests must prove:

- valid timestamps become timezone-aware Asia/Shanghai datetimes;
- water level strings become floats;
- duplicate `z_id` keeps the last valid occurrence and increments `duplicate_rows`;
- invalid timestamp rows and non-numeric levels are excluded from valid output and counted separately;
- missing levels are counted, not converted to zero;
- rows preserve optional station values when the source provides them.

- [ ] **Step 2: Write failing interval inference tests**

`infer_interval_minutes(index: DatetimeIndex) -> float | None` returns the median positive interval in minutes, ignores duplicate/non-positive differences, and returns `None` for fewer than two unique times.

- [ ] **Step 3: Run tests and confirm missing implementation**

Run:

```powershell
python -m pytest ml/tests/test_normalize.py ml/tests/test_quality.py -v
```

Expected: FAIL because functions are absent.

- [ ] **Step 4: Implement normalization without long-gap imputation**

Map `z_id -> source_record_id`, `time_d -> observed_at`, and `up_water_level -> water_level_m`. Parse timestamps with `errors="coerce"`, localize naive source timestamps to the configured timezone, and reject ambiguous/nonexistent local times. Parse levels with `pd.to_numeric(errors="coerce")`. Do not interpolate during normalization. Sort valid output by `observed_at` then `source_record_id`.

- [ ] **Step 5: Verify audit output and commit**

Run:

```powershell
python -m pytest ml/tests/test_normalize.py ml/tests/test_quality.py -v
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/quality.py ml/src/river_sentinel_ml/normalize.py ml/tests/test_normalize.py ml/tests/test_quality.py
git commit -m "feat: 实现水位规范化与质量审计"
```

---

### Task 6: 构建因果小时序列和时间顺序切分

**Files:**
- Create: `ml/src/river_sentinel_ml/dataset.py`
- Create: `ml/tests/test_dataset.py`

**Interfaces:**
- Consumes: normalized frame from Task 5.
- Produces: `resample_water_level(frame, frequency="1h", max_interpolation_steps=2) -> pd.DataFrame` and `chronological_split(frame, train_ratio=0.70, validation_ratio=0.15) -> DatasetSplit`.

- [ ] **Step 1: Write failing resampling tests**

Create a 15-minute series with one short gap and one four-hour gap. Assert:

- hourly water level is the final observed value within each closed hour;
- only gaps of at most two consecutive hourly steps are linearly interpolated;
- interpolated rows have `is_imputed=True` and `quality_flag="imputed"`;
- the four-hour gap remains missing;
- no value after a gap is used to fill observations before the prediction origin in feature construction.

- [ ] **Step 2: Write failing chronological split tests**

Assert train, validation and test are non-empty; each partition is sorted; `train.max_time < validation.min_time < test.min_time`; and the union contains every input timestamp exactly once.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```powershell
python -m pytest ml/tests/test_dataset.py -v
```

Expected: FAIL because dataset functions are absent.

- [ ] **Step 4: Implement resampling and split contracts**

Define:

```python
@dataclass(frozen=True)
class DatasetSplit:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
```

Reject duplicate timestamps before splitting. Calculate integer boundaries with at least one record in every partition; raise `ValueError` when fewer than seven usable rows remain. Do not shuffle.

- [ ] **Step 5: Verify dataset behavior and commit**

Run:

```powershell
python -m pytest ml/tests/test_dataset.py -v
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/dataset.py ml/tests/test_dataset.py
git commit -m "feat: 构建因果水位序列与时间切分"
```

---

### Task 7: 实现持久性基线和水文评价指标

**Files:**
- Create: `ml/src/river_sentinel_ml/baseline.py`
- Create: `ml/src/river_sentinel_ml/metrics.py`
- Create: `ml/tests/test_baseline.py`
- Create: `ml/tests/test_metrics.py`

**Interfaces:**
- Consumes: hourly series and horizon steps.
- Produces: `persistence_forecast(series, horizons=(1, 3, 6)) -> pd.DataFrame` and `evaluate_forecasts(frame, horizons) -> dict[int, ForecastMetrics]`.

- [ ] **Step 1: Write failing persistence tests**

For series `[1.0, 1.2, 1.5, 1.4]`, horizon 1 prediction at time index 1 must equal the observed value at index 0. Horizon 3 predictions must use only the value available three steps earlier. Missing targets or predictions are excluded pairwise and counted.

- [ ] **Step 2: Write failing metric tests**

Define:

```python
@dataclass(frozen=True)
class ForecastMetrics:
    sample_count: int
    mae: float
    rmse: float
    r2: float | None
    nse: float | None
    peak_absolute_error: float
```

Tests use hand-computed arrays for MAE/RMSE and verify R²/NSE return `None` when the target variance is zero. Peak absolute error compares the maximum observed target with the maximum prediction over the same valid pairs.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```powershell
python -m pytest ml/tests/test_baseline.py ml/tests/test_metrics.py -v
```

Expected: FAIL because baseline and metric modules are absent.

- [ ] **Step 4: Implement causal forecasts and metrics**

Output columns use exact names `observed`, `prediction_h1`, `prediction_h3`, and `prediction_h6`. Preserve the observation timestamp as index. Evaluation must reject horizons not present in the frame and never replace missing predictions with observations.

- [ ] **Step 5: Verify baseline behavior and commit**

Run:

```powershell
python -m pytest ml/tests/test_baseline.py ml/tests/test_metrics.py -v
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/baseline.py ml/src/river_sentinel_ml/metrics.py ml/tests/test_baseline.py ml/tests/test_metrics.py
git commit -m "feat: 增加水位持久性基线与评价指标"
```

---

### Task 8: 提供端到端命令和可追溯审计报告

**Files:**
- Create: `ml/src/river_sentinel_ml/reporting.py`
- Create: `ml/src/river_sentinel_ml/pipeline.py`
- Create: `ml/src/river_sentinel_ml/cli.py`
- Create: `ml/tests/test_pipeline.py`
- Create: `ml/tests/test_cli.py`
- Create: `docs/data-governance/source-cata-12720.md`
- Create: `docs/experiments/baseline-report-format.md`

**Interfaces:**
- Consumes: local data file or approved API credentials.
- Produces: processed Parquet, `quality-report.json`, `baseline-metrics.json`, `experiment-manifest.json`, and `baseline-report.md`.

- [ ] **Step 1: Write a failing sample-file integration test**

Call:

```python
result = run_file_baseline(
    input_path=sample_path,
    output_dir=tmp_path,
    timezone="Asia/Shanghai",
    frequency="1h",
    horizons=(1, 3, 6),
)
```

Assert every declared artifact exists, JSON validates through Task 2 contracts, the report states “历史回放/离线数据”, and no artifact contains `appsecret`.

- [ ] **Step 2: Write failing CLI tests**

Test these commands through `main([...])`:

```text
river-sentinel-data audit-file --input data/samples/water_level_sample.csv --output-dir build/audit
river-sentinel-data audit-api --output-dir build/audit
```

The API command must exit with a clear Chinese error when `WENZHOU_DATA_APPSECRET` is absent. The error must instruct the user to configure the environment variable without echoing its value.

- [ ] **Step 3: Run integration tests and confirm failure**

Run:

```powershell
python -m pytest ml/tests/test_pipeline.py ml/tests/test_cli.py -v
```

Expected: FAIL because pipeline, report and CLI modules are absent.

- [ ] **Step 4: Implement the orchestration pipeline**

`run_file_baseline` executes adapters in this exact order:

1. load source rows;
2. write immutable raw snapshot and manifest;
3. normalize and create `QualitySummary`;
4. resample and create chronological split;
5. run persistence forecasts on validation and test partitions;
6. calculate metrics;
7. capture current Git commit with `git rev-parse HEAD`;
8. write atomic JSON/Parquet/Markdown artifacts.

Use a temporary filename in the same output directory and `Path.replace()` for atomic report writes. On failure, retain the immutable raw snapshot but do not leave partial processed or metric files.

- [ ] **Step 5: Write exact governance documentation**

`source-cata-12720.md` records provider, dataset/interface IDs, source URLs, documented fields, access approval, maximum page size 200, acquisition timestamp location, checksum location, update-frequency caveat and research-only use. `baseline-report-format.md` explains every quality and forecast field and states that horizons are time steps until actual sampling frequency proves they represent hours.

- [ ] **Step 6: Run the full sample pipeline**

Run:

```powershell
river-sentinel-data audit-file --input data/samples/water_level_sample.csv --output-dir build/audit
Get-ChildItem build/audit
Get-Content -Raw build/audit/baseline-report.md
```

Expected: command exits 0; all five artifacts exist; report clearly labels synthetic sample and historical/offline mode.

- [ ] **Step 7: Verify and commit the pipeline**

Run:

```powershell
python -m pytest ml/tests/test_pipeline.py ml/tests/test_cli.py -v
python -m ruff check ml
git diff --check
git add ml/src/river_sentinel_ml/reporting.py ml/src/river_sentinel_ml/pipeline.py ml/src/river_sentinel_ml/cli.py ml/tests/test_pipeline.py ml/tests/test_cli.py docs/data-governance/source-cata-12720.md docs/experiments/baseline-report-format.md
git commit -m "feat: 打通飞云江数据审计与基线报告"
```

Do not add `build/audit`; generated reports are local evidence unless a later documentation task explicitly selects sanitized artifacts.

---

### Task 9: 完成里程碑验收和下一阶段准入结论

**Files:**
- Create: `docs/experiments/data-baseline-acceptance.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all tests, sample pipeline outputs, and—when the user has locally configured an approved secret—the real API audit outputs.
- Produces: evidence-backed acceptance record deciding whether 1/3/6 means hours or steps and whether rainfall discovery may start.

- [ ] **Step 1: Run the complete automated verification**

Run:

```powershell
python -m pytest --cov=river_sentinel_ml --cov-report=term-missing
python -m ruff check ml
git diff --check
```

Expected: all tests pass, Ruff reports no errors, and Git reports no whitespace errors. Record exact test count and coverage in the acceptance document; do not invent a minimum coverage percentage before seeing the result.

- [ ] **Step 2: Run the synthetic end-to-end smoke test from a clean output directory**

Use a new explicit directory name such as `build/acceptance-sample`; do not recursively delete an old directory.

```powershell
river-sentinel-data audit-file --input data/samples/water_level_sample.csv --output-dir build/acceptance-sample
Get-FileHash build/acceptance-sample/quality-report.json -Algorithm SHA256
Get-FileHash build/acceptance-sample/baseline-metrics.json -Algorithm SHA256
```

Expected: exit 0 and both hashes are printed.

- [ ] **Step 3: Run the real-data audit only when authorization is available**

```powershell
river-sentinel-data audit-api --output-dir build/acceptance-real
```

If the environment variable or platform approval is absent, record the exact blocking condition as `API_AUTH_PENDING`; do not substitute scraped webpage requests or fabricated data. If access succeeds, record row count, time range, inferred interval, duplicate/missing/invalid rates, station fields, long gaps and SHA-256 without committing bulk artifacts.

- [ ] **Step 4: Write the acceptance decision**

`data-baseline-acceptance.md` must contain:

- branch and commit hashes;
- commands and actual outputs;
- source ID and acquisition state;
- inferred sampling frequency;
- target station identity evidence or explicit absence;
- quality statistics and largest continuous gaps;
- whether horizons can be called 1/3/6 hours;
- whether the water-level baseline is sufficient;
- whether rainfall candidate discovery is authorized to begin;
- limitations and blockers.

Update `README.md` with verified setup and CLI commands only.

- [ ] **Step 5: Commit the acceptance record**

Run:

```powershell
git add README.md docs/experiments/data-baseline-acceptance.md
git commit -m "docs: 记录飞云江数据基线验收结论"
git status --short
git log --oneline -10
```

Expected: source files are clean. Generated data directories remain ignored and no secret, bulk data or model artifact is tracked.

## 最终验收标准

完成本计划必须同时满足：

1. `river_sentinel_ml` 可安装并通过完整 Pytest 与 Ruff 检查；
2. 本地文件和官方API共享同一规范化、审计与基线管线；
3. API单页不超过200条且失败信息不泄漏密钥；
4. 原始快照不可覆盖并带来源清单与SHA-256；
5. 无效时间、水位、重复和缺失均有可复核统计；
6. 数据按时间顺序切分，没有随机切分和未来信息泄漏；
7. 1/3/6步持久性预测与MAE、RMSE、R²、NSE、峰值误差可重复生成；
8. 报告明确区分官方字段“实时水位”、历史回放与真实实时数据；
9. 没有官方阈值时不产生或声称正式预警；
10. 真实API未获批时，项目保留可运行样例闭环并明确记录 `API_AUTH_PENDING`；
11. Git不包含密钥、真实批量数据、生成报告、数据库、缓存或模型权重；
12. 验收记录能够为下一份“多源降雨/流量匹配计划”提供真实采样频率、时间范围和质量依据。
