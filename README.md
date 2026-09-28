# Flipkart VendorHub PO Automation Pipeline

An automated Python pipeline built with **Playwright** and **PyMySQL** to fetch, acknowledge, log, and transform Purchase Orders (POs) from Flipkart VendorHub (FKI 2.0 Suite).

---

## 🌟 Key Features

1. **Authentication & Session Persistence (`step1_login.py`, `auth.py`)**:
   - Automated login handling with tenant selection (`FKI (2.0 Suite)` radio option & `Holistique Beauty Products Private Limited` account selection).
   - Reuses stored browser session state (`session_state.json`) to avoid unnecessary re-logins.
2. **Incremental & Full PO Fetching (`step2_po_download.py`)**:
   - Queries `MAX(po_date)` from `flipkart_po_logs` table to establish dynamic date windows (`from_date` to `thru_date`) so no POs are missed.
   - Filters pending POs (`status=approved,pending_acknowledgement`).
3. **Automated Approval & Database Logging (`step3_po_process.py`, `db.py`)**:
   - **PO Detail Fetch**: Calls `GET /vendor/purchase-order/{po_id}` API for detailed headers & line items.
   - **Raw Payload Storage**: Saves full API responses to MySQL table `flipkart_po_payloads`.
   - **Line Items Storage**: Parses and inserts line item breakdown to MySQL table `flipkart_po_items`.
   - **PO Approval/Acknowledgement**: Sends `POST /vendor/acknowledgement/purchase-order-async` (`action: "APPROVE"`).
4. **Standardized B2B Payload Transformation (`B2B_Automation.PDF_Base64`)**:
   - Maps raw API data into standardized B2B JSON payloads.
   - Inserts/updates records into database `B2B_Automation`, table `PDF_Base64`:
     - `ChannelName` = `'Flipkart OR'`
     - `Filename` / `csv_filename` / `PONumber` = `<PO_ID>`
     - `Status` = `'PENDING'`
     - `VendorGST` = Supplier GSTIN
     - `PurchaseOrderDate` = `DD-MM-YYYY`
     - `PurchaseOrderExpiryDate` = `DD-MM-YYYY`
     - `ValidatedOutput` = Transformed JSON payload

---

## 🛠️ Installation & Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Database Credentials
Database configuration is defined in `config.py`:
```python
DB_CONFIG = dict(
    host="holistique-middleware.c9wdjmzy25ra.ap-south-1.rds.amazonaws.com",
    user="Siddhanth",
    password="Siddhanth@#4321",
    database="Holistique",
    charset="utf8mb4",
)
```

---

## 🚀 Usage

### 1. Normal Pipeline Run
Runs the daily automated workflow (reuses session if valid, fetches new pending POs starting from `MAX(po_date)`):
```bash
python main.py
```

### 2. Force Fresh Login
Forces a new browser login session (useful if session cookie expired):
```bash
python main.py --login
```

### 3. Re-process All POs from DB
Loads all PO records from `flipkart_po_logs` table, re-fetches details, acknowledges, and updates `B2B_Automation.PDF_Base64`:
```bash
python main.py --all-db
```

---

## 📅 Date Handling Details

1. **Incremental Fetch Date Window (`from_date` to `thru_date`)**:
   - `get_date_range()` queries `MAX(po_date)` from `flipkart_po_logs`.
   - Passes `from_date` and `thru_date` formatted as `DD-MM-YYYY` to the Flipkart VendorHub API.
   - **Safety Guarantee**: If a daily run fails or is skipped, the next run automatically starts from the last recorded `po_date`, ensuring **no POs are ever missed**.
2. **Payload Date Formatting (`PurchaseOrderDate` & `PurchaseOrderExpiryDate`)**:
   - Parses ISO timestamp (`2026-09-28T07:28:19.000Z`) from API responses.
   - Reformats to `DD-MM-YYYY` (e.g., `28-09-2026`) for both `purchase_order_date` and `purchase_order_expiry_date` in `B2B_Automation.PDF_Base64`.
3. **Database Timestamp Formatting**:
   - Converted to `YYYY-MM-DD HH:MM:SS` for MySQL `DATETIME` columns (`po_date`, `run_date`, `created_at`, `updated_at`).

---

## 📊 Database Schema Overview

### Database: `Holistique`
- **`flipkart_po_logs`**: Tracks run history, `po_no`, `po_date`, supplier, and status (`pending`, `acknowledged`).
- **`flipkart_po_items`**: Stores line item details (FSN, EAN, Brand, Title, Quantity, MRP, Unit Price, Tax breakdown).
- **`flipkart_po_payloads`**: Stores un-truncated raw API response JSON payload for each PO.

### Database: `B2B_Automation`
- **`PDF_Base64`**: Stores standardized output JSON in `ValidatedOutput` column alongside metadata (`ChannelName='Flipkart OR'`, `Filename`, `csv_filename`, `PONumber`, `Status='PENDING'`, `VendorGST`, `PurchaseOrderDate`, `PurchaseOrderExpiryDate`).
