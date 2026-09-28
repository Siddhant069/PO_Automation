"""Step 2: Fetch pending-acknowledgement POs from Flipkart VendorHub.

Calls the purchase-orders API, auto-pages through all results, and returns a
structured list of PendingPO objects ready for Step 3 (individual download).

Expects a Playwright BrowserContext (from auth.get_authenticated_context) so
that session cookies / JWT are shared automatically.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import config

log = logging.getLogger(__name__)

# ── API constants ──────────────────────────────────────────────────────────────
PO_LIST_URL = f"{config.BASE_URL}/vendor/purchase-orders"
PAGE_SIZE    = 100          # max out the page size so we need fewer round-trips
STATUS       = "approved,pending_acknowledgement"


# ── Data model ─────────────────────────────────────────────────────────────────
@dataclass
class PendingPO:
    po_id:              str
    supplier_id:        str
    supplier_name:      str
    vendor_site_name:   str
    to_site_name:       str
    order_date:         str
    expiry_date:        str
    required_by_date:   str
    total_amount:       float
    currency:           str
    total_ordered_qty:  int
    total_pending_qty:  int
    transaction_type:   str
    payment_term:       str
    fulfillment_model:  str
    gstin:              str = ""
    status:             str = ""

    def days_until_expiry(self) -> Optional[int]:
        """Returns days remaining until expiry (negative = already expired)."""
        try:
            exp = datetime.fromisoformat(self.expiry_date)
            now = datetime.now(exp.tzinfo)
            return (exp - now).days
        except Exception:
            return None


# ── Core functions ─────────────────────────────────────────────────────────────
def fetch_pending_po_list(context) -> list["PendingPO"]:
    """
    Fetch all pending-acknowledgement POs.

    Navigates the real browser page to the PO list route, waits for network
    idle (all API calls done), captures every API response, and parses POs.
    Falls back to a direct context.request call with the captured CSRF token
    if the interceptor misses the response.
    """
    import json as _json
    import time
    import os
    from datetime import datetime, timedelta

    page_obj = context.new_page()

    try:
        # ── Capture ALL responses + CSRF token from outgoing requests ─────────
        all_responses: dict[str, tuple[int, str]] = {}  # url -> (status, body)
        csrf_token = {"value": ""}

        def on_request(req):
            tok = req.headers.get("x-csrf-token", "")
            if tok and not csrf_token["value"]:
                csrf_token["value"] = tok
                log.debug("Captured x-csrf-token from browser request: %s…", tok[:16])

        def on_response(resp):
            try:
                body = resp.text()
                all_responses[resp.url] = (resp.status, body)
            except Exception:
                pass

        context.on("request",  on_request)
        context.on("response", on_response)

        spa_url = f"{config.BASE_URL}/#/operations/po/list?status=approved,pending_acknowledgement"
        log.info("Navigating to: %s", spa_url)

        try:
            page_obj.goto(spa_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            log.warning("Initial navigation warning: %s", e)

        # ── Handle vendor/suite selection screen (/#/welcome/select-account) ─
        page_obj.wait_for_timeout(2_000)
        if "select-account" in page_obj.url or page_obj.get_by_text("NEXT", exact=False).is_visible():
            log.info("Vendor/suite selection screen detected — performing account selection...")
            try:
                v_opt = page_obj.get_by_text("Holistique Beauty Products", exact=False)
                if v_opt.is_visible(timeout=5000):
                    v_opt.click()
                    log.info("Clicked Vendor Account: Holistique Beauty Products")
                    page_obj.wait_for_timeout(1000)
            except Exception as e:
                log.debug("Vendor account click notice: %s", e)

            try:
                if not page_obj.get_by_text("FKI (2.0 Suite)", exact=True).is_visible():
                    n_btn = page_obj.get_by_text("NEXT", exact=True)
                    if n_btn.is_visible(timeout=2000):
                        n_btn.click()
                        log.info("Clicked intermediate NEXT button...")
                        page_obj.wait_for_timeout(2000)
            except Exception:
                pass

            try:
                s_opt = page_obj.get_by_text("FKI (2.0 Suite)", exact=True)
                if s_opt.is_visible(timeout=5000):
                    s_opt.click()
                    log.info("Clicked Suite: FKI (2.0 Suite)")
                    page_obj.wait_for_timeout(1000)
            except Exception as e:
                log.debug("Suite click notice: %s", e)

            try:
                next_btn = page_obj.get_by_text("NEXT", exact=True)
                if next_btn.is_visible(timeout=3000):
                    next_btn.click()
                    log.info("Clicked final NEXT — waiting for PO list page to load...")
                    page_obj.wait_for_load_state("networkidle", timeout=30_000)
            except Exception as e:
                log.debug("NEXT click check notice: %s", e)

        # Extra wait for any lazy API calls
        page_obj.wait_for_timeout(3_000)

        context.remove_listener("request",  on_request)
        context.remove_listener("response", on_response)

        # ── Diagnostics ───────────────────────────────────────────────────────
        page_url = page_obj.url
        log.info("Page landed at: %s", page_url)

        # Screenshot (saved next to main.py)
        screenshot_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "debug_po_page.png"
        )
        try:
            page_obj.screenshot(path=screenshot_path, full_page=False)
            log.info("Screenshot saved to: %s", screenshot_path)
            print(f"\n  [DEBUG] Screenshot saved → {screenshot_path}")
        except Exception as e:
            log.warning("Screenshot failed: %s", e)

        # Log every captured response URL and status
        log.info("All captured responses (%d):", len(all_responses))
        for url, (status, body) in sorted(all_responses.items()):
            log.info("  %d  %s", status, url)
            if "purchase-order" in url.lower():
                log.info("       ↳ BODY[:500]: %s", body[:500])

        # ── Find the PO list payload ───────────────────────────────────────────
        po_payload: dict | None = None

        for url, (status, body) in all_responses.items():
            if "/vendor/purchase-orders?" in url and status in (200, 304):
                try:
                    data = _json.loads(body)
                    po_payload = data.get("result", data)
                    log.info("Found PO list response at: %s | total=%s",
                             url, po_payload.get("total", "?"))
                    if po_payload.get("purchase_orders"):
                        break
                except Exception as e:
                    log.warning("Could not parse PO response body from %s: %s", url, e)

        # ── Fallback: direct context.request with captured CSRF token ─────────
        if po_payload is None or not po_payload.get("purchase_orders"):
            log.warning(
                "Interceptor did not capture populated vendor/purchase-orders response. "
                "Trying direct API call with captured CSRF token: %s…",
                csrf_token["value"][:16] if csrf_token["value"] else "(none)"
            )
            headers = {
                "Accept":           "application/json, text/plain, */*",
                "Referer":          f"{config.BASE_URL}/",
                "x-requested-with": "XMLHttpRequest",
                "x-csrf-token":     csrf_token["value"],
            }

            import db
            now = datetime.now()
            max_dt = db.get_max_po_date()
            if max_dt:
                # Start run from MAX(po_date) minus 1 day safety margin
                from_dt = (max_dt - timedelta(days=1)).strftime("%d-%m-%Y")
                log.info("Starting run from MAX(po_date) in DB: %s (from_date=%s)", max_dt, from_dt)
            else:
                from_dt = (now - timedelta(days=config.LOOKBACK_DAYS)).strftime("%d-%m-%Y")
                log.info("No MAX(po_date) in DB. Using default lookback of %d days (from_date=%s)", config.LOOKBACK_DAYS, from_dt)

            thru_dt = (now + timedelta(days=1)).strftime("%d-%m-%Y")

            raw_url = (
                f"{PO_LIST_URL}?page_number=1&page_size=100"
                f"&status=approved,pending_acknowledgement&order=desc"
                f"&from_date={from_dt}&thru_date={thru_dt}&sort_column=order_date"
            )
            log.info("Direct API GET: %s", raw_url)
            resp = context.request.get(raw_url, headers=headers)
            body_text = resp.text()
            log.info("Direct API: HTTP %d | body[:400]: %s", resp.status, body_text[:400])
            try:
                data = _json.loads(body_text)
                po_payload = data.get("result", data)
            except Exception as e:
                log.error("Failed to parse direct API response: %s", e)

        # ── Parse & Filter POs ────────────────────────────────────────────────
        if not po_payload:
            po_payload = {}

        raw_list = po_payload.get("purchase_orders", [])
        total    = po_payload.get("total", 0)
        log.info("Raw API list returned %d POs (total=%d); filtering for pending_acknowledgement", len(raw_list), total)

        all_pos: list[PendingPO] = []
        for raw in raw_list:
            raw_status    = raw.get("status", "")
            latest_action = raw.get("latestAction") or {}
            action_state  = latest_action.get("state", "")

            # Only include POs that require pending acknowledgement
            is_pending = (
                raw_status == "pending_acknowledgement"
                or action_state == "PENDING_ACK"
            )
            if not is_pending:
                log.debug("Skipping non-pending PO %s (status=%s, action_state=%s)",
                          raw.get("id"), raw_status, action_state)
                continue

            supplier  = raw.get("supplier", {})
            ship_addr = supplier.get("ship_from_address", {})
            all_pos.append(PendingPO(
                po_id             = raw["id"],
                supplier_id       = raw.get("supplier_id", ""),
                supplier_name     = supplier.get("name", ""),
                vendor_site_name  = raw.get("vendor_site", {}).get("name", ""),
                to_site_name      = raw.get("to_site_name", ""),
                order_date        = raw.get("order_date", ""),
                expiry_date       = raw.get("expiry_date", ""),
                required_by_date  = raw.get("required_by_date", ""),
                total_amount      = raw.get("total_amount", 0.0),
                currency          = raw.get("currency", "INR"),
                total_ordered_qty = raw.get("total_ordered_quantity", 0),
                total_pending_qty = raw.get("total_pending_quantity", 0),
                transaction_type  = raw.get("transaction_type", ""),
                payment_term      = raw.get("payment_term", ""),
                fulfillment_model = raw.get("fulfillment_model", ""),
                gstin             = ship_addr.get("gstin", ""),
                status            = raw_status,
            ))

        log.info("Found %d pending PO(s).", len(all_pos))
        return all_pos, csrf_token["value"]

    finally:
        page_obj.close()




def print_po_summary(po_list: list[PendingPO]) -> None:
    """Pretty-prints a summary table of pending POs to stdout."""
    if not po_list:
        print("  No pending POs found -- nothing to download.")
        return

    DIVIDER = "-" * 115
    print(f"\n{'=' * 115}")
    print(f"  PENDING POs TO DOWNLOAD  --  {len(po_list)} found")
    print(f"{'=' * 115}")
    print(
        f"  {'#':<4} {'PO ID':<16} {'Supplier':<26} {'To Site':<30} "
        f"{'Qty':>5} {'Amount':>10} {'Exp Days':>8}"
    )
    print(DIVIDER)

    for i, po in enumerate(po_list, 1):
        days     = po.days_until_expiry()
        days_str = f"{days}d" if days is not None else "N/A"
        urgency  = "[URGENT]" if (days is not None and days <= 3) else ("[SOON]" if (days is not None and days <= 7) else "[OK]")
        print(
            f"  {i:<4} {po.po_id:<16} {po.supplier_name:<26} "
            f"{po.to_site_name:<30} {po.total_pending_qty:>5} "
            f"{po.currency} {po.total_amount:>8,.0f}  {urgency:>8} {days_str:>5}"
        )

    print(DIVIDER)
    total_amount = sum(p.total_amount for p in po_list)
    total_qty    = sum(p.total_pending_qty for p in po_list)
    print(
        f"  {'TOTAL':<4} {'':<16} {'':<26} {'':<30} "
        f"{total_qty:>5}    INR {total_amount:>8,.0f}"
    )
    print(f"{'=' * 115}\n")

    print("  PO IDs queued for download:")
    print("  " + ", ".join(po.po_id for po in po_list))
    print()


# ── Entry point (standalone test) ─────────────────────────────────────────────
if __name__ == "__main__":
    from playwright.sync_api import sync_playwright
    import auth

    with sync_playwright() as p:
        ctx     = auth.get_authenticated_context(p)
        po_list = fetch_pending_po_list(ctx)
        print_po_summary(po_list)
        print(f"  {len(po_list)} PO(s) ready for step 3.\n")
        ctx.close()