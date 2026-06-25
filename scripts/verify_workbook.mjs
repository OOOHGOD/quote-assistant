import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

// Excel 渲染验证脚本：读取导出的工作簿，检查关键区域、公式错误，并渲染预览图片。
// 这个脚本用于人工或 CI 辅助验收，不参与正式导出逻辑。
const [xlsxPath, previewDir] = process.argv.slice(2);
if (!xlsxPath || !previewDir) throw new Error("Usage: node verify_workbook.mjs <xlsx> <preview-dir>");

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(xlsxPath));
// 先做工作簿/工作表级摘要，确认文件能被解析。
const summary = await workbook.inspect({
  kind: "workbook,sheet",
  maxChars: 4000,
});
// 再检查报价单关键区域的值和公式，快速定位模板映射是否写到了预期位置。
const keyRange = await workbook.inspect({
  kind: "table",
  range: "报价单!A1:N14",
  include: "values,formulas",
  tableMaxRows: 14,
  tableMaxCols: 14,
  maxChars: 7000,
});
// 最后扫描常见 Excel 公式错误，避免交付带 #REF!/ #VALUE! 的文件。
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});

await fs.mkdir(previewDir, { recursive: true });
for (const sheetName of ["报价单", "审核记录", "原始证据"]) {
  const preview = await workbook.render({ sheetName, scale: 1.4 });
  await fs.writeFile(path.join(previewDir, `${sheetName}.png`), Buffer.from(await preview.arrayBuffer()));
}

console.log(JSON.stringify({
  summary: summary.ndjson,
  keyRange: keyRange.ndjson,
  errors: errors.ndjson,
}, null, 2));
