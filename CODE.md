# Complete Source Code Documentation

This document contains the full code listing for all modules in the Flipkart VendorHub PO Automation Pipeline, including MS Graph Mailer Integration.

---

## 1. `config.py`
```python
"""Env-driven config. Fails fast on missing required vars."""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

REQUIRED = ["VENDORHUB_USERNAME", "VENDORHUB_PASSWORD", "VENDORHUB_BASE_URL"]
missing = [k for k in REQUIRED if not os.getenv(k)]
if missing:
    raise RuntimeError(f"Missing required env vars: {missing}")

USERNAME = os.environ["VENDORHUB_USERNAME"]
PASSWORD = os.environ["VENDORHUB_PASSWORD"]
BASE_URL = os.environ["VENDORHUB_BASE_URL"].rstrip("/")
SESSION_STATE_PATH = Path(os.getenv("SESSION_STATE_PATH", "./secrets/session_state.json"))
SESSION_ENC_KEY = os.getenv("SESSION_ENC_KEY", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "").strip()
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "60"))

TENANT_ID = os.getenv("TENANT_ID", "")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
SCOPE = os.getenv("SCOPE", "https://graph.microsoft.com/.default")
MAILBOX_USER = os.getenv("MAILBOX_USER", "")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "siddhant.pardhe@glidebrands.in")

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "holistique-middleware.c9wdjmzy25ra.ap-south-1.rds.amazonaws.com"),
    user=os.getenv("DB_USER", "Siddhanth"),
    password=os.getenv("DB_PASSWORD", "Siddhanth@#4321"),
    database=os.getenv("DB_NAME", "Holistique"),
    charset="utf8mb4",
    autocommit=False,
    connect_timeout=30,
    read_timeout=300,
    write_timeout=300,
)

SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
```

---

## 2. `mailer.py`
```python
"""Mailer module using Microsoft Graph API (Client Credentials Grant).

Sends HTML emails for workflow SUCCESS and FAILURE to configured recipients.
"""
import logging
import requests
from typing import List, Dict, Any

import config

log = logging.getLogger(__name__)


def get_graph_access_token() -> str:
    """Retrieves an OAuth2 access token from Microsoft Entra / Azure AD."""
    if not (config.TENANT_ID and config.CLIENT_ID and config.CLIENT_SECRET):
        raise RuntimeError("MS Graph authentication credentials (TENANT_ID, CLIENT_ID, CLIENT_SECRET) are missing.")

    token_url = f"https://login.microsoftonline.com/{config.TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": config.CLIENT_ID,
        "client_secret": config.CLIENT_SECRET,
        "scope": config.SCOPE,
    }

    log.info("Requesting MS Graph OAuth2 access token for tenant %s...", config.TENANT_ID)
    resp = requests.post(token_url, data=payload, timeout=30)
    if resp.status_code != 200:
        log.error("Failed to obtain MS Graph access token: HTTP %d — %s", resp.status_code, resp.text)
        raise RuntimeError(f"MS Graph token error: HTTP {resp.status_code} — {resp.text}")

    token_data = resp.json()
    return token_data["access_token"]


def send_email(subject: str, body_html: str, recipient_email: str = "") -> bool:
    """Sends an email via Microsoft Graph sendMail API."""
    recipient = recipient_email or config.NOTIFICATION_EMAIL
    mailbox = config.MAILBOX_USER or "AI@holistique.in"

    if not recipient:
        log.warning("No recipient email specified for notification.")
        return False

    try:
        access_token = get_graph_access_token()
        send_url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        recipients_list = []
        for em in recipient.replace(";", ",").split(","):
            em_clean = em.strip()
            if em_clean:
                recipients_list.append({"emailAddress": {"address": em_clean}})

        mail_payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": body_html,
                },
                "toRecipients": recipients_list,
            },
            "saveToSentItems": "true",
        }

        log.info("Sending email via MS Graph to %s with subject '%s'...", recipient, subject)
        resp = requests.post(send_url, headers=headers, json=mail_payload, timeout=30)

        if resp.status_code in (200, 202):
            log.info("Email sent successfully!")
            return True
        else:
            log.error("Failed to send email via MS Graph: HTTP %d — %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        log.error("Error sending email: %s", e)
        return False


def send_success_mail(po_details_list: List[Dict[str, Any]], recipient_email: str = "") -> bool:
    """Sends SUCCESS email with detailed PO summary table."""
    subject = "SUCCESS - Flipkart PO"

    total_pos = len(po_details_list)
    table_rows_html = ""

    for item in po_details_list:
        po_no = item.get("po_id") or item.get("po_no") or "N/A"
        po_date = item.get("po_date") or item.get("purchase_order_date") or item.get("order_date") or "N/A"
        expiry_date = item.get("expiry_date") or item.get("purchase_order_expiry_date") or "N/A"
        ship_to = item.get("ship_to_location_address") or item.get("to_site_name") or "N/A"
        qty = item.get("total_item_qty_in_units") or item.get("total_ordered_qty") or 0
        amount = item.get("order_total_amount_incl_tax") or item.get("total_amount") or 0.0

        table_rows_html += f"""
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; font-weight: bold; color: #1E293B;">{po_no}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{po_date}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{expiry_date}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{ship_to}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; text-align: right; color: #1E293B;">{qty}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; text-align: right; font-weight: bold; color: #059669;">₹{amount:,.2f}</td>
        </tr>
        """

    if not table_rows_html:
        table_rows_html = """
        <tr>
            <td colspan="6" style="padding: 15px; text-align: center; color: #64748B;">No new pending POs were found in this run.</td>
        </tr>
        """

    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; background-color: #F8FAFC; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; background: #FFFFFF; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); overflow: hidden; }}
            .header {{ background-color: #1E3A8A; color: #FFFFFF; padding: 24px; text-align: center; }}
            .header h1 {{ margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }}
            .content {{ padding: 24px; color: #334155; }}
            .badge {{ display: inline-block; background-color: #D1FAE5; color: #065F46; font-weight: bold; padding: 6px 12px; border-radius: 20px; font-size: 13px; margin-bottom: 16px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 14px; }}
            th {{ background-color: #F1F5F9; color: #475569; text-align: left; padding: 10px; border-bottom: 2px solid #CBD5E1; font-size: 12px; text-transform: uppercase; }}
            .footer {{ background-color: #F1F5F9; padding: 16px; text-align: center; font-size: 12px; color: #64748B; border-top: 1px solid #E2E8F0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Flipkart PO Workflow Report</h1>
            </div>
            <div class="content">
                <div class="badge">STATUS: SUCCESS</div>
                <p>Hello,</p>
                <p>The <b>Flipkart PO Workflow</b> ran successfully. All target Purchase Orders have been acknowledged, processed, and details have been logged into the database tables (<code>flipkart_po_logs</code> and <code>B2B_Automation.PDF_Base64</code>).</p>
                
                <h3 style="margin-top: 24px; color: #1E3A8A;">Processed PO Details ({total_pos} POs)</h3>
                <table>
                    <thead>
                        <tr>
                            <th>PO Number</th>
                            <th>PO Date</th>
                            <th>Expiry Date</th>
                            <th>Ship To Location</th>
                            <th style="text-align: right;">Total Units</th>
                            <th style="text-align: right;">Amount</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows_html}
                    </tbody>
                </table>
            </div>
            <div class="footer">
                This is an automated notification from Flipkart PO Automation Pipeline.
            </div>
        </div>
    </body>
    </html>
    """

    return send_email(subject, body_html, recipient_email)


def send_failure_mail(step_name: str, error_details: str, recipient_email: str = "") -> bool:
    """Sends FAILURE email with step name and exact error details."""
    subject = "FAILURE - Flipkart PO"

    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; background-color: #F8FAFC; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; background: #FFFFFF; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); overflow: hidden; }}
            .header {{ background-color: #991B1B; color: #FFFFFF; padding: 24px; text-align: center; }}
            .header h1 {{ margin: 0; font-size: 24px; font-weight: 600; }}
            .content {{ padding: 24px; color: #334155; }}
            .badge {{ display: inline-block; background-color: #FEE2E2; color: #991B1B; font-weight: bold; padding: 6px 12px; border-radius: 20px; font-size: 13px; margin-bottom: 16px; }}
            .error-box {{ background-color: #FEF2F2; border: 1px solid #FCA5A5; border-left: 4px solid #DC2626; padding: 16px; border-radius: 4px; font-family: Consolas, Monaco, monospace; font-size: 13px; color: #7F1D1D; white-space: pre-wrap; word-break: break-all; margin-top: 12px; }}
            .footer {{ background-color: #F1F5F9; padding: 16px; text-align: center; font-size: 12px; color: #64748B; border-top: 1px solid #E2E8F0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Flipkart PO Workflow Failure Alert</h1>
            </div>
            <div class="content">
                <div class="badge">STATUS: FAILURE</div>
                <p>Hello,</p>
                <p>An error occurred during the execution of the <b>Flipkart PO Automation Workflow</b>.</p>
                
                <p><b>Failed Step:</b> <span style="color: #991B1B; font-weight: bold;">{step_name}</span></p>
                
                <p><b>Exact Error Details:</b></p>
                <div class="error-box">{error_details}</div>
            </div>
            <div class="footer">
                This is an automated error notification from Flipkart PO Automation Pipeline.
            </div>
        </div>
    </body>
    </html>
    """

    return send_email(subject, body_html, recipient_email)
```

---

## 3. `main.py`
```python
"""main.py — Flipkart VendorHub PO Automation Pipeline."""
import argparse
import logging
import sys
import traceback

from playwright.sync_api import sync_playwright

import auth
import db
import mailer
import step2_po_download as step2
import step3_po_process as step3

log = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Flipkart VendorHub PO Automation")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Force a fresh browser login (ignores cached session)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging (shows raw API responses)",
    )
    parser.add_argument(
        "--all-db",
        action="store_true",
        help="Fetch and download all POs stored in flipkart_po_logs database table",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("DEBUG logging enabled.")

    print("\n" + "=" * 60)
    print("  Flipkart VendorHub — PO Automation Pipeline")
    print("=" * 60)

    current_step = "Database Initialization"
    po_summary_for_mailer = []

    try:
        db.init_db()
        max_date = db.get_max_po_date()
        if max_date:
            print(f"  [Database] Max PO Order Date recorded: {max_date}")

        with sync_playwright() as p:

            current_step = "Step 1: Authentication & Session Setup"
            print("\n[Step 1] Authenticating with VendorHub...")
            ctx = auth.get_authenticated_context(p, force_login=args.login)

            if not auth.is_session_valid(ctx):
                err_msg = "Session is not valid after authentication."
                log.error(err_msg)
                ctx.browser.close()
                raise RuntimeError(err_msg)

            print("  Session OK.\n")

            current_step = "Step 2: Fetching Pending PO List"
            print("[Step 2] Fetching pending-acknowledgement POs...")
            po_list, csrf_token = step2.fetch_pending_po_list(ctx)

            step2.print_po_summary(po_list)

            if po_list:
                new_count = db.save_po_logs(po_list, status="pending")
                latest_max = db.get_max_po_date()
                print(f"  [DB flipkart_po_logs] Saved {len(po_list)} PO logs ({new_count} new). MAX(po_date): {latest_max}\n")

            if args.all_db or not po_list:
                print("\n  [DB Query] Loading PO records directly from 'flipkart_po_logs' database table...")
                db_records = db.get_all_po_logs()
                if db_records:
                    print(f"  [DB Query] Loaded {len(db_records)} PO records from 'flipkart_po_logs'.")
                    from step2_po_download import PendingPO
                    po_list = []
                    for rec in db_records:
                        po_list.append(PendingPO(
                            po_id=rec["po_no"],
                            supplier_id="",
                            supplier_name=rec.get("supplier_name", "") or "",
                            vendor_site_name="",
                            to_site_name=rec.get("to_site_name", "") or "",
                            order_date=str(rec.get("po_date", "")) if rec.get("po_date") else "",
                            expiry_date="",
                            required_by_date="",
                            total_amount=float(rec.get("total_amount", 0.0) or 0.0),
                            currency=rec.get("currency", "INR") or "INR",
                            total_ordered_qty=0,
                            total_pending_qty=0,
                            transaction_type="",
                            payment_term="",
                            fulfillment_model="",
                            status=rec.get("status", "") or ""
                        ))

            if not po_list:
                print("  No POs found in DB or API. Exiting.\n")
                ctx.browser.close()
                mailer.send_success_mail([])
                sys.exit(0)

            current_step = "Step 3: PO Details Fetch, Approval & Database Sync"
            step3_res = step3.process_and_download_pos(ctx, po_list, csrf_token=csrf_token)
            print(f"  [Step 3 Complete] Processed: {step3_res['processed']}, Acknowledged: {step3_res['acknowledged']}")

            ctx.browser.close()

        conn = db.get_b2b_connection()
        try:
            with conn.cursor() as cursor:
                for po_obj in po_list:
                    po_id = po_obj.po_id
                    cursor.execute("SELECT Filename, PurchaseOrderDate, PurchaseOrderExpiryDate, ValidatedOutput FROM PDF_Base64 WHERE csv_filename = %s OR PONumber = %s", (po_id, po_id))
                    rec = cursor.fetchone()
                    if rec:
                        import json as _json
                        val_out = _json.loads(rec.get("ValidatedOutput") or "{}")
                        po_summary_for_mailer.append({
                            "po_id": po_id,
                            "po_date": rec.get("PurchaseOrderDate") or val_out.get("purchase_order_date") or "",
                            "expiry_date": rec.get("PurchaseOrderExpiryDate") or val_out.get("purchase_order_expiry_date") or "",
                            "ship_to_location_address": val_out.get("ship_to_location_address") or po_obj.to_site_name or "",
                            "total_item_qty_in_units": val_out.get("total_item_qty_in_units") or 0,
                            "order_total_amount_incl_tax": val_out.get("order_total_amount_incl_tax") or po_obj.total_amount or 0.0
                        })
        finally:
            conn.close()

        current_step = "Sending Success Email Notification"
        print("\n[Mailer] Sending SUCCESS notification email...")
        mailer.send_success_mail(po_summary_for_mailer)

        print("\n  Pipeline complete.\n")

    except Exception as e:
        tb_str = traceback.format_exc()
        log.error("Pipeline failed during [%s]: %s\n%s", current_step, e, tb_str)
        print(f"\n  ERROR in [{current_step}]: {e}")

        print("\n[Mailer] Sending FAILURE notification email...")
        try:
            mailer.send_failure_mail(step_name=current_step, error_details=f"{e}\n\nTraceback:\n{tb_str}")
        except Exception as mail_err:
            log.error("Failed to send failure email: %s", mail_err)

        sys.exit(1)


if __name__ == "__main__":
    main()
```
