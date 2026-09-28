"""VendorHub login and session manager.

Integrates automated CAPTCHA solving with Capsolver via step1_login.
Persists the resulting session (cookies + storage state) and reuses it
headlessly until it expires.

KEY DESIGN DECISION
-------------------
After a fresh login the login browser is kept alive and its BrowserContext
is returned directly to the caller.  This avoids the "new headless context
from storage_state" pattern which loses the live server-side vendor/retailer
selection state that the PO API depends on.

For stored-session reuse we navigate a warm-up page so the Angular app can
re-initialise the vendor session before any API calls are made.
"""
import argparse
import logging
import sys
from typing import Optional

from playwright.sync_api import sync_playwright, BrowserContext, Browser

import config
import session_store
import step1_login

log = logging.getLogger(__name__)

LOGIN_URL      = f"{config.BASE_URL}/#/welcome/login"
AUTH_CHECK_URL = f"{config.BASE_URL}/isAuthenticated"
PO_LIST_URL    = f"{config.BASE_URL}/#/operations/po/list"


# ── helpers ────────────────────────────────────────────────────────────────────

def is_session_valid(context: BrowserContext) -> bool:
    """Checks if the current session cookies/JWT are still authenticated."""
    resp = context.request.get(AUTH_CHECK_URL)
    if resp.status != 200:
        return False
    try:
        data = resp.json()
        return bool(
            data.get("authenticated")
            or data.get("vendor_selected")
            or data.get("retailer_selected")
            or resp.ok
        )
    except Exception:
        return resp.status == 200


def _warm_up(context: BrowserContext) -> None:
    """Navigate to the PO list page so the Angular app re-initialises its
    vendor/retailer session state server-side before we make API calls."""
    page = context.new_page()
    try:
        log.info("Warming up app context (navigating to PO list)...")
        page.goto(PO_LIST_URL, wait_until="networkidle", timeout=30_000)
        log.info("Warm-up complete.")
    except Exception as e:
        log.warning("Warm-up navigation failed (non-fatal): %s", e)
    finally:
        page.close()


# ── public API ─────────────────────────────────────────────────────────────────

def get_authenticated_context(playwright, force_login: bool = False) -> BrowserContext:
    """Returns a Playwright BrowserContext with a valid vendor session.

    Strategy:
    - If a stored session is valid → load it into a headless browser, warm up
      the Angular app context, and return.
    - Otherwise → open a browser for interactive login, keep it alive, and
      return the context directly (no close + reopen cycle that loses state).

    The caller is responsible for closing via ``context.browser.close()``.
    """
    state = None if force_login else session_store.load()

    # ── Try stored session ─────────────────────────────────────────────────────
    if state:
        browser: Browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state)
        if is_session_valid(context):
            log.info("Reusing stored session.")
            _warm_up(context)   # re-establish vendor state in the SPA
            return context
        log.info("Stored session expired — will re-login.")
        context.close()
        browser.close()

    # ── Fresh interactive login ────────────────────────────────────────────────
    log.info("No valid session — starting login flow.")
    headless = config.HEADLESS if config.CAPSOLVER_API_KEY else False
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context()
    page    = context.new_page()

    try:
        ok = step1_login.perform_login(page, context)
    finally:
        page.close()   # close the login page but keep browser + context alive

    if not ok:
        browser.close()
        raise RuntimeError("Login procedure failed.")

    session_store.save(context.storage_state())
    log.info("Login successful — using live authenticated browser context.")
    return context   # browser stays open; caller closes via context.browser.close()


def interactive_login(playwright=None, headless: Optional[bool] = None) -> dict:
    """Standalone helper: performs login and returns storage_state dict.

    Args:
        playwright: An already-open Playwright instance.  If None, a new one
                    is created internally (for standalone / testing use only).
        headless:   Override headless mode.
    """
    if headless is None:
        headless = config.HEADLESS if config.CAPSOLVER_API_KEY else False

    def _do(p):
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page    = context.new_page()
        try:
            ok = step1_login.perform_login(page, context)
            if not ok:
                raise RuntimeError("Login procedure failed.")
            return context.storage_state()
        finally:
            browser.close()

    if playwright is not None:
        return _do(playwright)

    with sync_playwright() as p:
        return _do(p)


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="Force a fresh login")
    args = parser.parse_args()

    with sync_playwright() as p:
        ctx = get_authenticated_context(p, force_login=args.login)
        ok  = is_session_valid(ctx)
        log.info("Session valid: %s", ok)
        ctx.browser.close()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
