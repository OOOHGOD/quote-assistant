from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from quote_assistant.template_inspect import inspect_xlsx_template, mapping_draft_from_report


WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

SHEET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="4">
      <c r="A4" t="inlineStr"><is><t>No</t></is></c>
      <c r="B4" t="inlineStr"><is><t>Product</t></is></c>
      <c r="C4" t="inlineStr"><is><t>货物图片</t></is></c>
    </row>
  </sheetData>
</worksheet>"""


def create_image_mapping_template(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS_XML)
        archive.writestr("xl/worksheets/sheet1.xml", SHEET_XML)
        archive.writestr("xl/styles.xml", b"<styleSheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"/>")


class TemplateInspectImageTests(unittest.TestCase):
    def test_product_image_column_is_detected_from_cargo_image_label(self):
        with tempfile.TemporaryDirectory() as directory:
            template_path = Path(directory) / "image-template.xlsx"
            create_image_mapping_template(template_path)

            report = inspect_xlsx_template(template_path)
            draft = mapping_draft_from_report(report, "Sheet1")

            self.assertEqual("C", draft["items"]["columns"]["product_image.value"])
            self.assertEqual(5, draft["items"]["start_row"])


if __name__ == "__main__":
    unittest.main()
