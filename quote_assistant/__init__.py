"""PDF quote review assistant."""
"""自动报价单整理核心包。

包内模块按职责拆分：
- `parser`：文本型 PDF 规则解析；
- `PaddleOCR`/`ocr`：PaddleOCR 异步 OCR；
- `deepseek_agent`：OCR 文本到标准 quote JSON 的抽取；
- `validation`：报价字段和金额一致性校验；
- `service`：任务、审核、模板和导出的应用服务；
- `template_inspect`/`template_export`：Excel 模板体检和不可变导出。
"""

