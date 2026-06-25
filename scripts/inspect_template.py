"""命令行模板体检脚本。

用途：对本地 Excel 报价模板做只读扫描，输出 `template_report.json` 和 `template_mapping.draft.json`。
草稿不会自动启用，必须人工确认每个单元格后再走 activate-template。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quote_assistant.template_inspect import write_template_report


def main() -> None:
    """解析模板路径参数并生成体检报告。"""
    parser = argparse.ArgumentParser(description="只读体检原始Excel报价模板并生成待审核映射草稿")
    parser.add_argument("template", type=Path, help="原始.xlsx或.xlsm模板")
    parser.add_argument("--sheet", help="作为报价单的工作表名称；默认使用第一个工作表")
    parser.add_argument("--report", type=Path, default=Path("templates/template_report.json"))
    parser.add_argument("--draft", type=Path, default=Path("templates/template_mapping.draft.json"))
    args = parser.parse_args()
    write_template_report(args.template, args.report, args.draft, args.sheet)
    print(f"模板报告：{args.report.resolve()}")
    print(f"映射草稿：{args.draft.resolve()}")
    print("草稿不会自动启用；必须人工确认所有单元格、明细起始行和最大预留行数。")


if __name__ == "__main__":
    main()
