import base64
import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from db import get_db

logger = logging.getLogger(__name__)


def push_configuration():
    public_key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("VAPID_SUBJECT", "").strip()
    return public_key, private_key, subject


def send_push_to_user(user_id, title, body):
    public_key, private_key, subject = push_configuration()
    if not all((public_key, private_key, subject)):
        return

    from pywebpush import WebPushException, webpush

    conn = get_db()
    try:
        subscriptions = conn.execute(
            """SELECT id, subscription_json FROM push_subscriptions
               WHERE user_id = ?""",
            (user_id,),
        ).fetchall()
        expired_ids = []
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=json.loads(subscription["subscription_json"]),
                    data=json.dumps({"title": title, "body": body, "url": "/"}),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": subject},
                )
            except (WebPushException, ValueError, TypeError, OSError) as error:
                status_code = getattr(
                    getattr(error, "response", None), "status_code", None
                )
                if status_code in (404, 410):
                    expired_ids.append(subscription["id"])
                logger.warning(
                    "Could not send push notification to user %s (HTTP %s): %s",
                    user_id,
                    status_code or "unknown",
                    error,
                )
        if expired_ids:
            conn.executemany(
                "DELETE FROM push_subscriptions WHERE id = ?",
                [(subscription_id,) for subscription_id in expired_ids],
            )
            conn.commit()
    finally:
        conn.close()


def send_sms_to_user(user_id, body):
    if os.environ.get("SMS_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not all((account_sid, auth_token, from_number)):
        logger.warning("SMS is enabled but Twilio credentials are incomplete")
        return

    conn = get_db()
    user = conn.execute(
        """SELECT phone_number FROM users
           WHERE id = ? AND sms_notifications_enabled = 1""",
        (user_id,),
    ).fetchone()
    conn.close()
    if not user or not user["phone_number"]:
        return

    credentials = base64.b64encode(
        f"{account_sid}:{auth_token}".encode("utf-8")
    ).decode("ascii")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=urlencode(
            {"To": user["phone_number"], "From": from_number, "Body": body}
        ).encode("utf-8"),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                logger.warning(
                    "Twilio SMS request for user %s returned HTTP %s",
                    user_id,
                    response.status,
                )
    except HTTPError as error:
        logger.warning(
            "Twilio SMS request for user %s failed with HTTP %s",
            user_id,
            error.code,
        )
    except URLError as error:
        logger.warning(
            "Twilio SMS request for user %s could not connect: %s",
            user_id,
            error.reason,
        )
    except (TimeoutError, OSError, ValueError) as error:
        logger.warning(
            "Twilio SMS request for user %s failed: %s",
            user_id,
            error,
        )
