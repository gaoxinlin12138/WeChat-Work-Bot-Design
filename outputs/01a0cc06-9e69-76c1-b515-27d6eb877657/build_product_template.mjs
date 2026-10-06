import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = path.resolve(".");
const outputPath = path.join(outputDir, "产品信息收集模板_35列.xlsx");
const fontFamily = "Arial";

const headers = [
  "序号", "产品型号", "系列", "产品描述", "材质", "档位", "颜色", "特殊功能", "出货地",
  "套装价(元)", "主体价(元)", "成本价", "供应商", "主体供应商",
  "说明书", "PVC管", "电话管", "顶喷", "手喷", "喷枪", "直管", "七字管", "中叉", "固定座",
  "装饰盖", "弯角", "六分橡皮", "膨胀螺丝", "生料带", "内盒", "外箱", "布袋", "珍珠棉带", "泡沫盒",
  "备注",
];

const exampleProduct = [
  1, "LTA-822", "822系列", "恒温淋浴花洒套装", "精铜主体/不锈钢", "3档", "枪灰色", "恒温、防烫", "广东",
  899, 599, 420, "整套供应商A", "主体供应商A",
  "说明书供应商", "PVC管供应商", "电话管供应商", "顶喷供应商", "手喷供应商", "喷枪供应商",
  "直管供应商", "七字管供应商", "中叉供应商", "固定座供应商", "装饰盖供应商", "弯角供应商",
  "橡皮供应商", "螺丝供应商", "生料带供应商", "内盒供应商", "外箱供应商", "布袋供应商",
  "珍珠棉带供应商", "泡沫盒供应商", "示例数据：正式导入前请替换或删除",
];

const examplePrice = [
  "", "", "", "", "", "", "", "", "", "", "", "", "", "价格",
  1.2, 8.5, 6.5, 48, 35, 28, 15, 18, 4.5, 5.5, 3, 2.5, 0.8, 1.6, 0.7, 3.2, 8.8, 2.6, 1.9, 12,
  "对应上一行的配件采购价",
];

if (headers.length !== 35 || exampleProduct.length !== 35 || examplePrice.length !== 35) {
  throw new Error("模板列数校验失败");
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("产品档案");
const guide = workbook.worksheets.add("说明");

sheet.showGridLines = false;
sheet.tabColor = "#1F4E78";
sheet.getRange("A1:AI1").format.borders = {
  bottom: { style: "medium", color: "#1F4E78" },
};
sheet.getRange("A1").values = [["花洒产品信息收集表"]];
sheet.getRange("A1").format.font = { name: fontFamily, size: 14, bold: true, color: "#16324F" };
sheet.getRange("A1").format.verticalAlignment = "center";
sheet.getRange("A1:AI1").format.rowHeight = 28;

sheet.getRange("A2:AI2").values = [headers];
sheet.getRange("A2:AI2").format = {
  fill: "#1F4E78",
  font: { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: {
    insideVertical: { style: "thin", color: "#FFFFFF" },
    bottom: { style: "medium", color: "#17365D" },
  },
};
sheet.getRange("A2:AI2").format.rowHeight = 34;

sheet.getRange("A3:AI4").values = [exampleProduct, examplePrice];
sheet.getRange("A3:AI44").format.font = { name: fontFamily, size: 10, color: "#243447" };
sheet.getRange("A3:AI44").format.verticalAlignment = "center";
sheet.getRange("A3:AI44").format.borders = {
  insideHorizontal: { style: "thin", color: "#D9E2F3" },
  bottom: { style: "thin", color: "#D9E2F3" },
};
sheet.getRange("A3:AI3").format.fill = "#FFF4CC";
sheet.getRange("A4:AI4").format.fill = "#EAF2F8";
sheet.getRange("N4").format.font = { name: fontFamily, size: 10, bold: true, color: "#1F4E78" };
sheet.getRange("A3:A44").format.horizontalAlignment = "center";
sheet.getRange("J3:L44").format.numberFormat = "#,##0.00";
sheet.getRange("O3:AH44").format.numberFormat = "#,##0.00";
sheet.getRange("A3:A44").format.numberFormat = "0";
sheet.getRange("D3:D44").format.wrapText = true;
sheet.getRange("AI3:AI44").format.wrapText = true;
sheet.getRange("A3:AI44").format.rowHeight = 24;
sheet.getRange("D3:D44").format.rowHeight = 32;

// Keep the wide two-row import format usable without hiding important identifiers.
const widths = {
  A: 7, B: 14, C: 12, D: 24, E: 18, F: 9, G: 11, H: 18, I: 11,
  J: 12, K: 12, L: 12, M: 16, N: 16, AI: 30,
};
for (const [column, width] of Object.entries(widths)) {
  sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}
for (const column of ["O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "AA", "AB", "AC", "AD", "AE", "AF", "AG", "AH"]) {
  sheet.getRange(`${column}:${column}`).format.columnWidth = 13;
}
sheet.freezePanes.freezeRows(2);
sheet.freezePanes.freezeColumns(2);

guide.showGridLines = false;
guide.tabColor = "#5B9BD5";
guide.getRange("A2").values = [["填写与导入说明"]];
guide.getRange("A2").format.font = { name: fontFamily, size: 14, bold: true, color: "#16324F" };
guide.getRange("A3:F3").format.borders = { bottom: { style: "medium", color: "#1F4E78" } };

guide.getRange("A5:B10").values = [
  ["步骤", "说明"],
  ["1", "在“产品档案”中，每个产品使用一条产品行；产品型号为必填项。"],
  ["2", "产品行的第15–34列填写各配件的供应商。"],
  ["3", "如需填写配件采购价，请在产品行下一行的“主体供应商”列填写“价格”，并在第15–34列填写金额。"],
  ["4", "第3–4行为示例数据，正式使用时请替换或删除。请勿修改工作表名称和35列表头。"],
  ["5", "图片和视频不填写在 Excel 中；导入产品后，请在管理台产品编辑器中直接上传关联。"],
];
guide.getRange("A5:B5").format = {
  fill: "#1F4E78",
  font: { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { insideVertical: { style: "thin", color: "#FFFFFF" } },
};
guide.getRange("A6:B10").format = {
  font: { name: fontFamily, size: 10, color: "#243447" },
  verticalAlignment: "center",
  wrapText: true,
  borders: { insideHorizontal: { style: "thin", color: "#D9E2F3" } },
};
guide.getRange("A6:A10").format = {
  fill: "#EAF2F8",
  font: { name: fontFamily, size: 10, bold: true, color: "#1F4E78" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
guide.getRange("A:A").format.columnWidth = 10;
guide.getRange("B:B").format.columnWidth = 88;
guide.getRange("A5:B5").format.rowHeight = 26;
guide.getRange("A6:B10").format.rowHeight = 34;

guide.getRange("A12:B15").values = [
  ["特别提醒", ""],
  ["导入状态", "导入后的产品先进入草稿，可在管理台检查后再发布。"],
  ["金额格式", "套装价、主体价、成本价及配件采购价请填写数字，不要输入负数。"],
  ["文件字段", "新版模板已移除主图、图集、配置图和视频文件名字段。"],
];
guide.getRange("A12:B12").format = {
  fill: "#D9EAF7",
  font: { name: fontFamily, size: 10, bold: true, color: "#16324F" },
  borders: { bottom: { style: "thin", color: "#9CC2E5" } },
};
guide.getRange("A13:A15").format.font = { name: fontFamily, size: 10, bold: true, color: "#1F4E78" };
guide.getRange("B13:B15").format.font = { name: fontFamily, size: 10, color: "#243447" };
guide.getRange("A13:B15").format.verticalAlignment = "center";
guide.getRange("A13:B15").format.wrapText = true;
guide.getRange("A13:B15").format.rowHeight = 28;

workbook.recalculate();

const inspections = [];
inspections.push(await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 3000 }));
inspections.push(await workbook.inspect({ kind: "region", sheetId: "产品档案", range: "A1:AI4", maxChars: 12000 }));
inspections.push(await workbook.inspect({ kind: "region", sheetId: "说明", range: "A2:B15", maxChars: 8000 }));
await fs.writeFile(path.join(outputDir, "workbook-inspection.txt"), inspections.map((item) => item.ndjson ?? String(item)).join("\n\n"), "utf8");

const errorPattern = /#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/;
const formulaErrors = [];
for (const currentSheet of [sheet, guide]) {
  const used = currentSheet.getUsedRange();
  const matrices = [used.values, used.formulas];
  matrices.forEach((matrix, matrixIndex) => {
    matrix.forEach((row, rowIndex) => row.forEach((value, columnIndex) => {
      if (typeof value === "string" && errorPattern.test(value)) {
        formulaErrors.push({ sheet: currentSheet.name, matrixIndex, rowIndex, columnIndex, value });
      }
    }));
  });
}
if (formulaErrors.length > 0) {
  throw new Error(`发现公式错误：${JSON.stringify(formulaErrors)}`);
}

for (const sheetName of ["产品档案", "说明"]) {
  const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(outputDir, `${sheetName}-预览.png`), new Uint8Array(await preview.arrayBuffer()));
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, headers: headers.length, formulaErrors: formulaErrors.length }, null, 2));
