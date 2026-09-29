"""Mailer module using Microsoft Graph API (Client Credentials Grant).

Sends HTML emails for workflow SUCCESS and FAILURE to configured recipients.
"""
import base64
import html
import io
import logging
import mimetypes
import zipfile
from datetime import datetime
import requests
from typing import List, Dict, Any, Optional, Tuple

import config

log = logging.getLogger(__name__)

Attachment = Tuple[str, bytes]  # (filename, file bytes)

# Graph's sendMail request is capped at ~4 MB; base64 inflates attachments by 4/3.
MAX_ATTACHMENT_BYTES = int(2.8 * 1024 * 1024)


def _fit_attachments(attachments: List[Attachment]) -> Tuple[List[Attachment], Optional[str]]:
    """Keeps attachments under the Graph size limit: zips them if too large, drops them if still too large.

    Returns (attachments to send, note for the email body or None).
    """
    total = sum(len(data) for _, data in attachments)
    if total <= MAX_ATTACHMENT_BYTES:
        return attachments, None

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in attachments:
            zf.writestr(name, data)
    zipped = buffer.getvalue()
    if len(zipped) <= MAX_ATTACHMENT_BYTES:
        log.info("Attachments total %d bytes — sending as one zip (%d bytes).", total, len(zipped))
        zip_name = f"Flipkart_POs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        return [(zip_name, zipped)], None

    log.error("Attachments too large to email even zipped (%d bytes) — sending without them.", len(zipped))
    return [], f"Attachments ({len(attachments)} file(s), {total / 1024 / 1024:.1f} MB) were too large to email and were not attached."


def _graph_file_attachment(name: str, data: bytes) -> dict:
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": name,
        "contentType": content_type,
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }


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


def send_email(subject: str, body_html: str, recipient_email: str = "",
               attachments: Optional[List[Attachment]] = None) -> bool:
    """Sends an email via Microsoft Graph sendMail API, with optional (filename, bytes) attachments."""
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

        # Support multiple recipient emails comma/semicolon separated if needed
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
        if attachments:
            mail_payload["message"]["attachments"] = [
                _graph_file_attachment(name, data) for name, data in attachments
            ]

        log.info("Sending email via MS Graph to %s with subject '%s' (%d attachment(s))...",
                 recipient, subject, len(attachments or []))
        resp = requests.post(send_url, headers=headers, json=mail_payload, timeout=60)

        if resp.status_code in (200, 202):
            log.info("Email sent successfully!")
            return True
        else:
            log.error("Failed to send email via MS Graph: HTTP %d — %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        log.error("Error sending email: %s", e)
        return False


def send_success_mail(po_details_list: List[Dict[str, Any]], recipient_email: str = "",
                      attachments: Optional[List[Attachment]] = None,
                      notes: Optional[List[str]] = None) -> bool:
    """Sends SUCCESS email with detailed PO summary table.

    attachments: (filename, bytes) files to attach (PO copies, line-items Excel).
    notes: warnings shown in the body, e.g. a PO copy that could not be downloaded.
    """
    subject = "SUCCESS - Flipkart PO"
    notes = list(notes or [])
    attachments, size_note = _fit_attachments(list(attachments or []))
    if size_note:
        notes.append(size_note)

    total_pos = len(po_details_list)
    
    table_rows_html = ""
    total_value = 0.0
    total_qty = 0

    for item in po_details_list:
        po_no = item.get("po_id") or item.get("po_no") or "N/A"
        po_date = item.get("po_date") or item.get("purchase_order_date") or item.get("order_date") or "N/A"
        expiry_date = item.get("expiry_date") or item.get("purchase_order_expiry_date") or "N/A"
        ship_to = item.get("ship_to_location_address") or item.get("to_site_name") or "N/A"
        
        qty = item.get("total_item_qty_in_units") or item.get("total_ordered_qty") or 0
        amount = item.get("order_total_amount_incl_tax") or item.get("total_amount") or 0.0
        
        try:
            total_value += float(amount)
            total_qty += int(qty)
        except Exception:
            pass

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

    intro_html = "All target Purchase Orders have been acknowledged, processed, and details have been logged into the database tables (<code>flipkart_po_logs</code> and <code>B2B_Automation.PDF_Base64</code>)."

    if not table_rows_html:
        intro_html = "No new Purchase Orders were fetched from Flipkart VendorHub in this run, so no action was taken."
        table_rows_html = """
        <tr>
            <td colspan="6" style="padding: 15px; text-align: center; color: #64748B;">No new pending POs were found in this run.</td>
        </tr>
        """

    extras_html = ""
    if attachments:
        names = "".join(f"<li>{html.escape(name)}</li>" for name, _ in attachments)
        extras_html += f'<p style="margin-top: 20px;"><b>Attached files:</b></p><ul>{names}</ul>'
    if notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
        extras_html += f'<p style="margin-top: 20px; color: #92400E;"><b>Notes:</b></p><ul style="color: #92400E;">{items}</ul>'

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
                <p>The <b>Flipkart PO Workflow</b> ran successfully. {intro_html}</p>
                
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
                {extras_html}
            </div>
            <div class="footer">
                This is an automated notification from Flipkart PO Automation Pipeline.
            </div>
        </div>
    </body>
    </html>
    """

    return send_email(subject, body_html, recipient_email, attachments=attachments)


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
