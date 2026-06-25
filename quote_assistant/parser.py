from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .models import field, issue


HEADER_PATTERNS = {
    "quote_no": [r"(?:quote|quotation|报价单?|报价编号)\s*(?:no\.?|number|编号|#)?\s*[:：]?\s*([^\n|]+)"],
    "supplier": [r"(?:supplier|vendor|供应商|供方)\s*[:：]\s*([^\n|]+)"],
    "customer": [r"(?:customer|client|客户|采购方)\s*[:：]\s*([^\n|]+)"],
    "project": [r"(?:project|项目)\s*[:：]\s*([^\n|]+)"],
    "quote_date": [r"(?:date|报价日期|日期)\s*[:：]\s*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?)"],
    "currency": [r"(?:currency|币种)\s*[:：]\s*([A-Za-z]{3}|人民币|美元|欧元|英镑)"],
}

TOTAL_PATTERNS = {
    "subtotal": r"^(?:subtotal|小计|未税合计)\s*[:：]?\s*(?:[A-Z]{3}|[¥￥$€])?\s*([0-9,]+(?:\.\d+)?)\s*$",
    "tax": r"^(?:tax|vat|税额)\s*[:：]?\s*(?:[A-Z]{3}|[¥￥$€])?\s*([0-9,]+(?:\.\d+)?)\s*$",
    "grand_total": r"^(?:grand\s*total|total|价税合计|总计|合计)\s*[:：]?\s*(?:[A-Z]{3}|[¥￥$€])?\s*([0-9,]+(?:\.\d+)?)\s*$",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t:：")


def _number(value: str) -> float | None:
    try:
        return float(value.replace(",", "").replace("¥", "").replace("￥", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _source(page: int, line: int, text: str) -> dict[str, Any]:
    return {"page": page, "line": line, "text": text[:500], "bbox": None}


def _find_header(pages: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for page in pages:
        for line_no, line in enumerate(page["lines"], start=1):
            for pattern in HEADER_PATTERNS[key]:
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if match:
                    value = _clean(match.group(1))
                    confidence = 0.97 if len(value) >= 2 else 0.72
                    return field(value, confidence, _source(page["page"], line_no, line))
    return field()


def _parse_pipe_item(page_no: int, line_no: int, line: str) -> dict[str, Any] | None:
    parts = [_clean(part) for part in line.split("|")]
    if len(parts) < 10 or not re.fullmatch(r"\d+", parts[0]):
        return None

    quantity = _number(parts[7])
    unit_price = _number(parts[8])
    amount = _number(parts[9])
    src = _source(page_no, line_no, line)
    values = (quantity, unit_price, amount)
    numeric_confidence = 0.98 if all(value is not None for value in values) else 0.35
    return {
        "line_no": int(parts[0]),
        "product_code": field(parts[1] or None, 0.95 if parts[1] else 0.0, src),
        "product_name": field(parts[2] or None, 0.96 if parts[2] else 0.0, src),
        "specification": field(parts[3] or None, 0.9 if parts[3] else 0.0, src),
        "material": field(parts[4] or None, 0.9 if parts[4] else 0.0, src),
        "color": field(parts[5] or None, 0.9 if parts[5] else 0.0, src),
        "unit": field(parts[6] or None, 0.94 if parts[6] else 0.0, src),
        "quantity": field(quantity, numeric_confidence, src),
        "unit_price": field(unit_price, numeric_confidence, src),
        "amount": field(amount, numeric_confidence, src),
        "location": field(parts[10] if len(parts) > 10 and parts[10] else None, 0.88 if len(parts) > 10 and parts[10] else 0.0, src),
        "remarks": field(parts[11] if len(parts) > 11 and parts[11] else None, 0.86 if len(parts) > 11 and parts[11] else 0.0, src),
    }


def _parse_space_item(page_no: int, line_no: int, line: str) -> dict[str, Any] | None:
    parts = [part.strip() for part in re.split(r"\s{2,}", line.strip()) if part.strip()]
    if len(parts) < 6 or not re.fullmatch(r"\d+", parts[0]):
        return None
    numeric = [_number(part) for part in parts[-3:]]
    if numeric[0] is None or numeric[1] is None or numeric[2] is None:
        return None
    src = _source(page_no, line_no, line)
    return {
        "line_no": int(parts[0]),
        "product_code": field(parts[1] if len(parts) > 6 else None, 0.74 if len(parts) > 6 else 0.0, src),
        "product_name": field(parts[2] if len(parts) > 6 else parts[1], 0.78, src),
        "specification": field(" ".join(parts[3:-3]) if len(parts) > 7 else None, 0.68 if len(parts) > 7 else 0.0, src),
        "material": field(),
        "color": field(),
        "unit": field("pcs", 0.55, src),
        "quantity": field(numeric[0], 0.76, src),
        "unit_price": field(numeric[1], 0.76, src),
        "amount": field(numeric[2], 0.76, src),
        "location": field(),
        "remarks": field(),
    }


def parse_pdf(path: Path) -> dict[str, Any]:
    reader = PdfReader(str(path))
    pages: list[dict[str, Any]] = []
    extraction_issues: list[dict[str, Any]] = []
    total_chars = 0

    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lines = [_clean(line) for line in text.splitlines() if _clean(line)]
        total_chars += len(text.strip())
        pages.append({"page": page_no, "text": text, "lines": lines})

    if total_chars < 80:
        extraction_issues.append(issue(
            "SCANNED_OR_EMPTY_PDF",
            "critical",
            "PDF未提取到足够文本，可能是扫描件或版面解析失败，需要OCR或人工录入。",
            "document",
            total_chars,
        ))

    headers = {key: _find_header(pages, key) for key in HEADER_PATTERNS}
    if not headers["currency"]["value"]:
        combined = "\n".join(page["text"] for page in pages)
        symbol_map = [("CNY", r"[¥￥]|RMB|CNY"), ("USD", r"\$|USD"), ("EUR", r"€|EUR")]
        for currency, pattern in symbol_map:
            if re.search(pattern, combined, flags=re.IGNORECASE):
                headers["currency"] = field(currency, 0.72, {"page": 1, "line": None, "text": "currency symbol"})
                break

    items: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for page in pages:
        for line_no, line in enumerate(page["lines"], start=1):
            item = _parse_pipe_item(page["page"], line_no, line) or _parse_space_item(page["page"], line_no, line)
            if item:
                items.append(item)
                evidence.append(_source(page["page"], line_no, line))

    totals: dict[str, Any] = {}
    for key, pattern in TOTAL_PATTERNS.items():
        found = field()
        for page in pages:
            for line_no, line in enumerate(page["lines"], start=1):
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if match:
                    found = field(_number(match.group(1)), 0.94, _source(page["page"], line_no, line))
                    break
            if found["value"] is not None:
                break
        totals[key] = found

    return {
        "document": {
            "page_count": len(pages),
            "text_char_count": total_chars,
            "parser": "pypdf+quote-heuristics-v1",
        },
        "headers": headers,
        "items": items,
        "totals": totals,
        "evidence": evidence,
        "extraction_issues": extraction_issues,
        "raw_pages": [{"page": page["page"], "text": page["text"]} for page in pages],
    }
