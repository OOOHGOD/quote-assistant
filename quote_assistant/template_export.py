"""固定 Excel 模板导出。

核心原则：只向映射允许的既有单元格写值，不新增行列、不改样式、不改公式、不改宏和工作表结构。
为了做到这一点，本模块直接操作 xlsx/xlsm 的 OpenXML 包，而不是用 openpyxl 重新保存工作簿。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*$")

ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", DOC_REL_NS)


class TemplateExportError(RuntimeError):
    """模板导出被阻断时抛出的业务异常。"""

    pass


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _get_path(payload: Any, path: str) -> Any:
    """按点号路径读取嵌套 dict/list 数据，例如 `quote.headers.supplier.value`。"""
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _resolve_sheet_path(files: dict[str, bytes], sheet_name: str) -> str:
    """根据工作表名称解析它在 OpenXML 包里的真实 XML 路径。"""
    workbook = ET.fromstring(files["xl/workbook.xml"])
    relationship_id = None
    for sheet in workbook.findall(f".//{_q(MAIN_NS, 'sheet')}"):
        if sheet.get("name") == sheet_name:
            relationship_id = sheet.get(_q(DOC_REL_NS, "id"))
            break
    if not relationship_id:
        raise TemplateExportError(f"模板中不存在工作表：{sheet_name}")

    relationships = ET.fromstring(files["xl/_rels/workbook.xml.rels"])
    target = None
    for relation in relationships.findall(_q(PKG_REL_NS, "Relationship")):
        if relation.get("Id") == relationship_id:
            target = relation.get("Target")
            break
    if not target:
        raise TemplateExportError(f"无法解析工作表关系：{sheet_name}")
    normalized = target.replace("\\", "/").lstrip("/")
    return posixpath.normpath(normalized if normalized.startswith("xl/") else f"xl/{normalized}")


def _cell_map(sheet_root: ET.Element) -> dict[str, ET.Element]:
    """建立单元格地址到 XML 节点的索引，方便后续定点写值。"""
    return {
        cell.get("r", ""): cell
        for cell in sheet_root.findall(f".//{_q(MAIN_NS, 'c')}")
        if cell.get("r")
    }


def _set_cell_value(cell: ET.Element, value: Any) -> None:
    """在不改变单元格样式的前提下写入值。

    如果目标单元格包含公式，立即阻断；公式单元格应由 Excel 自己计算，不能被导出值覆盖。
    """
    if cell.find(_q(MAIN_NS, "f")) is not None:
        raise TemplateExportError(f"映射单元格 {cell.get('r')} 含公式，禁止覆盖。")
    for child_name in ("v", "is"):
        child = cell.find(_q(MAIN_NS, child_name))
        if child is not None:
            cell.remove(child)

    if value in (None, ""):
        cell.attrib.pop("t", None)
        return
    if isinstance(value, bool):
        cell.set("t", "b")
        ET.SubElement(cell, _q(MAIN_NS, "v")).text = "1" if value else "0"
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cell.attrib.pop("t", None)
        ET.SubElement(cell, _q(MAIN_NS, "v")).text = str(value)
        return

    cell.set("t", "inlineStr")
    inline = ET.SubElement(cell, _q(MAIN_NS, "is"))
    text = ET.SubElement(inline, _q(MAIN_NS, "t"))
    text.text = str(value)
    if str(value).startswith(" ") or str(value).endswith(" "):
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _validate_cell_address(address: str, label: str) -> None:
    """校验映射中的单元格地址是否合法。"""
    if not CELL_RE.fullmatch(address):
        raise TemplateExportError(f"{label} 的单元格映射无效：{address!r}")


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def _cell_coordinates(address: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", address)
    if not match:
        raise TemplateExportError(f"单元格地址无效：{address}")
    return int(match.group(2)), _column_number(match.group(1))


def _mapping_targets(mapping: dict[str, Any]) -> list[tuple[str, str]]:
    """展开映射配置中所有可能写入的单元格。

    这里不仅检查表头/合计，也会按明细起始行和预留行数生成每一行的目标地址。
    """
    targets: list[tuple[str, str]] = []
    for source_path, address in {**mapping.get("header_cells", {}), **mapping.get("total_cells", {})}.items():
        _validate_cell_address(str(address), source_path)
        targets.append((source_path, str(address)))

    item_config = mapping.get("items", {})
    start_row = int(item_config.get("start_row") or 0)
    max_rows = int(item_config.get("max_rows") or 0)
    columns = item_config.get("columns", {})
    if start_row < 1 or max_rows < 1 or not columns:
        raise TemplateExportError("明细区域映射未配置完整。")
    for offset in range(max_rows):
        for source_path, column in columns.items():
            address = f"{str(column).upper()}{start_row + offset}"
            _validate_cell_address(address, f"items.{source_path}")
            targets.append((f"items[{offset}].{source_path}", address))

    by_address: dict[str, list[str]] = {}
    for source_path, address in targets:
        by_address.setdefault(address, []).append(source_path)
    duplicates = {address: sources for address, sources in by_address.items() if len(sources) > 1}
    if duplicates:
        # 一个单元格只能由一个字段负责，避免后写字段覆盖前写字段。
        address, sources = next(iter(duplicates.items()))
        raise TemplateExportError(f"多个字段映射到同一单元格 {address}：{', '.join(sources)}")
    return targets


def _merged_owner(sheet_root: ET.Element, address: str) -> str | None:
    """如果单元格属于合并区域，返回该合并区域左上角单元格地址。"""
    row, column = _cell_coordinates(address)
    for merge in sheet_root.findall(f".//{_q(MAIN_NS, 'mergeCell')}"):
        reference = merge.get("ref", "")
        if ":" not in reference:
            continue
        start, end = reference.split(":", 1)
        start_row, start_column = _cell_coordinates(start)
        end_row, end_column = _cell_coordinates(end)
        if start_row <= row <= end_row and start_column <= column <= end_column:
            return start
    return None


def inspect_template_configuration(mapping_path: Path) -> dict[str, Any]:
    """正式导出前的模板配置体检。

    检查点包括：映射已启用、模板存在、目标 sheet 可解析、所有目标单元格已存在、
    映射不覆盖公式、映射不指向合并区域的非左上角单元格。
    """
    if not mapping_path.exists():
        raise TemplateExportError(f"Excel模板映射文件不存在：{mapping_path}")
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateExportError(f"Excel模板映射文件无法读取：{exc}") from exc
    if not mapping.get("configured"):
        raise TemplateExportError("尚未配置原始Excel报价模板，正式导出已被禁止。")

    template_path = mapping_path.parent / str(mapping.get("template_file") or "")
    if not template_path.is_file():
        raise TemplateExportError(f"原始Excel报价模板不存在：{template_path}")
    if template_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise TemplateExportError("严格模板模式只支持.xlsx或.xlsm；不会自动转换文件格式。")

    try:
        with zipfile.ZipFile(template_path, "r") as archive:
            files = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise TemplateExportError(f"原始Excel模板不是有效的Open XML工作簿：{exc}") from exc

    workbook = ET.fromstring(files["xl/workbook.xml"])
    sheet_names = [sheet.get("name", "") for sheet in workbook.findall(f".//{_q(MAIN_NS, 'sheet')}")]
    sheet_name = str(mapping.get("sheet_name") or "")
    sheet_path = _resolve_sheet_path(files, sheet_name)
    sheet_root = ET.fromstring(files[sheet_path])
    cells = _cell_map(sheet_root)
    targets = _mapping_targets(mapping)
    addresses = {address for _, address in targets}
    missing = sorted(address for address in addresses if address not in cells)
    if missing:
        raise TemplateExportError(f"模板缺少已映射的预置单元格：{', '.join(missing[:12])}。禁止新增单元格。")

    formula_cells = sorted(address for address in addresses if cells[address].find(_q(MAIN_NS, "f")) is not None)
    if formula_cells:
        raise TemplateExportError(f"映射包含公式单元格：{', '.join(formula_cells[:12])}。禁止覆盖公式。")
    merged_non_anchors = []
    for address in sorted(addresses):
        owner = _merged_owner(sheet_root, address)
        if owner and owner != address:
            merged_non_anchors.append(f"{address}->{owner}")
    if merged_non_anchors:
        raise TemplateExportError(f"映射指向合并区域的非左上角单元格：{', '.join(merged_non_anchors[:12])}")

    return {
        "configured": True,
        "template_path": str(template_path),
        "template_file": template_path.name,
        "template_extension": template_path.suffix.lower(),
        "template_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
        "sheet_name": sheet_name,
        "sheet_names": sheet_names,
        "sheet_path": sheet_path,
        "mapped_cell_count": len(addresses),
        "structure_fingerprint": _package_fingerprint(files, sheet_path, addresses),
    }


def _build_assignments(job: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    """把 job 数据和模板映射转换成 `{单元格地址: 写入值}`。"""
    assignments: dict[str, Any] = {}
    for source_path, address in {**mapping.get("header_cells", {}), **mapping.get("total_cells", {})}.items():
        _validate_cell_address(address, source_path)
        assignments[address] = _get_path(job, source_path)

    item_config = mapping.get("items", {})
    items = _get_path(job, item_config.get("path", "quote.items")) or []
    start_row = int(item_config.get("start_row") or 0)
    max_rows = int(item_config.get("max_rows") or 0)
    columns = item_config.get("columns", {})
    if start_row < 1 or max_rows < 1 or not columns:
        raise TemplateExportError("明细区域映射未配置完整。")
    if len(items) > max_rows:
        raise TemplateExportError(f"报价明细共 {len(items)} 行，超过模板预留的 {max_rows} 行；禁止插行或修改模板。")

    rows_to_write = max_rows if mapping.get("clear_unused_item_rows", True) else len(items)
    for offset in range(rows_to_write):
        item = items[offset] if offset < len(items) else {}
        for source_path, column in columns.items():
            address = f"{column.upper()}{start_row + offset}"
            _validate_cell_address(address, f"items.{source_path}")
            assignments[address] = _get_path(item, source_path) if item else None
    return assignments


def _normalized_sheet_xml(data: bytes, writable_cells: set[str]) -> bytes:
    """生成用于结构指纹比较的工作表 XML。

    写入值本身会变化，所以计算指纹前先清空允许写入单元格的值，只比较模板结构是否改变。
    """
    root = ET.fromstring(data)
    cells = _cell_map(root)
    for address in writable_cells:
        cell = cells.get(address)
        if cell is None:
            continue
        cell.attrib.pop("t", None)
        for child_name in ("v", "is"):
            child = cell.find(_q(MAIN_NS, child_name))
            if child is not None:
                cell.remove(child)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _package_fingerprint(files: dict[str, bytes], sheet_path: str, writable_cells: set[str]) -> str:
    """计算整个 Excel OpenXML 包的结构指纹。"""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        data = files[name]
        if name == sheet_path:
            data = _normalized_sheet_xml(data, writable_cells)
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def export_from_immutable_template(job: dict[str, Any], project_root: Path, mapping_path: Path, output_path: Path) -> dict[str, Any]:
    """从固定模板生成 Excel 副本。

    写入完成后会重新打开输出文件并再次计算结构指纹；只要除了允许单元格值以外的内容发生变化，
    输出文件会被删除并抛错，避免交付被意外改版的报价单。
    """
    inspection = inspect_template_configuration(mapping_path)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    template_path = Path(inspection["template_path"])
    if output_path.suffix.lower() != template_path.suffix.lower():
        raise TemplateExportError("输出文件扩展名必须与原始模板完全一致。")

    with zipfile.ZipFile(template_path, "r") as archive:
        infos = archive.infolist()
        files = {info.filename: archive.read(info.filename) for info in infos}

    sheet_path = inspection["sheet_path"]
    assignments = _build_assignments(job, mapping)
    writable_cells = {address for _, address in _mapping_targets(mapping)}
    original_fingerprint = _package_fingerprint(files, sheet_path, writable_cells)
    sheet_root = ET.fromstring(files[sheet_path])
    cells = _cell_map(sheet_root)
    for address, value in assignments.items():
        _set_cell_value(cells[address], value)
    files[sheet_path] = ET.tostring(sheet_root, encoding="utf-8", xml_declaration=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as archive:
        for info in infos:
            archive.writestr(deepcopy(info), files[info.filename])

    with zipfile.ZipFile(output_path, "r") as archive:
        output_files = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    output_fingerprint = _package_fingerprint(output_files, sheet_path, writable_cells)
    if set(files) != set(output_files) or original_fingerprint != output_fingerprint:
        output_path.unlink(missing_ok=True)
        raise TemplateExportError("模板结构校验失败：输出文件除允许单元格值外发生变化，已停止交付。")

    return {
        "template_path": str(template_path),
        "mapping_path": str(mapping_path),
        "sheet_name": mapping["sheet_name"],
        "written_cell_count": len(assignments),
        "mapped_cell_count": inspection["mapped_cell_count"],
        "template_sha256": inspection["template_sha256"],
        "structure_fingerprint": output_fingerprint,
        "structure_unchanged": True,
    }
