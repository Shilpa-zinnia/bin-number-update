import os
from dotenv import load_dotenv

# Load .env: from project dir (config.py's directory) and from cwd so it works wherever you run from
_project_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(os.getcwd(), ".env"))
load_dotenv(os.path.join(_project_dir, ".env"), override=True)


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-prod")
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB upload limit
    ALLOWED_EXTENSIONS = {"xlsx", "xls"}

    # Oracle connection: ORACLE_USER or ORACLE_USERNAME; DSN from env or build from HOST/PORT/SID
    ORACLE_USER = (os.getenv("ORACLE_USER") or os.getenv("ORACLE_USERNAME") or "").strip()
    ORACLE_PASSWORD = (os.getenv("ORACLE_PASSWORD") or "").strip()
    _host = os.getenv("ORACLE_HOST", "").strip()
    _port = (os.getenv("ORACLE_PORT") or "1721").strip()
    _sid = os.getenv("ORACLE_SID", "").strip()
    _dsn = (os.getenv("ORACLE_DSN") or "").strip()
    # Ignore placeholder DSN so it doesn't trigger tnsnames.ora lookup
    if _dsn.lower() in ("hostname:port/service_name", "hostname:port/servicename", ""):
        _dsn = ""
    if _host and _sid:
        # Easy-connect: avoids tnsnames.ora (use this for LCUAT)
        ORACLE_DSN = f"{_host}:{_port}:{_sid}"
    elif _dsn.startswith("("):
        ORACLE_DSN = _dsn
    else:
        ORACLE_DSN = _dsn if _dsn else ""

    # Policy table: T_LIPO_POLICY (PO_POL_NUM, PO_CONT, PO_BRK_ACC_NUM)
    POLICY_TABLE = os.getenv("POLICY_TABLE", "T_LIPO_POLICY")
    POLICY_POL_COLUMN = os.getenv("POLICY_POL_COLUMN", "PO_POL_NUM")
    POLICY_CONT_COLUMN = os.getenv("POLICY_CONT_COLUMN", "PO_CONT")

    # Bin number column to update (defaults to the same table as POLICY_TABLE)
    BIN_TABLE = os.getenv("BIN_TABLE", POLICY_TABLE)
    BIN_COLUMN = os.getenv("BIN_COLUMN", "PO_BRK_ACC_NUM")

    # Audit log table: who made the change and when (run create_audit_table.sql once to create it)
    AUDIT_TABLE = os.getenv("AUDIT_TABLE", "T_AUDIT_BIN_LOG")
    AUDIT_SEQUENCE = os.getenv("AUDIT_SEQUENCE", "T_AUDIT_BIN_LOG_SEQ")
