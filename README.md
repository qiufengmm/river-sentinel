# River Sentinel

基于多源水雨情融合的飞云江水位预测与防汛辅助决策智能体系统。

项目是浙江水利水电学院人工智能专业本科毕业设计，同时复用成果完成相关课程实训。系统仅用于教学、科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。

## 当前里程碑

当前阶段建设飞云江二期治理工程水位数据的可追溯基线：

1. 从获批API或本地文件导入数据；
2. 保存不可变原始快照和来源清单；
3. 完成字段规范化、质量审计和时间切分；
4. 生成持久性预测及评价报告。

开放平台中的“实时水位”是源数据字段名称，不代表平台向本系统持续实时推送。离线演示和历史过程重放必须标注为“历史回放”。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check ml
```

需要调用温州市公共数据开放平台API时，将获批密钥配置到本地 `.env` 的 `WENZHOU_DATA_APPSECRET`。不得将 `.env` 或密钥提交到Git。

## 命令行用法（Task 8 起启用，已实测）

安装方式二选一：`pip install -e ".[dev]"` 后直接运行 `river-sentinel-data`；或免安装直跑
（Windows PowerShell，已实测可用）：

```powershell
$env:PYTHONPATH="<仓库路径>\ml\src"
python -m river_sentinel_ml.cli audit-file --input data/samples/water_level_sample.csv --output-dir build/audit
python -m river_sentinel_ml.cli audit-api --output-dir build/audit-real   # 需要 WENZHOU_DATA_APPSECRET
```

- 成功时产物写入 `--output-dir/run-<UTC时间戳>/`（同秒追加序号，绝不覆盖历史产物），stdout 打印该目录绝对路径，退出码 0；
- 参数错误退出码 2；缺凭据或管线失败退出码 1（中文错误，凭据零回显，缺凭据时不产生任何产物）；
- 产物布局与字段口径见 `docs/experiments/baseline-report-format.md`，数据源治理记录见 `docs/data-governance/source-cata-12720.md`，里程碑验收结论见 `docs/experiments/data-baseline-acceptance.md`。

## 设计资料

- 设计规格：`docs/superpowers/specs/2026-09-16-river-sentinel-project-design.md`
- 数据基线计划：`docs/superpowers/plans/2026-09-18-feiyunjiang-data-baseline.md`
