"""Step 1: Login to Flipkart VendorHub.

Supports automated CAPTCHA solving via Capsolver (CAPSOLVER_API_KEY).
If CAPSOLVER_API_KEY is not configured or solving fails, it falls back
to manual CAPTCHA solving in a headed browser window.
"""
import logging
from typing import Optional
from playwright.sync_api import sync_playwright, Page, BrowserContext

import config
import session_store
from captcha_solver import extract_recaptcha_sitekey, solve_recaptcha_v2, inject_recaptcha_token

log = logging.getLogger(__name__)

LOGIN_URL = f"{config.BASE_URL}/#/welcome/login"


def perform_login(page: Page, context: BrowserContext) -> bool:
    """Executes the full multi-screen VendorHub login flow.

    Screen 1: Email + Proceed
    Screen 2: Password + reCAPTCHA (Capsolver automated or manual) + SIGN IN
    Screen 3: Vendor account selection (if prompted)
    Screen 4: Suite selection (if prompted)
    Session verification and storage
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # Screen 1: email + Proceed
    log.info("Entering username...")
    email_field = page.locator("input").first
    email_field.click()
    email_field.press_sequentially(config.USERNAME, delay=30)
    page.get_by_text("PROCEED", exact=False).click()

    # Screen 2: password appears after Proceed
    log.info("Waiting for password field...")
    password_field = page.locator("input[type=password]")
    password_field.wait_for(state="visible", timeout=15_000)
    password_field.click()
    password_field.press_sequentially(config.PASSWORD, delay=30)

    # Let reCAPTCHA widget settle
    page.wait_for_timeout(2000)

    capsolver_key = config.CAPSOLVER_API_KEY
    solved_by_capsolver = False

    if capsolver_key:
        try:
            sitekey = extract_recaptcha_sitekey(page)
            print(f"[Capsolver] Solving reCAPTCHA v2 (sitekey: {sitekey})...")
            token = solve_recaptcha_v2(
                api_key=capsolver_key,
                website_url=LOGIN_URL,
                website_key=sitekey,
            )
            inject_recaptcha_token(page, token)
            page.wait_for_timeout(1000)

            print("[Capsolver] CAPTCHA token injected. Submitting login form...")
            # Click SIGN IN button while listening for the response
            with page.expect_response(
                lambda r: r.url.endswith("/login") and r.request.method == "POST",
                timeout=60_000,
            ) as resp_info:
                page.get_by_text("SIGN IN", exact=True).click()

            resp = resp_info.value
            solved_by_capsolver = True
        except Exception as e:
            print(f"[Capsolver] Automated solving encountered an issue: {e}")
            log.warning("Capsolver automated solve failed: %s", e)

    if not solved_by_capsolver:
        print("Manual solve required: check 'I'm not a robot' and click SIGN IN in the browser window...")
        with page.expect_response(
            lambda r: r.url.endswith("/login") and r.request.method == "POST",
            timeout=300_000,
        ) as resp_info:
            pass
        resp = resp_info.value

    body = resp.json() if resp.status == 200 else {}
    if not (resp.status == 200 and body.get("authenticated")):
        print(f"Login Failed: HTTP {resp.status} — {resp.text()[:300]}")
        return False

    # ── Screen 3 & 4: Vendor Account & Suite Selection Flow ──────────────────
    log.info("Handling post-login Vendor Account & Suite selection...")
    page.wait_for_timeout(3000)

    # 1. Select Vendor Account
    try:
        vendor_opt = page.get_by_text("Holistique Beauty Products", exact=False)
        if vendor_opt.is_visible(timeout=8_000):
            vendor_opt.click()
            log.info("Selected Vendor Account: Holistique Beauty Products")
            page.wait_for_timeout(1000)
    except Exception as e:
        log.debug("Vendor selection notice: %s", e)

    # 2. Check for intermediate NEXT button if suite option isn't visible yet
    try:
        if not page.get_by_text("FKI (2.0 Suite)", exact=True).is_visible():
            next_btn = page.get_by_text("NEXT", exact=True)
            if next_btn.is_visible(timeout=2000):
                next_btn.click()
                log.info("Clicked intermediate NEXT button after vendor account selection.")
                page.wait_for_timeout(2000)
    except Exception:
        pass

    # 3. Select FKI (2.0 Suite) using EXACT text match
    try:
        suite_opt = page.get_by_text("FKI (2.0 Suite)", exact=True)
        if suite_opt.is_visible(timeout=8_000):
            suite_opt.click()
            log.info("Selected Suite: FKI (2.0 Suite)")
            page.wait_for_timeout(1000)
        else:
            # Fallback: find radio/element containing 2.0 Suite
            suite_alt = page.locator("*:text('2.0 Suite'), *:text('FKI (2.0 Suite)')").last
            if suite_alt.is_visible(timeout=3000):
                suite_alt.click()
                log.info("Selected Suite via fallback text match: FKI (2.0 Suite)")
                page.wait_for_timeout(1000)
    except Exception as e:
        log.warning("Suite selection (FKI 2.0 Suite) notice: %s", e)

    # 4. Click final NEXT button to submit selection
    try:
        next_btn = page.get_by_text("NEXT", exact=True)
        if next_btn.is_visible(timeout=5_000):
            next_btn.click()
            log.info("Clicked final NEXT button.")
            page.wait_for_timeout(5000)
    except Exception as e:
        log.debug("Final NEXT click notice: %s", e)

    page.wait_for_timeout(2000)  # let final redirect/API calls settle

    check = context.request.get(f"{config.BASE_URL}/isAuthenticated")
    if check.status == 200:
        data = check.json() if "application/json" in check.headers.get("content-type", "") else {}
        if data.get("authenticated") or data.get("vendor_selected") or data.get("retailer_selected") or check.ok:
            session_store.save(context.storage_state())
            print("Login Successful")
            return True

    print(f"Login Failed at final check: HTTP {check.status} — {check.text()[:300]}")
    return False


def login(headless: Optional[bool] = None) -> bool:
    """Launches browser and performs login.

    If headless is not specified:
    - Runs headless=config.HEADLESS if CAPSOLVER_API_KEY is configured.
    - Forces headless=False if CAPSOLVER_API_KEY is missing so the user can solve manually.
    """
    if headless is None:
        if config.CAPSOLVER_API_KEY:
            headless = config.HEADLESS
        else:
            headless = False  # Need GUI for manual captcha solve

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            success = perform_login(page, context)
            return success
        finally:
            browser.close()


if __name__ == "__main__":
    login()