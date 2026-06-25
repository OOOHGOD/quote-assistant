"""OCR 兼容导入入口。

历史代码中真实实现文件名是 `PaddleOCR.py`，测试和外部调用更自然地使用 `quote_assistant.ocr`。
这个模块只做重新导出，避免改动现有导入路径。
"""

from __future__ import annotations

from .PaddleOCR import *  # noqa: F403
