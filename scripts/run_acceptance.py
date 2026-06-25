"""命令行验收报告脚本。

用途：生成一份 JSON 报告，检查模板导出、异常阻断、真实模板/真实 PDF 准备情况。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quote_assistant.acceptance import generate_acceptance_report


def main() -> None:
    """解析可选输入并输出验收报告 JSON。"""
    parser = argparse.ArgumentParser(description="运行报价单MVP验收并输出JSON报告")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default="", help="可选：将JSON报告写入指定路径")
    parser.add_argument("--template", default="", help="可选：真实Excel模板路径；未提供时使用脚本生成的样例模板")
    parser.add_argument("--mapping-json", default="", help="可选：真实模板对应的映射JSON；提供后会尝试执行正式导出验收")
    parser.add_argument("--normal-pdf", default="", help="可选：正常报价PDF路径；默认使用samples/quote-normal.pdf")
    parser.add_argument("--anomaly-pdf", default="", help="可选：异常报价PDF路径；默认使用samples/quote-anomaly.pdf")
    args = parser.parse_args()

    report = generate_acceptance_report(
        Path(args.project_root).resolve(),
        template_path=Path(args.template).resolve() if args.template else None,
        mapping_path=Path(args.mapping_json).resolve() if args.mapping_json else None,
        normal_pdf=Path(args.normal_pdf).resolve() if args.normal_pdf else None,
        anomaly_pdf=Path(args.anomaly_pdf).resolve() if args.anomaly_pdf else None,
    )

    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_file = Path(args.output).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
