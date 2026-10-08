"""User authentication for bin number update app."""
import getpass
import logging
from db import get_connection

logger = logging.getLogger(__name__)


def detect_local_username() -> str:
    """
    Return the Windows username of whoever is running this app on their own laptop
    (this app is meant to run locally, one instance per person - not as a shared server).
    Returns "" if it can't be determined for any reason.
    """
    try:
        return (getpass.getuser() or "").strip()
    except Exception:
        return ""


def validate_lan_id(lan_id: str) -> tuple[bool, dict | None]:
    """
    Validate a LAN ID against T_LFUS_USER_SECURITY.US_USER_SECURITY_LAN_ID, case-insensitively
    (the stored LAN ID may be upper- or lower-case).
    Returns (True, {"lan_id": ..., "name_id": int|None}) on success, (False, None) otherwise.
    name_id is derived from US_USER_SECURITY_ID if it is a valid positive integer, else None.
    """
    clean = (lan_id or "").strip()
    if not clean:
        return False, None

    # get_connection() / cursor.execute() are allowed to raise here (e.g. DB unreachable,
    # thick-mode not configured) rather than being swallowed into a false "not found" -
    # the caller surfaces the real error instead of a misleading credentials message.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT US_USER_SECURITY_LAN_ID, US_USER_SECURITY_ID "
        "FROM T_LFUS_USER_SECURITY "
        "WHERE UPPER(TRIM(US_USER_SECURITY_LAN_ID)) = UPPER(:lid)",
        {"lid": clean},
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        logger.warning(f"[AUTH] LAN ID '{clean}' not found in T_LFUS_USER_SECURITY")
        return False, None

    name_id = None
    try:
        val = int(row[1])
        if val > 0:
            name_id = val
    except (TypeError, ValueError):
        pass

    logger.info(f"[AUTH] LAN ID '{clean}' authenticated, name_id={name_id}")
    return True, {"lan_id": str(row[0]).strip(), "name_id": name_id}
