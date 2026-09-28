"""Step 3: Process, Acknowledge, and Export Flipkart POs.

Flow per pending PO:
  1. GET  /vendor/purchase-order/{po_id} -> Fetches full details & line items
  2. POST /vendor/acknowledgement/purchase-order-async -> Acknowledges PO (action: "APPROVE")
  3. GET  /vendor/purchase-order/{po_id} -> Verifies status updated to "approved"
  4. DB   Saves header to 'flipkart_po_logs' and line items to 'flipkart_po_items'
  5. EXCEL Exports consolidated line items to downloads/Flipkart_POs_{timestamp}.xlsx
"""
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import config
import db

log = logging.getLogger(__name__)

PO_DETAIL_URL = f"{config.BASE_URL}/vendor/purchase-order"
PO_ACK_URL    = f"{config.BASE_URL}/vendor/acknowledgement/purchase-order-async"
DOWNLOAD_DIR  = Path("downloads")


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


def export_pos_to_excel(all_po_items: list[dict], filename_suffix: str = "") -> Path:
    """Exports consolidated PO line items to a styled Excel workbook."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix_str = f"_{filename_suffix}" if filename_suffix else ""
    excel_path = DOWNLOAD_DIR / f"Flipkart_POs{suffix_str}_{timestamp}.xlsx"

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

    wb.save(excel_path)
    log.info("Excel report saved to: %s", excel_path.resolve())
    return excel_path


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
        Summary dict of processed PO results.
    """
    if not po_list:
        log.info("No POs to process in Step 3.")
        return {"processed": 0, "acknowledged": 0, "excel_file": None}

    all_exported_rows: list[dict] = []
    processed_count = 0
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

        processed_count += 1
        raw_items = details.get("purchase_order_items", [])
        log.info("PO %s has %d line item(s).", po_id, len(raw_items))

        # 2. Save raw API response payload to DB table 'flipkart_po_payloads'
        db.save_po_payload(po_id, details)
        # payload_file = DOWNLOAD_DIR / f"PO_{po_id}_payload.json"
        # with open(payload_file, "w", encoding="utf-8") as f:
        #     json.dump(details, f, indent=4, ensure_ascii=False)

        # 3. Build standardized B2B payload and save to B2B_Automation -> PDF_Base64
        std_payload = build_standard_payload(details, po_id)
        db.save_pdf_base64_payload(po_id, std_payload)
        # std_payload_file = DOWNLOAD_DIR / f"PO_{po_id}_b2b_payload.json"
        # with open(std_payload_file, "w", encoding="utf-8") as f:
        #     json.dump(std_payload, f, indent=4, ensure_ascii=False)

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

        # 6. (Disabled: File download to local folder commented out as requested)
        # po_doc_file = download_po_document(context, po_id)
        # db.update_po_status(po_id, "downloaded")

    # 7. (Disabled: Excel report generation commented out as requested)
    # excel_file = None
    # if all_exported_rows:
    #     excel_file = export_pos_to_excel(all_exported_rows)

    return {
        "processed": processed_count,
        "acknowledged": ack_count,
        "excel_file": None,
    }


def download_po_document(context, po_id: str) -> Optional[Path]:
    """Navigates to PO detail page in browser and clicks the 'Download' button."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    page = context.new_page()
    try:
        po_page_url = f"{config.BASE_URL}/#/operations/po/{po_id}"
        log.info("Navigating to PO page to click Download button: %s", po_page_url)
        page.goto(po_page_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)

        # Handle any vendor selection modal if prompted
        try:
            if page.get_by_text("NEXT", exact=False).is_visible(timeout=2000):
                page.get_by_text("NEXT", exact=False).click(force=True)
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Locate the Download button specific to this PO row if in a table, or fallback to first matching Download button
        download_btn = None
        try:
            row = page.get_by_role("row").filter(has_text=po_id)
            if row.count() > 0 and row.first.get_by_text("Download", exact=True).is_visible(timeout=3000):
                download_btn = row.first.get_by_text("Download", exact=True)
            elif page.get_by_text("Download", exact=True).count() > 0:
                download_btn = page.get_by_text("Download", exact=True).first
        except Exception:
            if page.get_by_text("Download", exact=True).count() > 0:
                download_btn = page.get_by_text("Download", exact=True).first

        if download_btn and download_btn.is_visible(timeout=3000):
            try:
                with page.expect_download(timeout=10_000) as download_info:
                    download_btn.click()
                download = download_info.value
                dest_path = DOWNLOAD_DIR / f"PO_{po_id}_{download.suggested_filename}"
                download.save_as(dest_path)
                log.info("Successfully downloaded PO file: %s", dest_path)
                print(f"  [File Downloaded] {dest_path}")
                return dest_path
            except Exception as e:
                log.warning("Download button click warning for PO %s: %s", po_id, e)
        else:
            log.info("No Download button found on page for PO %s", po_id)
    except Exception as e:
        log.warning("Browser download error for PO %s: %s", po_id, e)
    finally:
        page.close()
    return None
