# 数据基线里程碑验收记录（Task 9）

- 验收日期：2026-09-21
- 验收对象：`docs/superpowers/plans/2026-09-18-feiyunjiang-data-baseline.md` 全部 9 个任务
- 分支：`main`
- 提交：功能提交 `61828d4`（Task 8，7 文件 +2191 行）、合并提交 `1e77dce`；此前 Task 1-7 已依次合并
- 验收方式：主 Agent 在 `main` 上直接执行并实测取证，未转派执行 Agent；本文所有数字均为当次实测输出，无引用历史结论
- 解释器：`C:\Users\qiufengm\anaconda3\python.exe`（Python 3.13.9，pandas 2.3.3，ruff 0.12.0，pytest-cov 为本次验收安装）

## 一、自动化验证（命令与实际输出）

| 命令 | 实际输出 |
| --- | --- |
| `python -m pytest --cov=river_sentinel_ml --cov-report=term-missing -q` | `216 passed`；覆盖率 `TOTAL 1221 stmts / 50 miss / 96%`（未预设门槛，如实记录） |
| `python -m ruff check ml` | `All checks passed!` |
| `python -m ruff format --check ml` | `27 files already formatted` |
| `git diff --check` | 无输出，退出码 0 |

覆盖率分层记录（供后续任务对照）：`provenance.py` / `baseline.py` / `metrics.py` 100%；`contracts.py` 99%、`reporting.py` 99%、`cli.py` 98%、`dataset.py` 97%、`pipeline.py` 96%、`normalize.py` 90%、`wenzhou_api.py` 89%、`files.py` 85%。未覆盖行集中在网络失败分支与防御性路径。

## 二、端到端冒烟（干净目录，2026-09-21 实测）

命令：`river-sentinel-data audit-file --input data/samples/water_level_sample.csv --output-dir build/acceptance-sample`
（已 `pip install -e . --no-deps` 安装，同时验证了脚本入口 `river-sentinel-data.exe` 退出码 0；免安装方式 `PYTHONPATH=ml/src` + `python -m river_sentinel_ml.cli` 同样实测通过）

- 退出码 0；run 目录 `build/acceptance-sample/run-20260921T031343Z/`，内含 7 个产物文件（5 类 + 原始快照 + 来源清单）
- 快照 SHA-256：`7500a1c509d9c35ca23b6c0a74c07f1ccdb3af30767498985aea35d63b087a8f`
- `Get-FileHash quality-report.json`：`AC46789F014BFB64770DC292309381E17C92BB6F3B0821FEE36165DDBFAA7B55`
- `Get-FileHash baseline-metrics.json`：`F20ABE728536B73114FE33F3C0E86B65BA3C8DEF10D7EF91F6537AC21763BE0E`
- stdout 含"历史回放 / 离线数据"提示；产物内 `code_commit` 记录合并提交 `1e77dce…`

## 三、真实接口状态（`API_AUTH_PENDING`）

命令：`river-sentinel-data audit-api --output-dir build/acceptance-real`

- 本地 `.env` 不存在、环境变量未设置
- 实际行为：退出码 1，stderr 输出「缺少访问凭据：请设置环境变量 WENZHOU_DATA_APPSECRET 后重试（凭据不会写入任何产物、日志或异常消息）」，**未创建** `build/acceptance-real` 目录
- 结论：凭据未获批前不产生任何真实接口数据结论，阻塞条件如实记录为 `API_AUTH_PENDING`

## 四、数据源、质量与推断频率（基于合成样例的实测）

数据源 `cata_12720`（文成县飞云江二期治理工程水位信息，见 `docs/data-governance/source-cata-12720.md`）；下表数字来自本次冒烟 run 目录的 `quality-report.json` 与 `baseline-metrics.json`，样例为**合成演示数据**（刻意含 1 个重复 `z_id`、1 个缺失水位、1 个非法时间戳）：

| 项 | 实测值 |
| --- | --- |
| 总行数 / 有效行 | 12 / 9 |
| 重复 / 非法时间戳 / 非法水位 / 缺失水位 | 1 / 1 / 0 / 1 |
| 时间范围（Asia/Shanghai） | 2026-01-01 00:00 – 11:00 |
| 推断采样间隔 | 60.0 分钟（频率 1h） |
| 重采样网格 / 插补行 / 最长连续缺口 | 12 点 / 3 行 / 1 步 |
| 时间顺序切分 | train=8 / validation=1 / test=3 |
| test 段 1/3/6 步 MAE | 0.0500 / 0.1883 / 0.4283 |
| test 段 1/3/6 步 RMSE | 0.0526 / 0.1908 / 0.4293 |
| test 段 1/3/6 步 R²=NSE | −1.54 / −32.44 / −168.26 |
| test 段 1/3/6 步峰值绝对误差 | 0.03 / 0.15 / 0.39 |
| validation 段 | 样本不足（`insufficient_samples`，指标为 `null`，不填 0） |

目标站身份：样例**不含站名/断面字段**（`station_code=null`，管线以 `station_ids=("unknown",)` 兜底并标注）；真实测站身份须待 `audit-api` 获批后按平台元数据确认，本记录不推断。

## 五、验收裁定

### 1. 「1/3/6」是步长还是小时？

**当前口径为"步长"，条件性等价于小时，暂不升级为无条件"小时"表述。**

- 依据：样例推断频率 60.0 分钟，在 1h 网格下 1/3/6 步长在数值上等于 1/3/6 小时；
- 但样例是人为构造的合成序列，其频率**不构成真实频率证据**；真实频率须由 `audit-api` 实测确认；
- 落地规则（已内置于 `baseline-metrics.json` 的 `step_unit` 注释）：论文与下游表述为"步长（当次运行推断频率 1h）"时可换算为小时；仅当真实数据实测频率确认为小时级后，才可直接称"1/3/6 小时"。

### 2. 水位基线是否足够？

**足够（作为保底闭环与对照下限）。**

单源水位"导入→快照→规范化→质量审计→时间顺序切分→持久性基线→四类产物"全链路可复现（同一命令两次运行产物确定性一致）；指标含 MAE/RMSE/R²/NSE/峰值误差且失败样本不填 0。样例上 R²/NSE 为负属单调合成序列 + 测试段方差极小的预期现象，仅作后续 XGBoost/LSTM 的对照下限，不构成模型评估结论。

### 3. 是否授权开始降雨候选发现？

**授权开始，附条件。**

- 水位单源数据已通过准入（本文档 + `source-cata-12720.md` + 可运行闭环）；
- 候选发现可先开展：平台目录检索、雨量/流量候选站清单、人工复核测站关系表框架、时滞分析与准入审计规则设计；
- **附条件**：任何真实数据抓取与准入审计须待 `API_AUTH_PENDING` 解除；若最终无降雨数据通过准入，按设计规格降级为"基于飞云江二期治理工程数据的水位预测与防汛辅助决策智能体系统"，不得为保留多源题目伪造或不合理插补数据。

## 六、限制与阻塞

1. `API_AUTH_PENDING`：温州平台凭据未获批，`audit-api` 路径无真实数据结论（阻塞项，需平台侧申请）；
2. 样例为合成数据：本记录的频率、质量统计与指标仅证明管线正确性，不得作为真实水文结论引用；
3. 样例规模小：validation 段仅 1 行（样本不足记 `null`），test 段 3 行，不足以支撑任何泛化性结论；
4. 目标站身份未确认：待真实接口获批后补充；
5. 每次运行保留当次原始快照、不做跨 run 去重：长期运行需自行清理旧 run 目录。

## 七、最终验收标准核验（计划第 12 节逐条）

| # | 标准 | 结论 |
| --- | --- | --- |
| 1 | 可安装并通过完整 Pytest 与 Ruff | 通过（`pip install -e . --no-deps` + 脚本入口实测；216 passed；ruff 双绿） |
| 2 | 文件与官方 API 共用同一管线 | 通过（`cli.py` 两子命令共用 `pipeline.py`） |
| 3 | 单页 ≤200 且失败不泄漏密钥 | 通过（`MAX_PAGE_SIZE` + `iter_rows` 校验 + `--page-size` 越界显式中文报错，不静默截断） |
| 4 | 快照不可覆盖且带清单与 SHA-256 | 通过（排他创建 + run 目录唯一，清单含 `sha256`） |
| 5 | 无效时间/水位/重复/缺失可复核 | 通过（quality-report 全量统计，见第四节） |
| 6 | 时间顺序切分、无随机切分与未来泄漏 | 通过（`chronological_split`，8/1/3） |
| 7 | 1/3/6 步持久性指标可重复生成 | 通过（两次运行产物逐字节一致，测试固化） |
| 8 | 区分字段"实时水位"、历史回放与真实实时 | 通过（口径警示 + CLI 输出提示） |
| 9 | 无官方阈值不产预警 | 通过（系统不产生任何预警声明） |
| 10 | API 未获批时保留可运行样例闭环并记录 | 通过（本文档第三节） |
| 11 | Git 不含密钥/批量数据/生成报告/权重 | 通过（`.env` 不存在；`build/` 被忽略；本次验收仅提交本文档与 README） |
| 12 | 为多源降雨/流量匹配计划提供依据 | 通过（第四节实测频率与质量统计 + 第五节裁定 3 的附条件授权） |

**结论：数据基线计划（Task 1-9）验收通过。下一步为编制多源降雨/流量匹配计划，其真实数据获取受 `API_AUTH_PENDING` 制约。**
