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
                      notes: Optional[List[str]] = None,
                      unacknowledged_pos: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Sends email with detailed PO summary table and unacknowledged PO section if any.

    attachments: (filename, bytes) files to attach (PO copies, line-items Excel).
    notes: warnings shown in the body, e.g. a PO copy that could not be downloaded.
    unacknowledged_pos: list of POs held and not acknowledged due to amount <= threshold.
    """
    notes = list(notes or [])
    unack_list = list(unacknowledged_pos or [])
    attachments, size_note = _fit_attachments(list(attachments or []))
    if size_note:
        notes.append(size_note)

    ack_count = len(po_details_list)
    unack_count = len(unack_list)

    if ack_count and unack_count:
        subject = f"SUCCESS - Flipkart PO: {ack_count} Acknowledged, {unack_count} Held (≤ ₹{config.PO_AMOUNT_THRESHOLD:,.0f})"
    elif ack_count:
        subject = f"SUCCESS - Flipkart PO ({ack_count} Processed)"
    elif unack_count:
        subject = f"ALERT - Flipkart PO: {unack_count} Held (Amount ≤ ₹{config.PO_AMOUNT_THRESHOLD:,.0f} - Not Acknowledged)"
    else:
        subject = "SUCCESS - Flipkart PO (No New POs)"

    table_rows_html = ""
    total_value = 0.0
    total_qty = 0

    for item in po_details_list:
        po_no = item.get("po_id") or item.get("po_no") or "N/A"
        contract_ref_id = item.get("contract_ref_id") or "N/A"
        po_type = item.get("po_type") or "Unknown"
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
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{contract_ref_id}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #1E293B; font-weight: bold;">{po_type}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{po_date}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{expiry_date}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; color: #475569;">{ship_to}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; text-align: right; color: #1E293B;">{qty}</td>
            <td style="padding: 10px; border-bottom: 1px solid #E2E8F0; text-align: right; font-weight: bold; color: #059669;">₹{amount:,.2f}</td>
        </tr>
        """

    if ack_count and unack_count:
        status_badge = '<div class="badge" style="background-color: #FEF3C7; color: #92400E;">STATUS: COMPLETED (ACTION REQUIRED / LOW VALUE HOLD)</div>'
        intro_html = f"The <b>Flipkart PO Workflow</b> completed. <b>{ack_count}</b> PO(s) met the threshold (&gt; ₹{config.PO_AMOUNT_THRESHOLD:,.0f}) and were acknowledged and processed to database/B2B automation. <b>{unack_count}</b> PO(s) were held unacknowledged due to amount ≤ ₹{config.PO_AMOUNT_THRESHOLD:,.0f}."
    elif ack_count:
        status_badge = '<div class="badge">STATUS: SUCCESS</div>'
        intro_html = f"The <b>Flipkart PO Workflow</b> ran successfully. All <b>{ack_count}</b> target Purchase Order(s) have been acknowledged, processed, and details logged into the database tables (<code>flipkart_po_logs</code> and <code>B2B_Automation.PDF_Base64</code>)."
    elif unack_count:
        status_badge = f'<div class="badge" style="background-color: #FEE2E2; color: #991B1B;">STATUS: HELD (AMOUNT ≤ ₹{config.PO_AMOUNT_THRESHOLD:,.0f})</div>'
        intro_html = f"The <b>Flipkart PO Workflow</b> ran. No POs were acknowledged because all <b>{unack_count}</b> pending PO(s) have order value less than or equal to ₹{config.PO_AMOUNT_THRESHOLD:,.0f} and require manual review."
    else:
        status_badge = '<div class="badge">STATUS: SUCCESS</div>'
        intro_html = "No new Purchase Orders were fetched from Flipkart VendorHub in this run, so no action was taken."

    if table_rows_html:
        ack_section_html = f"""
        <h3 style="margin-top: 24px; color: #1E3A8A;">Processed &amp; Acknowledged PO Details ({ack_count} POs)</h3>
        <table>
            <thead>
                <tr>
                    <th>PO Number</th>
                    <th>Contract Ref ID</th>
                    <th>PO Type</th>
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
        """
    elif unack_list:
        ack_section_html = f"""
        <h3 style="margin-top: 24px; color: #1E3A8A;">Processed &amp; Acknowledged PO Details (0 POs)</h3>
        <p style="color: #64748B; font-size: 13px; font-style: italic;">No POs met the minimum amount threshold (&gt; ₹{config.PO_AMOUNT_THRESHOLD:,.0f}) for acknowledgement in this run.</p>
        """
    else:
        ack_section_html = """
        <table>
            <thead>
                <tr>
                    <th>PO Number</th>
                    <th>Contract Ref ID</th>
                    <th>PO Type</th>
                    <th>PO Date</th>
                    <th>Expiry Date</th>
                    <th>Ship To Location</th>
                    <th style="text-align: right;">Total Units</th>
                    <th style="text-align: right;">Amount</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td colspan="8" style="padding: 15px; text-align: center; color: #64748B;">No new pending POs were found in this run.</td>
                </tr>
            </tbody>
        </table>
        """

    unack_section_html = ""
    if unack_list:
        unack_rows_html = ""
        for item in unack_list:
            po_no = item.get("po_id") or item.get("po_no") or "N/A"
            po_date = item.get("po_date") or item.get("purchase_order_date") or item.get("order_date") or "N/A"
            expiry_date = item.get("expiry_date") or item.get("purchase_order_expiry_date") or "N/A"
            ship_to = item.get("ship_to_location_address") or item.get("to_site_name") or "N/A"
            qty = item.get("total_item_qty_in_units") or item.get("total_ordered_qty") or 0
            amount = float(item.get("order_total_amount_incl_tax") or item.get("total_amount") or 0.0)

            unack_rows_html += f"""
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; font-weight: bold; color: #9A3412;">{po_no}</td>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; color: #475569;">{po_date}</td>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; color: #475569;">{expiry_date}</td>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; color: #475569;">{ship_to}</td>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; text-align: right; color: #1E293B;">{qty}</td>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; text-align: right; font-weight: bold; color: #C2410C;">₹{amount:,.2f}</td>
                <td style="padding: 10px; border-bottom: 1px solid #FED7AA; text-align: center;">
                    <span style="background-color: #FEE2E2; color: #991B1B; border: 1px solid #FCA5A5; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 600;">
                        NOT ACKNOWLEDGED (≤ ₹{config.PO_AMOUNT_THRESHOLD:,.0f})
                    </span>
                </td>
            </tr>
            """

        unack_section_html = f"""
        <div style="margin-top: 32px; background-color: #FFF7ED; border: 1px solid #FDBA74; border-left: 5px solid #EA580C; border-radius: 8px; padding: 18px;">
            <h3 style="margin: 0 0 6px 0; color: #9A3412; font-size: 16px;">
                ⚠️ Unacknowledged POs (Amount ≤ ₹{config.PO_AMOUNT_THRESHOLD:,.0f}) — {unack_count} PO(s)
            </h3>
            <p style="margin: 0 0 14px 0; color: #7C2D12; font-size: 13px;">
                The following Purchase Order(s) were <b>NOT acknowledged</b> and downstream B2B processing was halted because the order value is less than or equal to ₹{config.PO_AMOUNT_THRESHOLD:,.0f}.
            </p>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px; background-color: #FFFFFF; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06);">
                <thead>
                    <tr style="background-color: #FFEDD5; color: #9A3412;">
                        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #FED7AA;">PO Number</th>
                        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #FED7AA;">PO Date</th>
                        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #FED7AA;">Expiry Date</th>
                        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #FED7AA;">Ship To Location</th>
                        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #FED7AA;">Units</th>
                        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #FED7AA;">Amount</th>
                        <th style="padding: 10px; text-align: center; border-bottom: 2px solid #FED7AA;">Action / Status</th>
                    </tr>
                </thead>
                <tbody>
                    {unack_rows_html}
                </tbody>
            </table>
        </div>
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
                {status_badge}
                <p>Hello,</p>
                <p>{intro_html}</p>
                
                {ack_section_html}
                {unack_section_html}
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
