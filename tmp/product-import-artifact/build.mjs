import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const source = "E:/Python_work/企业微信设计/outputs/product_import_12453_20261004/花洒12453_可导入版.xlsx";
const outputDir = "E:/Python_work/企业微信设计/outputs/product_import_12453_20261004";
const sourceBook = await SpreadsheetFile.importXlsx(await FileBlob.load(source));
const sourceSheet = sourceBook.worksheets.getItem("产品档案");
const sourceValues = sourceSheet.getRange("A1:AI124").values;

const base = ["序号", "产品型号", "系列", "产品描述", "材质", "档位", "颜色", "特殊功能", "出货地", "套装价", "成本价", "主体价", "件数", "供应商", "主体供应商"];
const components = ["说明书", "PVC管", "电话管", "顶喷", "手喷", "喷枪", "淋浴杆", "中叉", "固定座", "装饰盖*2", "弯角*2", "橡皮*4", "止水阀*2", "生料带", "内盒", "外箱", "布袋", "珍珠棉袋", "泡沫盒"];
const headers = [...base, ...components, "备注"];
const oldComponentIndexes = [14, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33];
const oldPriceIndexes = oldComponentIndexes;
const rows = [["花洒产品档案"], headers];
for (let rowIndex = 2; rowIndex < sourceValues.length; rowIndex += 1) {
  const sourceRow = sourceValues[rowIndex];
  if (!sourceRow?.some((value) => value !== null && value !== "")) continue;
  const model = sourceRow[1];
  if (sourceRow[13] === "价格") {
    const priceRow = Array(headers.length).fill(null);
    priceRow[14] = "价格";
    oldPriceIndexes.forEach((oldIndex, componentIndex) => {
      priceRow[15 + componentIndex] = sourceRow[oldIndex];
    });
    rows.push(priceRow);
    continue;
  }
  if (!model) continue;
  const productRow = Array(headers.length).fill(null);
  productRow.splice(0, 12, ...sourceRow.slice(0, 9), sourceRow[9], sourceRow[11], sourceRow[10]);
  productRow[12] = null;
  productRow[13] = sourceRow[12];
  productRow[14] = sourceRow[13];
  oldComponentIndexes.forEach((oldIndex, componentIndex) => {
    productRow[15 + componentIndex] = sourceRow[oldIndex];
  });
  productRow[34] = sourceRow[34];
  rows.push(productRow);
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("产品档案");
sheet.showGridLines = false;
sheet.getRangeByIndexes(0, 0, rows.length, headers.length).values = rows;
sheet.freezePanes.freezeRows(2);
sheet.getRange("A1:AI1").merge();
sheet.getRange("A1:AI1").format = {
  fill: "#163B5C",
  font: { name: "Aptos", size: 14, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
sheet.getRange("A2:AI2").format = {
  fill: "#EAF1F7",
  font: { name: "Aptos", size: 10, bold: true, color: "#163B5C" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "all", style: "thin", color: "#B9CBD9" },
};
sheet.getRange(`A3:AI${rows.length}`).format = {
  font: { name: "Aptos", size: 10, color: "#1F2937" },
  verticalAlignment: "center",
  wrapText: true,
};
sheet.getRange(`J3:L${rows.length}`).format.numberFormat = "0.00";
sheet.getRange(`O3:AG${rows.length}`).format.numberFormat = "0.00";
sheet.getRange(`A1:AI${rows.length}`).format.borders = {
  insideHorizontal: { style: "thin", color: "#D6E0E8" },
  bottom: { style: "thin", color: "#B9CBD9" },
};
sheet.getRange("A:A").format.columnWidth = 9;
sheet.getRange("B:B").format.columnWidth = 16;
sheet.getRange("C:C").format.columnWidth = 22;
sheet.getRange("D:D").format.columnWidth = 34;
sheet.getRange("E:I").format.columnWidth = 16;
sheet.getRange("J:L").format.columnWidth = 12;
sheet.getRange("M:M").format.columnWidth = 10;
sheet.getRange("N:O").format.columnWidth = 18;
sheet.getRange("P:AH").format.columnWidth = 15;
sheet.getRange("AI:AI").format.columnWidth = 18;
sheet.getRange("A1:AI1").format.rowHeight = 28;
sheet.getRange("A2:AI2").format.rowHeight = 34;

workbook.recalculate();
const check = await workbook.inspect({
  kind: "table",
  range: "产品档案!A1:AI8",
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 35,
  maxChars: 9000,
});
console.log(check.ndjson);
const preview = await workbook.render({
  sheetName: "产品档案",
  range: "A1:AI10",
  scale: 1,
  format: "png",
});
await fs.writeFile(`${outputDir}/花洒12453_新字段预览.png`, new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(`${outputDir}/花洒12453_新字段可导入版.xlsx`);
