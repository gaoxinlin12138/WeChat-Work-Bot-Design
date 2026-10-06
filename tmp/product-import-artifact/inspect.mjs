import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const source = "E:/Python_work/企业微信设计/outputs/product_import_12453_20261004/花洒12453_可导入版.xlsx";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(source));
const summary = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 6000,
  tableMaxRows: 8,
  tableMaxCols: 40,
  tableMaxCellChars: 80,
});
console.log(summary.ndjson);
const preview = await workbook.render({
  sheetName: "产品档案",
  range: "A1:AI10",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  "E:/Python_work/企业微信设计/tmp/product-import-artifact/source-preview.png",
  new Uint8Array(await preview.arrayBuffer()),
);
