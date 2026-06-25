"""Excel 模板识别与映射草稿生成。

这个模块不会修改模板文件，只读取 xlsx/xlsm 的 OpenXML 内容，提取工作表、单元格、公式、
合并区域、行列尺寸等信息，并根据常见字段别名生成 `template_mapping.draft.json`。
草稿必须经过人工确认和 `activate-template` 校验后才会成为正式导出映射。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .template_export import DOC_REL_NS, MAIN_NS, TemplateExportError, _q, _resolve_sheet_path


HEADER_ALIASES = {
    "quote.headers.quote_no.value": {"报价编号", "报价单号", "quote no", "quotation no"},
    "quote.headers.supplier.value": {"供应商", "供方", "supplier", "vendor"},
    "quote.headers.customer.value": {"客户", "采购方", "customer", "client"},
    "quote.headers.project.value": {"项目", "项目名称", "project"},
    "quote.headers.quote_date.value": {"报价日期", "日期", "quote date", "date"},
    "quote.headers.currency.value": {"币种", "currency"},
}

ITEM_ALIASES = {
    "product_image.value": {"货物图片", "产品图片", "商品图片", "图片", "image", "product image", "item image", "photo", "picture"},
    "line_no": {"序号", "项次", "no"},
    "product_code.value": {"产品编码", "物料编码", "货号", "code", "item code"},
    "product_name.value": {"产品名称", "品名", "description", "product"},
    "specification.value": {"规格尺寸", "规格", "尺寸", "specification", "size"},
    "material.value": {"材质", "material"},
    "color.value": {"颜色", "色号", "color"},
    "unit.value": {"单位", "unit"},
    "quantity.value": {"数量", "qty", "quantity"},
    "unit_price.value": {"单价", "unit price", "price"},
    "amount.value": {"金额", "合价", "amount"},
    "location.value": {"房间/区域", "区域", "位置", "location", "room"},
    "remarks.value": {"备注", "说明", "remarks", "notes"},
}

TOTAL_ALIASES = {
    "quote.totals.subtotal.value": {"小计", "未税合计", "subtotal"},
    "quote.totals.tax.value": {"税额", "tax", "vat"},
    "quote.totals.grand_total.value": {"总计", "价税合计", "合计", "grand total", "total"},
}


def _normalize_label(value: Any) -> str:
    """统一标签文本，降低大小写、空白和中英文冒号差异带来的影响。"""
    return re.sub(r"\s+", " ", str(value or "").strip().lower().rstrip(":："))


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def _column_letters(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _split_address(address: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", address)
    if not match:
        raise TemplateExportError(f"模板包含无效单元格地址：{address}")
    return match.group(1), int(match.group(2))


def _right_address(address: str) -> str:
    column, row = _split_address(address)
    return f"{_column_letters(_column_number(column) + 1)}{row}"


def _shared_strings(files: dict[str, bytes]) -> list[str]:
    """读取 Excel sharedStrings 表；xlsx/xlsm 常用索引引用这里的文本。"""
    data = files.get("xl/sharedStrings.xml")
    if not data:
        return []
    root = ET.fromstring(data)
    values = []
    for item in root.findall(_q(MAIN_NS, "si")):
        values.append("".join(text.text or "" for text in item.findall(f".//{_q(MAIN_NS, 't')}")))
    return values


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    """把 OpenXML 单元格节点转换成 Python 可读值。"""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(f".//{_q(MAIN_NS, 't')}"))
    value_element = cell.find(_q(MAIN_NS, "v"))
    if value_element is None or value_element.text is None:
        return None
    raw = value_element.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    if cell_type in {"str", "e"}:
        return raw
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def _find_label_candidates(cells: list[dict[str, Any]], aliases: dict[str, set[str]]) -> dict[str, list[dict[str, Any]]]:
    """根据别名表在模板单元格中寻找字段候选位置。"""
    result: dict[str, list[dict[str, Any]]] = {}
    normalized_aliases = {path: {_normalize_label(alias) for alias in values} for path, values in aliases.items()}
    existing_addresses = {cell["address"] for cell in cells}
    for cell in cells:
        normalized = _normalize_label(cell.get("value"))
        if not normalized:
            continue
        for source_path, values in normalized_aliases.items():
            if normalized in values:
                right = _right_address(cell["address"])
                result.setdefault(source_path, []).append({
                    "label_cell": cell["address"],
                    "label": cell["value"],
                    "suggested_value_cell": right if right in existing_addresses else None,
                    "style_id": cell.get("style_id"),
                })
    return result


def inspect_xlsx_template(template_path: Path, *, cell_limit: int = 2000) -> dict[str, Any]:
    """只读扫描 Excel 模板并生成体检报告。"""
    if template_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise TemplateExportError("模板体检只支持.xlsx或.xlsm文件。")
    try:
        with zipfile.ZipFile(template_path, "r") as archive:
            files = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise TemplateExportError(f"无法读取Excel模板：{exc}") from exc

    workbook = ET.fromstring(files["xl/workbook.xml"])
    shared = _shared_strings(files)
    sheets = []
    for sheet in workbook.findall(f".//{_q(MAIN_NS, 'sheet')}"):
        sheet_name = sheet.get("name", "")
        sheet_path = _resolve_sheet_path(files, sheet_name)
        root = ET.fromstring(files[sheet_path])
        cells = []
        formula_count = 0
        for cell in root.findall(f".//{_q(MAIN_NS, 'c')}"):
            address = cell.get("r")
            if not address:
                continue
            formula = cell.find(_q(MAIN_NS, "f"))
            if formula is not None:
                formula_count += 1
            if len(cells) < cell_limit:
                cells.append({
                    "address": address,
                    "value": _cell_value(cell, shared),
                    "formula": formula.text if formula is not None else None,
                    "style_id": cell.get("s"),
                    "type": cell.get("t"),
                })

        dimension = root.find(_q(MAIN_NS, "dimension"))
        merges = [merge.get("ref") for merge in root.findall(f".//{_q(MAIN_NS, 'mergeCell')}")]
        rows = [{"row": row.get("r"), "height": row.get("ht"), "style": row.get("s")} for row in root.findall(f".//{_q(MAIN_NS, 'row')}") if row.get("ht") or row.get("s")]
        columns = [{key: column.get(key) for key in ("min", "max", "width", "style", "hidden")} for column in root.findall(f".//{_q(MAIN_NS, 'col')}")]
        page_setup = root.find(_q(MAIN_NS, "pageSetup"))
        page_margins = root.find(_q(MAIN_NS, "pageMargins"))
        auto_filter = root.find(_q(MAIN_NS, "autoFilter"))
        report_sheet = {
            "name": sheet_name,
            "path": sheet_path,
            "dimension": dimension.get("ref") if dimension is not None else None,
            "cell_node_count": len(root.findall(f".//{_q(MAIN_NS, 'c')}")),
            "reported_cell_count": len(cells),
            "cell_limit_reached": len(cells) >= cell_limit,
            "formula_count": formula_count,
            "merged_ranges": merges,
            "row_dimensions": rows,
            "column_dimensions": columns,
            "page_setup": dict(page_setup.attrib) if page_setup is not None else {},
            "page_margins": dict(page_margins.attrib) if page_margins is not None else {},
            "auto_filter": auto_filter.get("ref") if auto_filter is not None else None,
            "cells": cells,
        }
        report_sheet["field_candidates"] = {
            "headers": _find_label_candidates(cells, HEADER_ALIASES),
            "items": _find_label_candidates(cells, ITEM_ALIASES),
            "totals": _find_label_candidates(cells, TOTAL_ALIASES),
        }
        sheets.append(report_sheet)

    return {
        "template_path": str(template_path.resolve()),
        "template_file": template_path.name,
        "template_extension": template_path.suffix.lower(),
        "template_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
        "package_file_count": len(files),
        "sheet_count": len(sheets),
        "sheets": sheets,
    }


def mapping_draft_from_report(report: dict[str, Any], sheet_name: str | None = None) -> dict[str, Any]:
    """根据模板体检报告生成待审核映射草稿。"""
    target_sheet = None
    for sheet in report["sheets"]:
        if sheet_name is None or sheet["name"] == sheet_name:
            target_sheet = sheet
            break
    if target_sheet is None:
        raise TemplateExportError(f"模板报告中不存在工作表：{sheet_name}")

    def unique_suggestion(group: str, source_path: str) -> str:
        """只有唯一可用候选时才自动给出建议，避免误选。"""
        candidates = target_sheet["field_candidates"][group].get(source_path, [])
        usable = [candidate["suggested_value_cell"] for candidate in candidates if candidate.get("suggested_value_cell")]
        return usable[0] if len(set(usable)) == 1 else ""

    header_cells = {path: unique_suggestion("headers", path) for path in HEADER_ALIASES}
    total_cells = {path: unique_suggestion("totals", path) for path in TOTAL_ALIASES}
    item_columns = {}
    header_rows = []
    for path in ITEM_ALIASES:
        candidates = target_sheet["field_candidates"]["items"].get(path, [])
        if len(candidates) == 1:
            column, row = _split_address(candidates[0]["label_cell"])
            item_columns[path] = column
            header_rows.append(row)
        else:
            item_columns[path] = ""
    item_header_row = header_rows[0] if header_rows and len(set(header_rows)) == 1 else 0
    return {
        "configured": False,
        "review_required": True,
        "template_file": report["template_file"],
        "template_sha256": report["template_sha256"],
        "sheet_name": target_sheet["name"],
        "header_cells": header_cells,
        "items": {
            "path": "quote.items",
            "header_row_suggestion": item_header_row,
            "start_row": item_header_row + 1 if item_header_row else 0,
            "max_rows": 0,
            "columns": item_columns,
        },
        "total_cells": total_cells,
        "clear_unused_item_rows": True,
    }


def write_template_report(template_path: Path, report_path: Path, draft_path: Path, sheet_name: str | None = None) -> None:
    """生成模板体检报告和映射草稿文件。"""
    report = inspect_xlsx_template(template_path)
    draft = mapping_draft_from_report(report, sheet_name)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
