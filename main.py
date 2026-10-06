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
        # Initialize database & display max PO date
        db.init_db()
        max_date = db.get_max_po_date()
        if max_date:
            print(f"  [Database] Max PO Order Date recorded: {max_date}")

        with sync_playwright() as p:

            # ── Step 1: Login / session reuse ─────────────────────────────
            current_step = "Step 1: Authentication & Session Setup"
            print("\n[Step 1] Authenticating with VendorHub...")
            ctx = auth.get_authenticated_context(p, force_login=args.login)

            if not auth.is_session_valid(ctx):
                err_msg = "Session is not valid after authentication."
                log.error(err_msg)
                ctx.browser.close()
                raise RuntimeError(err_msg)

            print("  Session OK.\n")

            # ── Step 2: Fetch pending POs from VendorHub API ──────────────
            current_step = "Step 2: Fetching Pending PO List"
            print("[Step 2] Fetching pending-acknowledgement POs...")
            po_list, csrf_token = step2.fetch_pending_po_list(ctx)

            step2.print_po_summary(po_list)

            # ── Database Storage (flipkart_po_logs) ─────────────────────────
            if po_list:
                new_count = db.save_po_logs(po_list, status="pending")
                latest_max = db.get_max_po_date()
                print(f"  [DB flipkart_po_logs] Saved {len(po_list)} PO logs ({new_count} new). MAX(po_date): {latest_max}\n")

            # Only re-process POs from flipkart_po_logs when explicitly asked via --all-db
            if args.all_db:
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
                print("  No new POs fetched. Sending notification and exiting.\n")
                ctx.browser.close()
                mailer.send_success_mail([])
                sys.exit(0)

            # ── Step 3: Fetch details, Acknowledge, Save Items & Transform B2B Payload ──
            current_step = "Step 3: PO Details Fetch, Approval & Database Sync"
            step3_res = step3.process_and_download_pos(ctx, po_list, csrf_token=csrf_token)
            print(f"  [Step 3 Complete] Processed: {step3_res['processed']}, Acknowledged: {step3_res['acknowledged']}")

            ctx.browser.close()

        # Build list of processed PO info for success email from B2B_Automation database
        conn = db.get_b2b_connection()
        try:
            with conn.cursor() as cursor:
                for po_obj in po_list:
                    po_id = po_obj.po_id
                    cursor.execute("SELECT Filename, PurchaseOrderDate, PurchaseOrderExpiryDate, ValidatedOutput FROM PDF_Base64 WHERE csv_filename = %s OR PONumber = %s", (po_id, po_id))
                    rec = cursor.fetchone()
                    if rec:
                        import json as _json
                        # ValidatedOutput is filled later by the parser; until then use this run's payload
                        val_out = _json.loads(rec.get("ValidatedOutput") or "{}") or step3_res["payloads"].get(po_id, {})
                        contract_ref_id = step3_res["contracts"].get(po_id, "")
                        po_summary_for_mailer.append({
                            "po_id": po_id,
                            "contract_ref_id": contract_ref_id,
                            "po_type": step3.CONTRACT_PO_TYPES.get(contract_ref_id, "Unknown"),
                            "po_date": rec.get("PurchaseOrderDate") or val_out.get("purchase_order_date") or "",
                            "expiry_date": rec.get("PurchaseOrderExpiryDate") or val_out.get("purchase_order_expiry_date") or "",
                            "ship_to_location_address": val_out.get("ship_to_location_address") or po_obj.to_site_name or "",
                            "total_item_qty_in_units": val_out.get("total_item_qty_in_units") or 0,
                            "order_total_amount_incl_tax": val_out.get("order_total_amount_incl_tax") or po_obj.total_amount or 0.0
                        })
        finally:
            conn.close()

        # ── Send Success Email Notification ──────────────────────────────
        current_step = "Sending Success Email Notification"
        print(f"\n[Mailer] Sending SUCCESS notification email with {len(step3_res['attachments'])} attachment(s)...")
        mailer.send_success_mail(
            po_summary_for_mailer,
            attachments=step3_res["attachments"],
            notes=step3_res["notes"],
        )

        print("\n  Pipeline complete.\n")

    except Exception as e:
        tb_str = traceback.format_exc()
        log.error("Pipeline failed during [%s]: %s\n%s", current_step, e, tb_str)
        print(f"\n  ERROR in [{current_step}]: {e}")

        # ── Send Failure Email Notification ──────────────────────────────
        print("\n[Mailer] Sending FAILURE notification email...")
        try:
            mailer.send_failure_mail(step_name=current_step, error_details=f"{e}\n\nTraceback:\n{tb_str}")
        except Exception as mail_err:
            log.error("Failed to send failure email: %s", mail_err)

        sys.exit(1)


if __name__ == "__main__":
    main()
