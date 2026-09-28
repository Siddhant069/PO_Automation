"""Capsolver integration for Google reCAPTCHA v2.

Supports solving reCAPTCHA automatically using Capsolver API (ReCaptchaV2TaskProxyLess)
and injecting the resulting token into Playwright page DOM and its internal callbacks.
"""
import logging
import re
import time
from typing import Optional
import requests
from playwright.sync_api import Page

log = logging.getLogger(__name__)

CAPSOLVER_API_URL = "https://api.capsolver.com"
DEFAULT_RECAPTCHA_SITEKEY = "6LdEyyQUAAAAACuau_3HiFNBBU6SEHapaY1Ksspc"


def extract_recaptcha_sitekey(page: Page) -> str:
    """Extract reCAPTCHA sitekey from iframes or page DOM; falls back to known default."""
    try:
        # Check frames
        for frame in page.frames:
            if "recaptcha" in frame.url and "k=" in frame.url:
                match = re.search(r"[?&]k=([a-zA-Z0-9_-]+)", frame.url)
                if match:
                    return match.group(1)

        # Check DOM element or iframe src
        sitekey = page.evaluate("""() => {
            let el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            let iframe = Array.from(document.querySelectorAll('iframe')).find(f => f.src && f.src.includes('k='));
            if (iframe) {
                let m = iframe.src.match(/[?&]k=([a-zA-Z0-9_-]+)/);
                if (m) return m[1];
            }
            return null;
        }""")
        if sitekey:
            return sitekey
    except Exception as e:
        log.debug("Error while extracting sitekey: %s", e)

    return DEFAULT_RECAPTCHA_SITEKEY


def solve_recaptcha_v2(
    api_key: str,
    website_url: str,
    website_key: str,
    timeout_seconds: int = 120,
) -> str:
    """Requests Capsolver to solve Google reCAPTCHA v2 and returns gRecaptchaResponse token."""
    if not api_key:
        raise ValueError("Capsolver API key is required.")

    log.info("Sending reCAPTCHA v2 task to Capsolver (sitekey: %s)...", website_key)
    create_payload = {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "isInvisible": False,
        },
    }

    resp = requests.post(f"{CAPSOLVER_API_URL}/createTask", json=create_payload, timeout=30)
    data = resp.json()

    if data.get("errorId", 0) != 0:
        raise RuntimeError(
            f"Capsolver createTask failed [{data.get('errorCode')}]: {data.get('errorDescription')}"
        )

    if data.get("status") == "ready" and data.get("solution"):
        return data["solution"]["gRecaptchaResponse"]

    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"Capsolver did not return a taskId: {data}")

    log.info("Capsolver task created: %s. Waiting for solution...", task_id)

    start_time = time.time()
    poll_payload = {
        "clientKey": api_key,
        "taskId": task_id,
    }

    while time.time() - start_time < timeout_seconds:
        time.sleep(3)
        poll_resp = requests.post(f"{CAPSOLVER_API_URL}/getTaskResult", json=poll_payload, timeout=30)
        poll_data = poll_resp.json()

        if poll_data.get("errorId", 0) != 0:
            raise RuntimeError(
                f"Capsolver getTaskResult failed [{poll_data.get('errorCode')}]: {poll_data.get('errorDescription')}"
            )

        status = poll_data.get("status")
        if status == "ready":
            token = poll_data.get("solution", {}).get("gRecaptchaResponse")
            if not token:
                raise RuntimeError(f"Solution missing gRecaptchaResponse: {poll_data}")
            log.info("Capsolver successfully solved reCAPTCHA.")
            return token

        log.debug("Capsolver task status: %s", status)

    raise TimeoutError(f"Capsolver timed out after {timeout_seconds} seconds waiting for solution.")


def inject_recaptcha_token(page: Page, token: str) -> bool:
    """Injects solved reCAPTCHA response token into textarea and triggers grecaptcha callback."""
    return page.evaluate(
        """(token) => {
        let injected = false;

        // 1. Update all g-recaptcha-response textareas
        const textareas = document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response');
        textareas.forEach(t => {
            t.value = token;
            t.innerHTML = token;
            t.dispatchEvent(new Event('input', { bubbles: true }));
            t.dispatchEvent(new Event('change', { bubbles: true }));
            injected = true;
        });

        // 2. Trigger registered callback in ___grecaptcha_cfg
        try {
            if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
                for (let clientId in window.___grecaptcha_cfg.clients) {
                    let client = window.___grecaptcha_cfg.clients[clientId];
                    function findAndCall(obj, depth = 0) {
                        if (!obj || depth > 5) return false;
                        if (typeof obj.callback === 'function') {
                            obj.callback(token);
                            return true;
                        }
                        for (let k in obj) {
                            if (obj[k] && typeof obj[k] === 'object') {
                                if (findAndCall(obj[k], depth + 1)) return true;
                            }
                        }
                        return false;
                    }
                    if (findAndCall(client)) {
                        injected = true;
                    }
                }
            }
        } catch (e) {
            console.error('Error invoking recaptcha callback:', e);
        }

        return injected;
    }""",
        token,
    )
