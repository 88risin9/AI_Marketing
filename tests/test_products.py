import csv
import io
import json
import unittest
import zipfile
from xml.etree import ElementTree as ET

from openpyxl import load_workbook

from app.products import (
    COLUMNS, csv_safe, example_products, parse_import, template_bytes,
    validate_product,
)


def csv_data(headers, rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def changed_xlsx_cell(content, address, kind, value):
    """Patch fixture XML, retaining original exported template formatting."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    source, dest = io.BytesIO(content), io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(dest, "w") as result:
        for entry in original.infolist():
            data = original.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                for cell in root.iter(ns + "c"):
                    if cell.attrib.get("r") == address:
                        cell.attrib.pop("t", None)
                        for child in list(cell):
                            cell.remove(child)
                        if kind == "formula":
                            ET.SubElement(cell, ns + "f").text = value
                            ET.SubElement(cell, ns + "v").text = "999"
                        else:
                            ET.SubElement(cell, ns + "v").text = value
                        break
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            result.writestr(entry, data)
    return dest.getvalue()


class ProductValidationTests(unittest.TestCase):
    def test_missing_is_visible_without_fabrication(self):
        p = validate_product({"name": "MCB", "category": "MCB", "specs": {"current_a": "16"}})
        self.assertEqual(p["purchase_price"], "")
        self.assertEqual(p["currency"], "")
        self.assertEqual(p["lead_time"], "")
        self.assertNotIn("voltage_v", p["specs"])
        self.assertIn("型号", p["missing"])
        self.assertIn("额定电压(V)", p["missing"])

    def test_identifiers_units_and_decimals_preserved(self):
        p = validate_product({"model": " 0016-A ", "unit": "PCS ", "purchase_price": "0.12345678", "moq": "1.50", "currency": "usd"})
        self.assertEqual(p["model"], " 0016-A ")
        self.assertEqual(p["unit"], "PCS ")
        self.assertEqual(p["purchase_price"], "0.12345678")
        self.assertEqual(p["moq"], "1.50")
        self.assertEqual(p["currency"], "USD")

    def test_invalid_numbers_and_currency_rejected(self):
        for field, value in [("purchase_price", "-1"), ("purchase_price", "NaN"), ("purchase_price", "1e5"), ("purchase_price", "1,000"), ("purchase_price", "1.123456789"), ("moq", "0"), ("currency", "元"), ("model", 16), ("unit", 1)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_product({field: value})
        self.assertEqual(validate_product({"purchase_price": 0})["purchase_price"], "0")

    def test_extension_and_no_mutation(self):
        original = {"category": "RELAY", "specs": {"coil_voltage": "24 V DC", "frequency_hz": 50}}
        normalized = validate_product(original)
        self.assertEqual(normalized["specs"]["frequency_hz"], "50")
        self.assertEqual(original["specs"]["frequency_hz"], 50)
        self.assertNotIn("极数", normalized["missing"])
        with self.assertRaises(ValueError):
            validate_product({"specs": False})

    def test_fictional_records_are_independent(self):
        rows = example_products()
        self.assertEqual(len(rows), 3)
        self.assertFalse(rows[0]["missing"])
        self.assertTrue(rows[2]["missing"])
        rows[0]["specs"]["poles"] = "BAD"
        self.assertEqual(example_products()[0]["specs"]["poles"], "1P")
        self.assertTrue(all("虚构" in p["source"] for p in rows))


class ProductImportTests(unittest.TestCase):
    def test_csv_preview_retains_text_and_incomplete_records(self):
        result = parse_import("suppliers.csv", csv_data(["型号", "计量单位", "采购价", "采购币种"], [[" 0010 ", "pCs ", "1.05", "USD"], ["unknown", "pcs", "", ""]]))
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["product"]["model"], " 0010 ")
        self.assertEqual(result["rows"][0]["product"]["unit"], "pCs ")
        self.assertEqual(result["rows"][1]["errors"], [])
        self.assertTrue(result["rows"][1]["warnings"])

    def test_errors_are_attached_to_rows(self):
        result = parse_import("bad.csv", csv_data(["型号", "采购价"], [["A", "2.50"], ["B", "USD 4"]]))
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["rows"][0]["errors"])
        self.assertTrue(result["rows"][1]["errors"])
        self.assertEqual(result["rows"][1]["row"], 3)

    def test_ambiguous_header_and_extension_collision_rejected(self):
        for headers in [["型号", "model"], ["型号", "unknown"]]:
            self.assertTrue(parse_import("bad.csv", csv_data(headers, [["A", "B"]]))["errors"])
        result = parse_import("specs.csv", csv_data(["型号", "额定电流(A)", "扩展参数(JSON)"], [["A", "16", json.dumps({"current_a": "32"})]]))
        self.assertTrue(result["rows"][0]["errors"])

    def test_parameter_extensions_and_gb_csv(self):
        body = csv_data(["型号", "参数:frequency_hz", "扩展参数(JSON)"], [["A", "50/60", '{"mounting":"DIN rail"}']])
        body = body.decode("utf-8-sig").encode("gb18030")
        result = parse_import("source.CSV", body)
        self.assertEqual(result["rows"][0]["product"]["specs"], {"frequency_hz": "50/60", "mounting": "DIN rail"})

    def test_empty_invalid_and_extra_cells(self):
        self.assertTrue(parse_import("x.xls", b"abc")["errors"])
        self.assertTrue(parse_import("x.csv", b"")["errors"])
        self.assertTrue(parse_import("x.xlsx", b"junk")["errors"])
        self.assertTrue(parse_import("x.csv", b"model\nA,B\n")["rows"][0]["errors"])

    def test_csv_formula_safeguard_leaves_stored_data_unchanged(self):
        for text in ["=HYPERLINK(\"bad\")", "+CMD", "-1+1", "@SUM(A1)", "  =cmd"]:
            self.assertTrue(csv_safe(text).startswith("'"))
        self.assertEqual(csv_safe("0010"), "0010")
        result = parse_import("x.csv", csv_data(["型号"], [["=not-a-formula"]]))
        self.assertEqual(result["rows"][0]["product"]["model"], "=not-a-formula")
        self.assertTrue(result["rows"][0]["warnings"])

    def test_exported_templates_round_trip(self):
        for file_format in ("csv", "xlsx"):
            for demo in (False, True):
                with self.subTest(format=file_format, demo=demo):
                    content = template_bytes(file_format, demo)
                    result = parse_import("template." + file_format, content)
                    self.assertEqual(result["errors"], [])
                    self.assertEqual(len(result["rows"]), 3 if demo else 0)
                    self.assertTrue(all(not row["errors"] for row in result["rows"]))
                    if demo:
                        self.assertEqual(result["rows"][0]["product"]["model"], "DEMO-MCB-C16-1P")
                        self.assertEqual(result["rows"][0]["product"]["unit"], "pcs")

    def test_xlsx_text_formats_numeric_types_and_no_formulas(self):
        wb = load_workbook(io.BytesIO(template_bytes("xlsx", True)))
        sheet = wb["产品资料"]
        self.assertEqual(sheet["D2"].number_format, "@")
        self.assertEqual(sheet["K2"].number_format, "@")
        self.assertEqual(sheet["D100"].number_format, "@")
        self.assertIsInstance(sheet["L2"].value, (int, float))
        self.assertEqual(sheet["L2"].value, 8.5)
        self.assertEqual(sheet["Q2"].value.date().isoformat(), "2026-09-15")
        self.assertTrue(all(cell.data_type != "f" for worksheet in wb for row in worksheet for cell in row))
        wb.close()

    def test_excel_number_model_and_formula_rejected(self):
        fixture = template_bytes("xlsx", True)
        number = changed_xlsx_cell(fixture, "D2", "number", "123")
        result = parse_import("unsafe.xlsx", number)
        self.assertTrue(result["rows"][0]["errors"])
        formula = changed_xlsx_cell(fixture, "L2", "formula", "2+2")
        result = parse_import("unsafe.xlsx", formula)
        self.assertTrue(any("公式" in error for error in result["rows"][0]["errors"]))

    def test_damaged_worksheet_is_a_preview_error(self):
        target = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(template_bytes("xlsx", True))) as source, zipfile.ZipFile(target, "w") as output:
            for entry in source.infolist():
                output.writestr(entry, b"<worksheet><broken" if entry.filename == "xl/worksheets/sheet1.xml" else source.read(entry.filename))
        result = parse_import("damaged.xlsx", target.getvalue())
        self.assertTrue(result["errors"])
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
