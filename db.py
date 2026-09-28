"""Database manager for Flipkart PO Automation (MySQL / AWS RDS).

Manages the 'flipkart_po_logs' and 'flipkart_po_items' tables in AWS RDS MySQL.
Tracks run_date, po_no, po_date, status ('pending', 'acknowledged', 'downloaded'), line items, etc.
"""
import logging
import pymysql
import pymysql.cursors
from datetime import datetime
from typing import Optional, List, Dict, Any

import config

log = logging.getLogger(__name__)


def get_connection():
    """Returns a PyMySQL connection using config.DB_CONFIG."""
    cfg = config.DB_CONFIG.copy()
    cfg["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**cfg)


def init_db() -> None:
    """Creates tables if they do not exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipkart_po_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    run_date DATETIME NOT NULL,
                    po_no VARCHAR(50) NOT NULL UNIQUE,
                    po_date DATETIME NULL,
                    supplier_name VARCHAR(255) NULL,
                    to_site_name VARCHAR(255) NULL,
                    total_amount DECIMAL(15,2) DEFAULT 0.00,
                    currency VARCHAR(10) DEFAULT 'INR',
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    INDEX idx_po_no (po_no),
                    INDEX idx_status (status),
                    INDEX idx_po_date (po_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipkart_po_items (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    po_no VARCHAR(50) NOT NULL,
                    order_item_id VARCHAR(50) NULL,
                    fsn VARCHAR(50) NULL,
                    ean VARCHAR(50) NULL,
                    product_title TEXT NULL,
                    brand VARCHAR(100) NULL,
                    vertical VARCHAR(100) NULL,
                    hsn VARCHAR(50) NULL,
                    quantity INT DEFAULT 0,
                    pending_quantity INT DEFAULT 0,
                    unit_price DECIMAL(15,2) DEFAULT 0.00,
                    mrp DECIMAL(15,2) DEFAULT 0.00,
                    tax_percent DECIMAL(5,2) DEFAULT 0.00,
                    tax_amount DECIMAL(15,2) DEFAULT 0.00,
                    cgst_amount DECIMAL(15,2) DEFAULT 0.00,
                    sgst_amount DECIMAL(15,2) DEFAULT 0.00,
                    igst_amount DECIMAL(15,2) DEFAULT 0.00,
                    created_at DATETIME NOT NULL,
                    INDEX idx_item_po_no (po_no),
                    INDEX idx_fsn (fsn)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipkart_po_payloads (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    po_no VARCHAR(50) NOT NULL UNIQUE,
                    raw_payload LONGTEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    INDEX idx_payload_po_no (po_no)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
        conn.commit()
        log.info("Database tables initialized successfully.")
    except Exception as e:
        log.error("Failed to initialize database tables: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def get_max_po_date() -> Optional[datetime]:
    """Returns the maximum po_date from flipkart_po_logs, or None if empty."""
    init_db()
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT MAX(po_date) AS max_date FROM flipkart_po_logs")
            row = cursor.fetchone()
            if row and row["max_date"]:
                res = row["max_date"]
                log.info("Found MAX(po_date) in database: %s", res)
                return res
            log.info("No existing records in flipkart_po_logs. MAX(po_date) is None.")
            return None
    except Exception as e:
        log.error("Error querying MAX(po_date): %s", e)
        return None
    finally:
        conn.close()


def save_po_logs(po_list: list, status: str = "pending") -> int:
    """Inserts or updates fetched PendingPO objects into flipkart_po_logs."""
    init_db()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    new_count = 0

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            for po in po_list:
                po_dt_str = None
                if getattr(po, "order_date", None):
                    try:
                        dt = datetime.fromisoformat(po.order_date)
                        po_dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        po_dt_str = str(po.order_date)[:19].replace("T", " ")

                po_no = po.po_id
                supplier_name = getattr(po, "supplier_name", "")
                to_site_name = getattr(po, "to_site_name", "")
                total_amount = getattr(po, "total_amount", 0.0)
                currency = getattr(po, "currency", "INR")

                cursor.execute("SELECT id FROM flipkart_po_logs WHERE po_no = %s", (po_no,))
                existing = cursor.fetchone()

                if not existing:
                    new_count += 1
                    cursor.execute("""
                        INSERT INTO flipkart_po_logs (
                            run_date, po_no, po_date, supplier_name, to_site_name,
                            total_amount, currency, status, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        now_str, po_no, po_dt_str, supplier_name, to_site_name,
                        total_amount, currency, status, now_str, now_str
                    ))
                else:
                    cursor.execute("""
                        UPDATE flipkart_po_logs SET
                            run_date = %s,
                            total_amount = %s,
                            updated_at = %s
                        WHERE po_no = %s
                    """, (now_str, total_amount, now_str, po_no))

        conn.commit()
        log.info("Saved %d PO logs to database (%d newly inserted).", len(po_list), new_count)
        return new_count
    except Exception as e:
        log.error("Failed to save PO logs to database: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def save_po_items(po_no: str, raw_items: list) -> int:
    """Inserts line items for a specific PO into flipkart_po_items."""
    init_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # Delete existing items for this PO to prevent duplicate line items
            cursor.execute("DELETE FROM flipkart_po_items WHERE po_no = %s", (po_no,))

            for item in raw_items:
                order_item_id = item.get("order_item_id")
                fsn = item.get("fsn")
                product_title = item.get("product_title")
                qty = item.get("quantity", 0)
                pending_qty = item.get("pending_quantity", 0)
                unit_price = item.get("supplier_app_amount", 0.0)
                mrp = item.get("supplier_mrp_amount", 0.0)
                tax_percent = item.get("tax_percent", 0.0)
                tax_amount = item.get("tax_amount", 0.0)
                cgst = item.get("cgst_amount", 0.0)
                sgst = item.get("sgst_amount", 0.0)
                igst = item.get("igst_amount", 0.0)
                hsn = item.get("hsn")

                attr = item.get("product_category_attributes") or {}
                ean = attr.get("ean")
                brand = attr.get("brand")
                vertical = attr.get("vertical")

                cursor.execute("""
                    INSERT INTO flipkart_po_items (
                        po_no, order_item_id, fsn, ean, product_title, brand, vertical,
                        hsn, quantity, pending_quantity, unit_price, mrp, tax_percent,
                        tax_amount, cgst_amount, sgst_amount, igst_amount, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    po_no, order_item_id, fsn, ean, product_title, brand, vertical,
                    hsn, qty, pending_qty, unit_price, mrp, tax_percent,
                    tax_amount, cgst, sgst, igst, now_str
                ))
                inserted += 1

        conn.commit()
        log.info("Saved %d line item(s) for PO %s to flipkart_po_items.", inserted, po_no)
        return inserted
    except Exception as e:
        log.error("Failed to save items for PO %s: %s", po_no, e)
        conn.rollback()
        raise
    finally:
        conn.close()


def update_po_status(po_no: str, status: str) -> bool:
    """Updates the status of a specific PO in flipkart_po_logs (e.g. 'acknowledged' or 'downloaded')."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE flipkart_po_logs SET
                    status = %s,
                    updated_at = %s
                WHERE po_no = %s
            """, (status, now_str, po_no))
        conn.commit()
        log.info("Updated PO %s status to '%s'", po_no, status)
        return True
    except Exception as e:
        log.error("Failed to update status for PO %s: %s", po_no, e)
        conn.rollback()
        return False
    finally:
        conn.close()


def save_po_payload(po_no: str, payload_dict: dict) -> bool:
    """Saves the raw API response payload of a PO into flipkart_po_payloads table."""
    import json as _json
    init_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload_json = _json.dumps(payload_dict, ensure_ascii=False)

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO flipkart_po_payloads (po_no, raw_payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    raw_payload = VALUES(raw_payload),
                    updated_at = VALUES(updated_at)
            """, (po_no, payload_json, now_str, now_str))
        conn.commit()
        log.info("Saved raw API response payload for PO %s to 'flipkart_po_payloads' table.", po_no)
        return True
    except Exception as e:
        log.error("Failed to save raw payload for PO %s: %s", po_no, e)
        conn.rollback()
        return False
    finally:
        conn.close()


def get_all_po_logs(status_filter: Optional[str] = None) -> list[dict]:
    """Retrieves records from flipkart_po_logs table."""
    init_db()
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if status_filter:
                cursor.execute("SELECT * FROM flipkart_po_logs WHERE status = %s ORDER BY po_date DESC", (status_filter,))
            else:
                cursor.execute("SELECT * FROM flipkart_po_logs ORDER BY po_date DESC")
            return cursor.fetchall()
    except Exception as e:
        log.error("Failed to query flipkart_po_logs: %s", e)
        return []
    finally:
        conn.close()


def get_b2b_connection():
    """Returns a PyMySQL connection for 'B2B_Automation' database."""
    cfg = config.DB_CONFIG.copy()
    cfg["database"] = "B2B_Automation"
    cfg["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**cfg)


def init_b2b_db() -> None:
    """Ensures database 'B2B_Automation' exists."""
    cfg_root = config.DB_CONFIG.copy()
    cfg_root.pop("database", None)
    cfg_root["cursorclass"] = pymysql.cursors.DictCursor
    try:
        root_conn = pymysql.connect(**cfg_root)
        with root_conn.cursor() as cursor:
            cursor.execute("CREATE DATABASE IF NOT EXISTS B2B_Automation DEFAULT CHARACTER SET utf8mb4;")
        root_conn.commit()
        root_conn.close()
    except Exception as e:
        log.warning("Could not auto-create B2B_Automation DB: %s", e)


def save_pdf_base64_payload(po_id: str, payload_dict: dict) -> bool:
    """Saves transformed PO payload into B2B_Automation.PDF_Base64 table."""
    import json as _json
    init_b2b_db()
    validated_output_json = _json.dumps(payload_dict, ensure_ascii=False)
    vendor_gst = payload_dict.get("vendor_gst") or ""
    po_date = payload_dict.get("purchase_order_date") or ""
    expiry_date = payload_dict.get("purchase_order_expiry_date") or ""

    conn = get_b2b_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT ID FROM PDF_Base64 WHERE csv_filename = %s OR PONumber = %s OR Filename = %s", (po_id, po_id, po_id))
            existing = cursor.fetchone()

            if existing:
                cursor.execute("""
                    UPDATE PDF_Base64 SET
                        ChannelName = %s,
                        Filename = %s,
                        csv_filename = %s,
                        Status = %s,
                        ValidatedOutput = %s,
                        VendorGST = %s,
                        PONumber = %s,
                        PurchaseOrderDate = %s,
                        PurchaseOrderExpiryDate = %s
                    WHERE ID = %s
                """, ('Flipkart OR', po_id, po_id, 'PENDING', validated_output_json, vendor_gst, po_id, po_date, expiry_date, existing['ID']))
            else:
                cursor.execute("""
                    INSERT INTO PDF_Base64 (
                        ChannelName, Filename, csv_filename, Status, ValidatedOutput,
                        VendorGST, PONumber, PurchaseOrderDate, PurchaseOrderExpiryDate
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, ('Flipkart OR', po_id, po_id, 'PENDING', validated_output_json, vendor_gst, po_id, po_date, expiry_date))
        conn.commit()
        log.info("Saved PO %s validated output payload to 'B2B_Automation.PDF_Base64' table.", po_id)
        return True
    except Exception as e:
        log.error("Failed to save B2B_Automation.PDF_Base64 record for PO %s: %s", po_id, e)
        conn.rollback()
        return False
    finally:
        conn.close()

