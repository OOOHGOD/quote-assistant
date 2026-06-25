"""报价单结构校验与人工修正工具。

校验层不关心数据来自 pypdf、PaddleOCR 还是 DeepSeek，只检查统一 quote JSON：
必填字段、置信度、明细金额、合计金额、重复编码和人工确认状态。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import field, issue


REQUIRED_HEADERS = ["quote_no", "supplier", "currency"]
REQUIRED_ITEM_FIELDS = ["product_name", "quantity", "unit_price", "amount"]
ITEM_FIELDS = [
    "product_code", "product_name", "specification", "material", "color", "unit",
    "quantity", "unit_price", "amount", "location", "remarks",
]
NUMERIC_ITEM_FIELDS = {"quantity", "unit_price", "amount"}


def _value(candidate: dict[str, Any] | None) -> Any:
    """读取字段包装对象中的实际值。"""
    return (candidate or {}).get("value")


def _confidence(candidate: dict[str, Any] | None) -> float:
    """读取字段包装对象中的置信度，缺失时按 0 处理。"""
    return float((candidate or {}).get("confidence") or 0.0)


def validate_quote(quote: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """校验报价单并给出流程决策。

    返回的 `decision` 只有两种：存在 error/critical 时进入 `needs_review`，
    否则进入 `ready_for_review`，仍然需要人工批准后才能导出 Excel。
    """
    issues = [deepcopy(entry) for entry in quote.get("extraction_issues", []) if not entry.get("resolved_by_human")]
    threshold = float(config.get("confidence_threshold", 0.8))
    critical_threshold = float(config.get("critical_confidence_threshold", 0.9))
    tolerance = float(config.get("amount_tolerance", 0.02))
    critical_fields = set(config.get("critical_fields", []))

    for name in REQUIRED_HEADERS:
        # 表头必填字段缺失会阻断批准，避免没有供应商/币种/报价号的报价单流入导出。
        candidate = quote.get("headers", {}).get(name)
        if _value(candidate) in (None, ""):
            issues.append(issue("MISSING_REQUIRED_FIELD", "error", f"缺少必填字段：{name}", f"headers.{name}"))

    if not quote.get("items"):
        issues.append(issue("NO_LINE_ITEMS", "critical", "未识别到任何报价明细行。", "items"))

    for group_name in ("headers", "totals"):
        for name, candidate in quote.get(group_name, {}).items():
            if _value(candidate) in (None, ""):
                continue
            required = critical_threshold if name in critical_fields else threshold
            if _confidence(candidate) < required:
                issues.append(issue("LOW_CONFIDENCE", "error" if name in critical_fields else "warning", f"字段 {name} 识别置信度偏低。", f"{group_name}.{name}", _confidence(candidate)))

    calculated_subtotal = 0.0
    seen_codes: set[str] = set()
    for index, item in enumerate(quote.get("items", [])):
        # 明细行同时检查必填字段、置信度和金额公式，尽量把 OCR/agent 错误暴露给人工审核。
        for name in REQUIRED_ITEM_FIELDS:
            candidate = item.get(name)
            if _value(candidate) in (None, ""):
                issues.append(issue("MISSING_REQUIRED_FIELD", "error", f"第 {index + 1} 行缺少 {name}。", f"items.{index}.{name}"))

        for name, candidate in item.items():
            if name == "line_no" or not isinstance(candidate, dict) or _value(candidate) in (None, ""):
                continue
            required = critical_threshold if name in critical_fields else threshold
            if _confidence(candidate) < required:
                issues.append(issue("LOW_CONFIDENCE", "error" if name in critical_fields else "warning", f"第 {index + 1} 行字段 {name} 识别置信度偏低。", f"items.{index}.{name}", _confidence(candidate)))

        quantity = _value(item.get("quantity"))
        unit_price = _value(item.get("unit_price"))
        amount = _value(item.get("amount"))
        if isinstance(quantity, (int, float)) and quantity <= 0:
            issues.append(issue("INVALID_QUANTITY", "error", f"第 {index + 1} 行数量必须大于0。", f"items.{index}.quantity", quantity))
        if isinstance(unit_price, (int, float)) and unit_price < 0:
            issues.append(issue("INVALID_UNIT_PRICE", "error", f"第 {index + 1} 行单价不能小于0。", f"items.{index}.unit_price", unit_price))
        if all(isinstance(value, (int, float)) for value in (quantity, unit_price, amount)):
            expected = round(quantity * unit_price, 2)
            calculated_subtotal += expected
            if abs(expected - amount) > max(0.01, abs(expected) * tolerance):
                issues.append(issue("AMOUNT_MISMATCH", "error", f"第 {index + 1} 行金额与数量×单价不一致，应为 {expected:.2f}。", f"items.{index}.amount", amount))

        code = str(_value(item.get("product_code")) or "").strip().lower()
        if code and code in seen_codes:
            issues.append(issue("DUPLICATE_PRODUCT_CODE", "warning", f"产品编码重复：{code}", f"items.{index}.product_code", code))
        seen_codes.add(code)

    subtotal = _value(quote.get("totals", {}).get("subtotal"))
    if isinstance(subtotal, (int, float)) and abs(subtotal - calculated_subtotal) > max(0.01, abs(calculated_subtotal) * tolerance):
        issues.append(issue("SUBTOTAL_MISMATCH", "error", f"报价小计与明细合计不一致，明细合计为 {calculated_subtotal:.2f}。", "totals.subtotal", subtotal))

    tax = _value(quote.get("totals", {}).get("tax"))
    grand_total = _value(quote.get("totals", {}).get("grand_total"))
    if all(isinstance(value, (int, float)) for value in (subtotal, tax, grand_total)):
        expected_total = round(subtotal + tax, 2)
        if abs(grand_total - expected_total) > max(0.01, abs(expected_total) * tolerance):
            issues.append(issue("GRAND_TOTAL_MISMATCH", "error", f"总计与小计+税额不一致，应为 {expected_total:.2f}。", "totals.grand_total", grand_total))

    blocking = [entry for entry in issues if entry["severity"] in {"error", "critical"}]
    return {
        "issues": issues,
        "blocking_issue_count": len(blocking),
        "warning_count": sum(1 for entry in issues if entry["severity"] == "warning"),
        "calculated_subtotal": round(calculated_subtotal, 2),
        "decision": "needs_review" if blocking else "ready_for_review",
    }


def apply_corrections(quote: dict[str, Any], corrections: dict[str, Any]) -> dict[str, Any]:
    """应用前端提交的字段级人工修正。

    被人工修正的字段置信度直接设为 1.0，并打上 `corrected_by_human` 标记。
    """
    corrected = deepcopy(quote)
    for path, value in corrections.items():
        parts = path.split(".")
        target: Any = corrected
        try:
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            leaf = parts[-1]
            candidate = target[int(leaf)] if isinstance(target, list) else target.get(leaf)
            if isinstance(candidate, dict) and "value" in candidate:
                candidate["value"] = value
                candidate["confidence"] = 1.0
                candidate["corrected_by_human"] = True
            elif isinstance(target, list):
                target[int(leaf)] = value
            else:
                target[leaf] = value
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return corrected


def apply_item_rows(quote: dict[str, Any], rows: list[dict[str, Any]], max_items: int = 500) -> dict[str, Any]:
    """应用人工编辑后的明细表。

    支持删除、重排和新增明细；新增字段会保留人工录入来源，便于后续审计。
    """
    if len(rows) > max_items:
        raise ValueError(f"人工明细最多允许 {max_items} 行。")
    corrected = deepcopy(quote)
    original_items = corrected.get("items", [])
    rebuilt: list[dict[str, Any]] = []
    used_original_indices: set[int] = set()

    for new_index, row in enumerate(rows):
        original_index = row.get("original_index")
        if isinstance(original_index, int) and 0 <= original_index < len(original_items) and original_index not in used_original_indices:
            item = deepcopy(original_items[original_index])
            used_original_indices.add(original_index)
        else:
            item = {name: field() for name in ITEM_FIELDS}

        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        for name in ITEM_FIELDS:
            value = values.get(name)
            if name in NUMERIC_ITEM_FIELDS:
                if value in (None, ""):
                    value = None
                elif not isinstance(value, (int, float)) or isinstance(value, bool):
                    try:
                        value = float(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"第 {new_index + 1} 行 {name} 必须是数字。") from exc
            elif value is not None:
                value = str(value).strip() or None

            candidate = item.get(name)
            if not isinstance(candidate, dict) or "value" not in candidate:
                candidate = field()
                item[name] = candidate
            if candidate.get("value") != value:
                candidate["value"] = value
                candidate["confidence"] = 1.0
                candidate["source"] = {"type": "manual_review", "text": "人工录入或修改"}
                candidate["corrected_by_human"] = True

        item["line_no"] = new_index + 1
        rebuilt.append(item)

    corrected["items"] = rebuilt
    corrected["manual_item_structure_changed"] = [row.get("original_index") for row in rows] != list(range(len(original_items)))
    return corrected


def resolve_extraction_issues(quote: dict[str, Any], reviewer: str, reviewed_at: str, source_sha256: str = "") -> dict[str, Any]:
    """把“需要人工核对源文件”的抽取问题标记为已人工确认。"""
    corrected = deepcopy(quote)
    for entry in corrected.get("extraction_issues", []):
        entry["resolved_by_human"] = True
        entry["resolution"] = {
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "method": "source_document_verified",
            "source_sha256": source_sha256,
        }
    corrected["human_source_verification"] = {
        "verified": True,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "source_sha256": source_sha256,
    }
    return corrected
