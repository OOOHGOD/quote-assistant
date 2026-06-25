# Automatic Order Assistant / Quote Assistant

本项目是一个本地运行的自动报价单整理工具，用于把供应商 PDF 报价单整理成结构化 JSON，并按固定 Excel 模板生成报价单副本。

当前主流程不依赖 Google Drive 等云端文件存储。文件输入、模板、任务记录和导出结果都保存在本机；外部服务只用于 OCR 和结构化抽取：

- PaddleOCR：识别 PDF 版面并输出 JSONL/Markdown。
- DeepSeek：把 OCR 文本抽取成统一的报价单 JSON。

## 核心能力

- 本地 PDF 报价单解析。
- PaddleOCR 异步 OCR：提交任务、轮询状态、下载 JSONL。
- DeepSeek agent 结构化抽取：表头、明细、金额合计。
- Excel 模板体检：扫描工作表、单元格、公式、合并区域和字段候选。
- 固定模板导出：只写入映射允许的单元格，不修改样式、公式、图片、宏和工作表结构。
- 人工审核状态：保存、批准、驳回、阻断异常。
- 异常告警：本地告警记录和可选 Webhook 推送。
- 命令行流程和可选本地 HTTP 工作台。

## 整体架构

```text
本地 PDF
  -> PaddleOCR API
  -> data/jobs/<job_id>/ocr/ocr.jsonl
  -> data/jobs/<job_id>/ocr/ocr.md
  -> DeepSeek agent
  -> 标准 quote JSON
  -> validation.py 校验
  -> 人工审核/批准
  -> template_mapping.json 映射
  -> template_export.py 写入固定 Excel 模板副本
  -> output/quote-<job_id>.xlsx 或 .xlsm
```

模板导出不是让 AI 直接填写 Excel。AI 只负责把 OCR 文本整理为标准 JSON；最终 Excel 写入完全由 `templates/template_mapping.json` 决定。

## 目录结构

```text
quote_assistant/
  app.py                         # 可选 HTTP 工作台入口
  config.json                    # 系统配置
  environment.yml                # Conda 环境文件
  requirements.txt               # pip 依赖文件
  README.md                      # 项目说明
  quote_assistant/               # Python 核心包
  scripts/                       # 辅助脚本
  tests/                         # 单元测试
  templates/                     # Excel 模板和映射
  samples/                       # 测试用 PDF 样例
  data/jobs/                     # 本地任务记录，运行时生成
  output/                        # Excel 导出结果，运行时生成
  docs/                          # 额外说明文档
```

## 主要文件说明

### 根目录

| 文件 | 作用 |
| --- | --- |
| `app.py` | 本地 HTTP 工作台入口，提供上传、审核、导出等 API。 |
| `config.json` | 置信度阈值、金额容差、模板映射文件路径等配置。 |
| `environment.yml` | 推荐的 Anaconda 环境定义。 |
| `requirements.txt` | pip 安装依赖。 |
| `.gitignore` | 排除运行产物、缓存、日志、IDE 配置和密钥文件。 |

### `quote_assistant/` 核心包

| 文件 | 作用 |
| --- | --- |
| `cli.py` | 命令行入口，支持本地 OCR/抽取/导出、模板导入、模板激活、验收报告。 |
| `PaddleOCR.py` | PaddleOCR 客户端，实现提交文件、轮询任务、下载 JSONL、提取 Markdown。 |
| `ocr.py` | 兼容入口，重新导出 `PaddleOCR.py` 中的 OCR 类和函数。 |
| `deepseek_agent.py` | DeepSeek agent，把 OCR Markdown 抽取成统一报价单 JSON。 |
| `local_workflow.py` | 本地完整流程编排：PDF -> OCR -> DeepSeek -> 校验 -> 任务记录 -> 可选导出。 |
| `parser.py` | 文本型 PDF 的本地解析器，使用 `pypdf` 和规则提取报价字段。 |
| `validation.py` | 校验表头、明细、金额、合计、低置信度和人工修正。 |
| `service.py` | 任务服务层，负责创建任务、审核、模板导入、模板激活、导出和告警。 |
| `storage.py` | 本地 JSON 任务存储。 |
| `alerts.py` | 本地告警记录和可选 Webhook 推送。 |
| `template_inspect.py` | Excel 模板识别/体检，生成模板报告和映射草稿。 |
| `template_export.py` | 按固定映射写入 Excel OpenXML，保护公式、样式、合并区域和宏。 |
| `acceptance.py` | 验收报告生成，验证模板导出和异常阻断流程。 |
| `models.py` | 通用字段、问题和时间工具。 |

### `scripts/`

| 文件 | 作用 |
| --- | --- |
| `inspect_template.py` | 手动体检 Excel 模板，生成 `template_report.json` 和 `template_mapping.draft.json`。 |
| `run_acceptance.py` | 运行验收报告。 |
| `generate_samples.py` | 生成测试 PDF 样例。 |
| `create_standard_template.py` | 生成标准报价模板，需要 `openpyxl`。 |
| `verify_workbook.mjs` | 使用 Node 工具检查工作簿。 |

### `templates/`

| 文件 | 作用 |
| --- | --- |
| `standard-quotation-template.xlsm` | 当前默认标准 Excel 报价模板。 |
| `template_mapping.json` | 当前已激活的正式字段映射。导出时只使用这个文件。 |
| `template_mapping.draft.json` | 模板体检后生成的映射草稿，需要人工确认后激活。 |
| `template_report.json` | 模板体检报告。 |
| `Telop - Quotation for (description) dd.mm.yyyy.xlsx` | Telop 相关模板样例。 |

### `tests/`

| 文件 | 作用 |
| --- | --- |
| `test_validation.py` | 报价字段校验和人工修正测试。 |
| `test_template_export.py` | Excel 固定模板导出和结构保护测试。 |
| `test_template_inspect_image.py` | 模板识别中“货物图片”字段识别测试。 |
| `test_service_alerts.py` | 服务层、告警、模板激活、源文件完整性测试。 |
| `test_app_endpoints.py` | HTTP API 测试。 |
| `test_local_workflow.py` | 本地 PaddleOCR/DeepSeek 流程的 mock 测试。 |

## 安装环境

推荐使用 Anaconda：

```cmd
cd "C:\Users\DC·Laptop-air\Documents\Automatic Order Assistant\quote_assistant"
conda env create -f environment.yml
conda activate quote-assistant
```

也可以用 pip：

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 配置 API

真实 OCR + DeepSeek 流程需要配置环境变量。

CMD：

```cmd
set PADDLEOCR_TOKEN=你的PaddleOCR_TOKEN
set DEEPSEEK_API_KEY=你的DeepSeek_API_KEY
set PADDLEOCR_MODEL=PaddleOCR-VL
set DEEPSEEK_MODEL=deepseek-v4-flash
```

PowerShell：

```powershell
$env:PADDLEOCR_TOKEN = "你的PaddleOCR_TOKEN"
$env:DEEPSEEK_API_KEY = "你的DeepSeek_API_KEY"
$env:PADDLEOCR_MODEL = "PaddleOCR-VL"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
```

可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PADDLEOCR_JOB_URL` | `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs` | PaddleOCR 任务地址。 |
| `PADDLEOCR_TIMEOUT_SECONDS` | `600` | OCR 任务最大等待时间。 |
| `PADDLEOCR_POLL_INTERVAL_SECONDS` | `5` | OCR 轮询间隔。 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 地址。 |
| `DEEPSEEK_TIMEOUT_SECONDS` | `120` | DeepSeek 请求超时。 |
| `ALERT_WEBHOOK_URL` | 空 | 可选告警 Webhook。 |
| `ALERT_WEBHOOK_SECRET` | 空 | 可选 Webhook HMAC 签名密钥。 |

不要把 API key 写入代码或提交到 GitHub。

## 快速使用

### 1. 运行本地 PDF 流程

```cmd
python -m quote_assistant.cli run-local --pdf .\samples\quote-normal.pdf --reviewer Test
```

成功后会生成：

```text
data/jobs/<job_id>/source.pdf
data/jobs/<job_id>/ocr/ocr.jsonl
data/jobs/<job_id>/ocr/ocr.md
data/jobs/<job_id>/job.json
```

### 2. 批准并导出 Excel

```cmd
python -m quote_assistant.cli run-local --pdf .\samples\quote-normal.pdf --reviewer Test --approve --export
```

导出文件位于：

```text
output/quote-<job_id>.xlsm
```

### 3. 生成验收报告

```cmd
python -m quote_assistant.cli acceptance
```

### 4. 启动可选 HTTP 工作台

```cmd
python app.py --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765
```

## 模板识别和激活

### 1. 导入模板

```cmd
python -m quote_assistant.cli import-template --template "D:\path\to\your-template.xlsx"
```

系统会复制模板到 `templates/`，并生成：

```text
templates/template_report.json
templates/template_mapping.draft.json
```

### 2. 检查映射草稿

打开 `templates/template_mapping.draft.json`，确认字段与目标单元格或列是否正确。

常见字段路径：

```text
quote.headers.quote_no.value
quote.headers.supplier.value
quote.items[].product_name.value
quote.items[].product_image.value
quote.items[].quantity.value
quote.items[].unit_price.value
quote.items[].amount.value
quote.totals.grand_total.value
```

明细区映射位于：

```json
"items": {
  "start_row": 7,
  "max_rows": 20,
  "columns": {
    "product_name.value": "C",
    "product_image.value": "B"
  }
}
```

注意：当前导出层主要写入单元格值；图片字段已经能被模板识别为映射字段，但真正把图片二进制插入 Excel 还需要扩展 `template_export.py` 的图片写入逻辑。

### 3. 激活模板

```cmd
python -m quote_assistant.cli activate-template --mapping-json .\templates\template_mapping.draft.json --reviewer Test --confirm-format-immutable
```

激活成功后，正式导出会使用：

```text
templates/template_mapping.json
```

## Excel 导出机制

导出逻辑位于 `quote_assistant/template_export.py`。

系统会：

1. 打开 `.xlsx/.xlsm` 文件的 OpenXML zip 包。
2. 根据 `template_mapping.json` 定位工作表和单元格。
3. 检查目标单元格是否存在。
4. 阻止覆盖公式单元格。
5. 阻止写入合并区域的非左上角单元格。
6. 写入允许单元格的值。
7. 重新打包生成副本。
8. 计算结构指纹，确认除允许单元格值外没有改变模板结构。

## 运行测试

```cmd
python -m unittest discover -s tests -v
```

当前验证过的结果：

```text
Ran 36 tests
OK
```

## 数据和安全

以下内容不会提交到 GitHub：

- API key 和 `.env` 文件。
- `data/jobs/` 下的任务记录、OCR 结果和源 PDF。
- `output/` 下的导出 Excel。
- `node_modules/`、`__pycache__/`、IDE 配置和日志。

如果需要共享模板，请先确认模板中不包含供应商隐私、价格敏感信息或客户信息。

## 常见问题

### 为什么导出的不是我刚提供的模板？

导出永远使用当前激活的 `templates/template_mapping.json`。如果其中的 `template_file` 是 `standard-quotation-template.xlsm`，导出就会使用该模板。要切换模板，需要先导入并激活新的映射。

### 为什么运行时报 `PADDLEOCR_TOKEN is required`？

说明当前终端没有设置 `PADDLEOCR_TOKEN`。在同一个 CMD 或 PowerShell 窗口设置环境变量后再运行。

### OCR JSON 是怎么进入 Excel 的？

OCR JSONL 会先转为 Markdown，再由 DeepSeek 抽取为标准 quote JSON。Excel 导出只读取标准 quote JSON，并按 `template_mapping.json` 映射写入模板。

### 能否直接把图片填进 Excel？

模板识别已支持 `product_image.value` 字段。若要真正插入图片文件，需要继续扩展导出层，让它把图片文件复制进 OpenXML 包并创建 drawing relationship。
