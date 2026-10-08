"""Step 3: Process, Acknowledge, and Export Flipkart POs.

Flow per pending PO:
  1. GET  /vendor/purchase-order/{po_id} -> Fetches full details & line items
  2. POST /vendor/acknowledgement/purchase-order-async -> Acknowledges PO (action: "APPROVE")
  3. GET  /vendor/purchase-order/{po_id} -> Verifies status updated to "approved"
  4. DB   Saves header to 'flipkart_po_logs' and line items to 'flipkart_po_items'
  5. FILE Downloads the PO copy from VendorHub (kept in memory)
  6. EXCEL Builds a workbook of this run's 'flipkart_po_items' rows (kept in memory)

Nothing is written to local disk; the files are returned as (filename, bytes)
attachments for the success email.
"""
import io
import json
import logging
from datetime import datetime
from typing import Optional
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import config
import db

log = logging.getLogger(__name__)

PO_DETAIL_URL = f"{config.BASE_URL}/vendor/purchase-order"
PO_ACK_URL    = f"{config.BASE_URL}/vendor/acknowledgement/purchase-order-async"
PO_DOWNLOAD_URL = f"{config.BASE_URL}/vendor/purchase-order-download"

# CONTRACT REF ID printed on the PO Excel -> PO type shown in the success email
CONTRACT_PO_TYPES = {
    "FKI-OR-01293546": "Hyperlocal / Flipkart Minutes",
    "FKI-OR-01388640": "National",
}


def extract_contract_ref_id(xlsx_bytes: bytes) -> str:
    """Reads the value next to the 'CONTRACT REF ID' label in the PO Excel (E14 -> G14 today).

    Scans the header area for the label and returns the first non-empty cell to
    its right, so a moved or re-merged header still works. Returns "" if absent.
    """
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
        try:
            for row in wb.active.iter_rows(min_row=1, max_row=40, max_col=30, values_only=True):
                for idx, value in enumerate(row):
                    if isinstance(value, str) and value.strip().upper() == "CONTRACT REF ID":
                        for right in row[idx + 1:]:
                            if right not in (None, ""):
                                return str(right).strip()
        finally:
            wb.close()
    except Exception as e:
        log.warning("Could not read CONTRACT REF ID from PO Excel: %s", e)
    return ""


def fetch_po_details(context, po_id: str, csrf_token: str = "") -> dict:
    """Fetches full details and line items for a single PO."""
    url = f"{PO_DETAIL_URL}/{po_id}"
    headers = {
        "Accept":           "application/json, text/plain, */*",
        "Referer":          f"{config.BASE_URL}/",
        "x-requested-with": "XMLHttpRequest",
    }
    if csrf_token:
        headers["x-csrf-token"] = csrf_token

    log.info("Fetching details for PO %s...", po_id)
    resp = context.request.get(url, headers=headers)
    if resp.status != 200:
        log.error("Failed to fetch details for PO %s: HTTP %d — %s", po_id, resp.status, resp.text()[:200])
        return {}

    try:
        data = resp.json()
        orders = data.get("result", {}).get("details", {}).get("purchase_orders", [])
        return orders[0] if orders else {}
    except Exception as e:
        log.error("Failed to parse JSON for PO %s: %s", po_id, e)
        return {}


def acknowledge_po(context, po_id: str, csrf_token: str = "") -> dict:
    """Acknowledges (Approves) a pending PO."""
    headers = {
        "Accept":           "application/json, text/plain, */*",
        "Content-Type":     "application/json;charset=UTF-8",
        "Referer":          f"{config.BASE_URL}/",
        "x-requested-with": "XMLHttpRequest",
    }
    if csrf_token:
        headers["x-csrf-token"] = csrf_token

    payload = {
        "action": "APPROVE",
        "external_ref_id": po_id,
    }

    log.info("Sending Acknowledgement (APPROVE) for PO %s...", po_id)
    resp = context.request.post(PO_ACK_URL, headers=headers, data=json.dumps(payload))
    body = resp.text()
    log.info("Acknowledgement response for PO %s: HTTP %d | %s", po_id, resp.status, body[:300])

    try:
        return resp.json()
    except Exception:
        return {"status": resp.status, "raw": body}


def export_pos_to_excel(all_po_items: list[dict], filename_suffix: str = "") -> tuple[str, bytes]:
    """Builds a styled Excel workbook of PO line items in memory; returns (filename, xlsx bytes)."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix_str = f"_{filename_suffix}" if filename_suffix else ""
    excel_name = f"Flipkart_POs{suffix_str}_{timestamp}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Purchase Order Items"

    headers = [
        "PO ID", "Order Date", "Supplier", "To Site", "Status",
        "Order Item ID", "FSN", "EAN", "Product Title", "Brand",
        "Vertical", "HSN", "Qty", "Pending Qty", "Unit Price (INR)",
        "MRP (INR)", "Tax %", "Tax Amount (INR)", "CGST (INR)", "SGST (INR)", "IGST (INR)"
    ]

    ws.append(headers)

    # Style header row
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        top=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC')
    )

    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    # Append data rows
    for row in all_po_items:
        ws.append([
            row.get("po_id", ""),
            row.get("order_date", ""),
            row.get("supplier_name", ""),
            row.get("to_site_name", ""),
            row.get("status", ""),
            row.get("order_item_id", ""),
            row.get("fsn", ""),
            row.get("ean", ""),
            row.get("product_title", ""),
            row.get("brand", ""),
            row.get("vertical", ""),
            row.get("hsn", ""),
            row.get("quantity", 0),
            row.get("pending_quantity", 0),
            row.get("unit_price", 0.0),
            row.get("mrp", 0.0),
            row.get("tax_percent", 0.0),
            row.get("tax_amount", 0.0),
            row.get("cgst_amount", 0.0),
            row.get("sgst_amount", 0.0),
            row.get("igst_amount", 0.0),
        ])

    # Format data rows
    for row_cells in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for col_idx, cell in enumerate(row_cells, start=1):
            cell.border = thin_border
            if col_idx in (13, 14):  # Qty, Pending Qty
                cell.number_format = '#,##0'
                cell.alignment = Alignment(horizontal="right")
            elif col_idx in (15, 16, 17, 18, 19, 20, 21):  # Amounts / Tax
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right")

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    buffer = io.BytesIO()
    wb.save(buffer)
    log.info("Excel report built in memory: %s (%d row(s))", excel_name, len(all_po_items))
    return excel_name, buffer.getvalue()


def build_standard_payload(details: dict, po_id: str) -> dict:
    """Transforms raw VendorHub PO JSON response into standardized B2B payload format."""
    supplier = details.get("supplier") or {}
    ship_from = supplier.get("ship_from_address") or {}
    vendor_gst = ship_from.get("gstin") or "27AAKCM9228M1ZR"
    vendor_pan = vendor_gst[:10] if vendor_gst and len(vendor_gst) >= 10 else "AAKCM9228M"

    warehouse = details.get("warehouse_details") or details.get("ship_to_address") or {}
    customer_gst = warehouse.get("gstin") or ""
    customer_name = warehouse.get("name") or "Flipkart India Private Limited"

    ship_to_address_str = warehouse.get("address") or details.get("to_site_name") or ""
    if not ship_to_address_str and warehouse:
        parts = [warehouse.get("name"), warehouse.get("address1"), warehouse.get("address2"), warehouse.get("city"), warehouse.get("state"), warehouse.get("pincode")]
        ship_to_address_str = ", ".join(p for p in parts if p)
    if not ship_to_address_str:
        ship_to_address_str = customer_name

    vendor_address_parts = [
        ship_from.get("address_line1") or ship_from.get("address1"),
        ship_from.get("address_line2") or ship_from.get("address2"),
        ship_from.get("city"),
        ship_from.get("state"),
        ship_from.get("postal_code") or ship_from.get("pincode")
    ]
    vendor_address = ", ".join(p for p in vendor_address_parts if p) or "BCCL-CFA -Bhiwandi , C/O:S.S. Supply Chain Solution Pvt. Ltd. Ground Floor, BLDG C1, H No. 166/1,Gala 01, Sumeet Logistic & Industrial - INDL, Kukse- Boriwali, Bhiwandi, Thane 421302, Maharashtra"

    ship_to_pincode = str(warehouse.get("pincode") or warehouse.get("postal_code") or "")
    ship_to_city = str(warehouse.get("city") or "")

    def _format_date(dt_raw):
        if not dt_raw:
            return ""
        try:
            dt_str = str(dt_raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%d-%m-%Y")
        except Exception:
            parts = str(dt_raw)[:10].split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                return f"{parts[2]}-{parts[1]}-{parts[0]}"
            return str(dt_raw)[:10]

    po_date_str = _format_date(details.get("order_date"))
    expiry_date_str = _format_date(details.get("expiry_date"))

    items_raw = details.get("purchase_order_items") or []
    line_item_details = []
    total_qty = 0.0

    for item in items_raw:
        attr = item.get("product_category_attributes") or {}
        ean_val = str(attr.get("ean") or "nan")
        qty_val = float(item.get("quantity") or 0.0)
        total_qty += qty_val
        unit_price_val = float(item.get("supplier_app_amount") or 0.0)
        mrp_val = float(item.get("supplier_mrp_amount") or 0.0)
        line_total_val = float(qty_val * unit_price_val)
        tax_amt_val = float(item.get("tax_amount") or 0.0)
        tax_pct_val = float(item.get("tax_percent") or 0.0)
        base_amt_val = float(line_total_val - tax_amt_val)

        line_item_details.append({
            "ean": ean_val,
            "qty": qty_val,
            "hsn_code": str(item.get("hsn") or ""),
            "line_total": line_total_val,
            "product_id": str(item.get("fsn") or ""),
            "product_name": str(item.get("product_title") or ""),
            "price_breakdown": {
                "item1": qty_val,
                "item2": mrp_val,
                "item3": unit_price_val,
                "item4": base_amt_val,
                "item5": tax_pct_val,
                "item6": tax_amt_val
            },
            "unit_price_incl_tax": unit_price_val
        })

    payload = {
        "metadata": {},
        "raw_text": "Extracted from Excel",
        "accuracy_1": 0,
        "vendor_gst": vendor_gst,
        "vendor_pan": vendor_pan,
        "customer_id": "",
        "location_id": "",
        "vendor_name": supplier.get("name") or "Holistique Beauty Products Private Limited(holistique beauty products pvtltd.)",
        "customer_gst": customer_gst,
        "ship_to_city": ship_to_city,
        "customer_name": customer_name,
        "accuracy_step1": 0,
        "accuracy_step2": 0,
        "vendor_address": vendor_address,
        "ship_to_pincode": ship_to_pincode,
        "total_line_items": len(items_raw),
        "line_item_details": line_item_details,
        "purchase_order_date": po_date_str,
        "external_order_number": po_id,
        "total_item_qty_in_units": total_qty,
        "ship_to_location_address": ship_to_address_str,
        "total_line_items_explicit": len(items_raw),
        "purchase_order_expiry_date": expiry_date_str,
        "order_total_amount_incl_tax": float(details.get("total_amount") or 0.0)
    }
    return payload


def process_and_download_pos(context, po_list: list, csrf_token: str = "") -> dict:
    """Executes Step 3: Fetches details, acknowledges, stores in DB, and exports to Excel.

    Args:
        context: Playwright BrowserContext
        po_list: List of PendingPO objects from step2
        csrf_token: Optional captured CSRF token

    Returns:
        Summary dict of processed PO results. "attachments" holds (filename, bytes)
        files for the success email; "notes" lists anything that could not be attached.
    """
    if not po_list:
        log.info("No POs to process in Step 3.")
        return {"processed": 0, "acknowledged": 0, "attachments": [], "notes": [], "payloads": {}, "contracts": {}}

    processed_ids: list[str] = []
    payloads: dict[str, dict] = {}
    contracts: dict[str, str] = {}   # po_id -> CONTRACT REF ID from the PO Excel
    po_copies: list[tuple[str, bytes]] = []
    notes: list[str] = []
    ack_count = 0

    print(f"\n{'=' * 80}")
    print(f"  STEP 3: PROCESSING & ACKNOWLEDGING {len(po_list)} PO(s)")
    print(f"{'=' * 80}")

    for idx, po in enumerate(po_list, 1):
        po_id = po.po_id
        print(f"\n[{idx}/{len(po_list)}] Processing PO {po_id}...")

        # 1. Fetch full details & line items
        details = fetch_po_details(context, po_id, csrf_token=csrf_token)
        if not details:
            log.warning("Skipping PO %s due to missing detail response.", po_id)
            continue

        processed_ids.append(po_id)
        raw_items = details.get("purchase_order_items", [])
        log.info("PO %s has %d line item(s).", po_id, len(raw_items))

        # 2. Save raw API response payload to DB table 'flipkart_po_payloads'
        db.save_po_payload(po_id, details)
        # payload_file = DOWNLOAD_DIR / f"PO_{po_id}_payload.json"
        # with open(payload_file, "w", encoding="utf-8") as f:
        #     json.dump(details, f, indent=4, ensure_ascii=False)

        # 3. Build standardized payload (header columns for PDF_Base64 + success-email summary)
        std_payload = build_standard_payload(details, po_id)
        payloads[po_id] = std_payload

        # 4. Save line items to DB table 'flipkart_po_items'
        db.save_po_items(po_id, raw_items)

        # 5. Acknowledge / Approve PO
        ack_res = acknowledge_po(context, po_id, csrf_token=csrf_token)
        ack_state = ack_res.get("response", {}).get("state", "")

        if ack_res.get("response", {}).get("status") in ("INITIATED", "COMPLETED") or ack_state:
            ack_count += 1
            log.info("PO %s successfully acknowledged (state: %s).", po_id, ack_state)
            db.update_po_status(po_id, "acknowledged")
        else:
            log.warning("Acknowledgement response for PO %s unexpected: %s", po_id, ack_res)

        # 6. Download the PO Excel from VendorHub (in memory) and push its Base64 +
        #    ValidatedOutput payload to B2B_Automation.PDF_Base64
        po_doc = download_po_document(context, po_id, csrf_token=csrf_token)
        if po_doc:
            po_copies.append(po_doc)
            file_name, file_bytes = po_doc
            contract_ref_id = extract_contract_ref_id(file_bytes)
            contracts[po_id] = contract_ref_id
            log.info("PO %s CONTRACT REF ID: %s (%s)", po_id, contract_ref_id or "not found",
                     CONTRACT_PO_TYPES.get(contract_ref_id, "unknown type"))
            if not db.save_pdf_base64_payload(po_id, std_payload, file_name, file_bytes):
                notes.append(f"PO {po_id} could not be saved to B2B_Automation.PDF_Base64.")
        else:
            log.warning("PO copy for %s could not be downloaded directly. Generating fallback Excel from API items...", po_id)
            try:
                item_rows = db.get_po_items_for_export([po_id])
                if item_rows:
                    fallback_name = f"purchase_order_{po_id}.xlsx"
                    _, fallback_bytes = export_pos_to_excel(item_rows, filename_suffix=po_id)
                    fallback_doc = (fallback_name, fallback_bytes)
                    po_copies.append(fallback_doc)
                    db.save_pdf_base64_payload(po_id, std_payload, fallback_name, fallback_bytes)
                    log.info("Pushed generated fallback Excel for PO %s to B2B_Automation.PDF_Base64 and attached to email.", po_id)
            except Exception as fb_err:
                log.error("Failed to generate fallback Excel for PO %s: %s", po_id, fb_err)
            notes.append(f"PO copy for {po_id} could not be downloaded directly from VendorHub; fallback Excel was generated, attached to email & pushed to B2B_Automation.PDF_Base64.")

    # 7. Excel of everything pushed to flipkart_po_items in this run
    items_excel = None
    if processed_ids:
        try:
            item_rows = db.get_po_items_for_export(processed_ids)
            items_excel = export_pos_to_excel(item_rows, filename_suffix="Items")
        except Exception as e:
            log.error("Failed to build flipkart_po_items Excel: %s", e)
            notes.append(f"Line-items Excel could not be generated: {e}")

    return {
        "processed": len(processed_ids),
        "acknowledged": ack_count,
        "attachments": ([items_excel] if items_excel else []) + po_copies,
        "notes": notes,
        "payloads": payloads,
        "contracts": contracts,
    }


def download_po_document(context, po_id: str, csrf_token: str = "") -> Optional[tuple[str, bytes]]:
    """Downloads the PO Excel from VendorHub and returns (filename, bytes), kept in memory.

    Calls the same endpoint the 'Download' button in the PO list uses, with the
    logged-in session, so no page navigation or button lookup is involved.
    """
    headers = {
        "Accept":           "*/*",
        "Referer":          f"{config.BASE_URL}/",
        "x-requested-with": "XMLHttpRequest",
    }
    if csrf_token:
        headers["x-csrf-token"] = csrf_token

    try:
        resp = context.request.get(PO_DOWNLOAD_URL, params={"id": po_id}, headers=headers)
        data = resp.body()
        if resp.status != 200 or data[:2] != b"PK":  # xlsx files are zip archives
            log.warning("PO download for %s failed: HTTP %d, %s, %d bytes",
                        po_id, resp.status, resp.headers.get("content-type", ""), len(data))
            return None
    except Exception as e:
        log.warning("PO download error for %s: %s", po_id, e)
        return None

    # Content-Disposition: attachment; filename=purchase_order_<PO>.xlsx
    disposition = resp.headers.get("content-disposition", "")
    file_name = disposition.split("filename=")[-1].strip('"; ') if "filename=" in disposition else ""
    if po_id not in file_name:
        file_name = f"purchase_order_{po_id}.xlsx"
    log.info("Downloaded PO file %s (%d bytes)", file_name, len(data))
    print(f"  [File Downloaded] {file_name}")
    return file_name, data
