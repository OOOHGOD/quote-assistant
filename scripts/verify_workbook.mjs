import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [xlsxPath, previewDir] = process.argv.slice(2);
if (!xlsxPath || !previewDir) throw new Error("Usage: node verify_workbook.mjs <xlsx> <preview-dir>");

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(xlsxPath));
const summary = await workbook.inspect({
  kind: "workbook,sheet",
  maxChars: 4000,
});
const keyRange = await workbook.inspect({
  kind: "table",
  range: "报价单!A1:N14",
  include: "values,formulas",
  tableMaxRows: 14,
  tableMaxCols: 14,
  maxChars: 7000,
});
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
