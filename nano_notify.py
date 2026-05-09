#!/usr/bin/env python3
"""
NANO Notification System
Sends alerts via Telegram (primary) with Gmail fallback.
If Telegram fails, sends email to ricky.farmerai@gmail.com

Usage:
    from nano_notify import notify
    notify("🔔 New Pro sale! Key: ll_p_xxx | Email: buyer@email.com")

Setup email fallback:
    1. Go to https://myaccount.google.com/apppasswords
    2. Generate an App Password for "Mail"
    3. Save it to C:\\Users\\USER\\.nanobot\\skills\\gmail.env
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""

import os
import json
import httpx
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ── CONFIG ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "8587526488:AAEqwKpuFHrC3F_by9LjKDQLt4xvZpi1QoA"
TELEGRAM_CHAT_ID = "2119918902"
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

GMAIL_FROM       = "ricky.farmerai@gmail.com"
GMAIL_TO         = "ricky.farmerai@gmail.com"
GMAIL_SMTP       = "smtp.gmail.com"
GMAIL_PORT       = 587

# Load Gmail app password from env file
def _load_gmail_password() -> str:
    env_file = Path(r"C:\Users\USER\.nanobot\skills\gmail.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("GMAIL_APP_PASSWORD="):
                return line.split("=", 1)[1].strip()
    # Also check environment variable
    return os.environ.get("GMAIL_APP_PASSWORD", "")

# ── TELEGRAM ──────────────────────────────────────────────────────────────
def _send_telegram(message: str) -> bool:
    """Send via Telegram. Returns True if successful."""
    try:
        resp = httpx.post(
            TELEGRAM_URL,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=10.0
        )
        data = resp.json()
        if data.get("ok"):
            logging.info("✓ Telegram notification sent")
            return True
        else:
            logging.warning(f"Telegram failed: {data.get('description','unknown error')}")
            return False
    except Exception as e:
        logging.warning(f"Telegram error: {e}")
        return False

# ── EMAIL ─────────────────────────────────────────────────────────────────
def _send_email(subject: str, message: str) -> bool:
    """Send via Gmail SMTP. Returns True if successful."""
    password = _load_gmail_password()
    if not password:
        logging.error("Gmail app password not found. Add to C:\\Users\\USER\\.nanobot\\skills\\gmail.env")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[NANO] {subject}"
        msg["From"]    = GMAIL_FROM
        msg["To"]      = GMAIL_TO

        # Plain text version
        text_part = MIMEText(message, "plain")

        # HTML version with NANO styling
        html_body = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="font-family:Arial,sans-serif;background:#0a0f1a;padding:20px">
  <div style="max-width:500px;margin:0 auto;background:#111827;border-radius:10px;overflow:hidden">
    <div style="background:#1a4fd6;padding:16px 20px">
      <div style="font-family:monospace;font-size:18px;font-weight:700;color:white">NANO AI Agency</div>
      <div style="font-size:13px;color:#93c5fd;margin-top:4px">{subject}</div>
    </div>
    <div style="padding:20px">
      <pre style="font-family:monospace;font-size:13px;color:#f1f5f9;white-space:pre-wrap;line-height:1.6">{message}</pre>
    </div>
    <div style="padding:12px 20px;border-top:1px solid #1e293b;font-size:11px;color:#64748b;font-family:monospace">
      Sent: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC &nbsp;|&nbsp; 
      NANO AI Agency &nbsp;|&nbsp; 
      <a href="https://legis-link-mcp-production-3e9b.up.railway.app" style="color:#3b82f6">Legis-Link</a>
    </div>
  </div>
</body>
</html>"""
        html_part = MIMEText(html_body, "html")

        msg.attach(text_part)
        msg.attach(html_part)

        with smtplib.SMTP(GMAIL_SMTP, GMAIL_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_FROM, password)
            server.sendmail(GMAIL_FROM, GMAIL_TO, msg.as_string())

        logging.info(f"✓ Email notification sent to {GMAIL_TO}")
        return True

    except smtplib.SMTPAuthenticationError:
        logging.error("Gmail auth failed. Check app password at https://myaccount.google.com/apppasswords")
        return False
    except Exception as e:
        logging.error(f"Email error: {e}")
        return False

# ── LOG ───────────────────────────────────────────────────────────────────
def _log_notification(subject: str, message: str, channels: list):
    """Log all notifications to file."""
    log_file = Path(r"C:\Users\USER\.nanobot\NANO_NOTIFICATIONS.log")
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "subject": subject,
            "message": message[:200],
            "channels": channels
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logging.warning(f"Log write failed: {e}")

# ── MAIN NOTIFY FUNCTION ──────────────────────────────────────────────────
def notify(message: str, subject: str = "NANO Alert") -> dict:
    """
    Send notification via Telegram first, Gmail fallback if Telegram fails.
    
    Args:
        message: The notification text
        subject: Email subject line (used as header)
    
    Returns:
        dict with status of each channel
    """
    results = {"telegram": False, "email": False}

    # Try Telegram first
    results["telegram"] = _send_telegram(message)

    # Always send email too for important alerts (Pro sales)
    if "sale" in message.lower() or "pro" in message.lower() or "payment" in message.lower():
        results["email"] = _send_email(subject or "New Pro Sale", message)
    elif not results["telegram"]:
        # Telegram failed — fallback to email
        logging.info("Telegram failed — sending email fallback")
        results["email"] = _send_email(subject, message)

    # Log the notification
    channels = [k for k, v in results.items() if v]
    _log_notification(subject, message, channels)

    if not any(results.values()):
        logging.error("ALL notification channels failed!")

    return results

# ── PRO SALE NOTIFICATION ─────────────────────────────────────────────────
def notify_pro_sale(email: str, key: str, amount: str = "$199") -> dict:
    """Send Pro sale notification — always uses both channels."""
    message = (
        f"🎉 NEW PRO SALE\n\n"
        f"Amount: {amount}\n"
        f"Buyer: {email}\n"
        f"Key: {key}\n\n"
        f"Action required:\n"
        f"1. Open: C:\\Users\\USER\\.nanobot\\skills\\legis_link_pro_key_email.txt\n"
        f"2. Replace [PASTE KEY] with: {key}\n"
        f"3. Send to: {email}\n\n"
        f"Gumroad: https://app.gumroad.com/sales"
    )

    # Always use both channels for Pro sales
    telegram_ok = _send_telegram(message)
    email_ok    = _send_email("New Pro Sale", message)

    _log_notification("Pro Sale", message, 
                      (["telegram"] if telegram_ok else []) + 
                      (["email"] if email_ok else []))

    return {"telegram": telegram_ok, "email": email_ok}

# ── DAILY SUMMARY ─────────────────────────────────────────────────────────
def notify_daily_summary(queries: int, pro_sales: int, revenue: float) -> dict:
    """Send daily NANO performance summary."""
    message = (
        f"📊 NANO DAILY SUMMARY\n\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Legis-Link queries: {queries:,}\n"
        f"Pro sales today: {pro_sales}\n"
        f"Revenue today: ${revenue:.2f}\n\n"
        f"Dashboard: https://legis-link-mcp-production-3e9b.up.railway.app/health"
    )
    return notify(message, "Daily Summary")

# ── TEST ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing NANO notification system...")
    print()

    # Test Telegram
    print("1. Testing Telegram...")
    tg = _send_telegram("🔔 NANO test — Telegram working")
    print(f"   Telegram: {'✓ OK' if tg else '✗ FAILED'}")
    print()

    # Test Email
    print("2. Testing Gmail...")
    em = _send_email("Test notification", "NANO notification system test.\n\nIf you receive this, email notifications are working.")
    print(f"   Gmail: {'✓ OK' if em else '✗ FAILED (check gmail.env)'}")
    print()

    if not em:
        print("To enable Gmail notifications:")
        print("1. Go to https://myaccount.google.com/apppasswords")
        print("2. Sign in → Select app: Mail → Select device: Windows")
        print("3. Copy the 16-character app password")
        print("4. Save to C:\\Users\\USER\\.nanobot\\skills\\gmail.env:")
        print("   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx")
        print()

    # Test combined notify
    print("3. Testing combined notify...")
    result = notify("🔔 NANO notification system online — Telegram + Gmail active", "System Test")
    print(f"   Results: {result}")
