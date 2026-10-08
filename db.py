import logging
import os
import oracledb
from config import Config

logger = logging.getLogger(__name__)

# Use thick mode so Oracle Client handles password (verifier 0x939); thin mode does not support it.
# Try ORACLE_CLIENT_LIB_DIR, ORACLE_HOME, PATH dirs with oci.dll, then common Windows locations, then None (module dir + PATH).


def _instantclient_candidates():
    seen = set()
    # 1) Explicit env
    for env_var in ("ORACLE_CLIENT_LIB_DIR", "ORACLE_HOME"):
        p = (os.getenv(env_var) or "").strip()
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            yield p
        if env_var == "ORACLE_HOME":
            bin_p = os.path.join(p, "bin") if p else ""
            if bin_p and os.path.isdir(bin_p) and bin_p not in seen:
                seen.add(bin_p)
                yield bin_p
    # 2) PATH entries that contain oci.dll (Instant Client on PATH)
    path_str = os.environ.get("PATH", "")
    for part in path_str.split(os.pathsep):
        part = part.strip()
        if not part or part in seen:
            continue
        try:
            if part and os.path.isdir(part) and any(
                f.lower() == "oci.dll" for f in os.listdir(part)
            ):
                seen.add(part)
                yield part
        except Exception:
            pass
    # 3) Common Windows locations
    for _base in ["C:\\oracle", "C:\\instantclient", os.path.expandvars("%LOCALAPPDATA%")]:
        if not _base or not os.path.isdir(_base):
            continue
        try:
            for _name in os.listdir(_base):
                if _name.lower().startswith("instantclient"):
                    _path = os.path.join(_base, _name)
                    if (
                        os.path.isdir(_path)
                        and _path not in seen
                        and any(f.lower().endswith(".dll") for f in os.listdir(_path))
                    ):
                        seen.add(_path)
                        yield _path
                    break
        except Exception:
            pass
    # 4) Let oracledb search (module dir + PATH)
    yield None


for _lib_dir in _instantclient_candidates():
    try:
        oracledb.init_oracle_client(lib_dir=_lib_dir)
        break
    except Exception:
        continue


def _str(val):
    """Ensure value is a plain Python str for Oracle connect (username/password as string)."""
    if val is None:
        return ""
    return str(val).strip() if not isinstance(val, str) else val.strip()


_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        dsn = _str(Config.ORACLE_DSN)
        user = _str(Config.ORACLE_USER)
        password = _str(Config.ORACLE_PASSWORD)
        _pool = oracledb.create_pool(
            user=user,
            password=password,
            dsn=dsn,
            min=1,
            max=5,
            increment=1,
        )
    return _pool


def get_connection():
    try:
        return _get_pool().acquire()
    except Exception:
        # Fall back to direct connect if pool fails (e.g. credentials not yet set)
        dsn = _str(Config.ORACLE_DSN)
        user = _str(Config.ORACLE_USER)
        password = _str(Config.ORACLE_PASSWORD)
        return oracledb.connect(user=user, password=password, dsn=dsn)


def get_pol_nums_from_contracts(contract_nums: list[str]) -> tuple[dict[str, str], set[str]]:
    """
    Look up PO_POL_NUM from T_LIPO_POLICY by PO_CONT (contract number).
    Returns (contract_num -> pol_num mapping, set of contract numbers not found).
    """
    table = getattr(Config, "POLICY_TABLE", None) or "T_LIPO_POLICY"
    col_cont = getattr(Config, "POLICY_CONT_COLUMN", None) or "PO_CONT"
    col_pol = getattr(Config, "POLICY_POL_COLUMN", None) or "PO_POL_NUM"
    if not contract_nums:
        return {}, set()

    unique = list(dict.fromkeys(str(c).strip() for c in contract_nums if c))
    if not unique:
        return {}, set()

    conn = get_connection()
    cursor = conn.cursor()
    mapping = {}
    batch_size = 500
    for i in range(0, len(unique), batch_size):
        batch = unique[i : i + batch_size]
        placeholders = ", ".join(f":b{j}" for j in range(len(batch)))
        sql = f"SELECT {col_cont}, {col_pol} FROM {table} WHERE {col_cont} IN ({placeholders})"
        params = {f"b{j}": v for j, v in enumerate(batch)}
        cursor.execute(sql, params)
        for row in cursor:
            c, p = row[0], row[1]
            if c is not None:
                mapping[str(c).strip()] = str(p).strip() if p is not None else ""
    cursor.close()
    conn.close()
    missing = set(unique) - set(mapping.keys())
    return mapping, missing


def update_bin_numbers(rows: list[dict], progress_callback=None) -> dict:
    """
    Update the bin number column (Config.BIN_COLUMN) on Config.BIN_TABLE for each row,
    matched by policy number (Config.POLICY_POL_COLUMN) resolved from the uploaded contract number.

    Args:
        rows: List of dicts with pol_num, bin_num
        progress_callback: optional callable(processed, success_count, failed_count) invoked after each batch
    """
    table = getattr(Config, "BIN_TABLE", None) or "T_LIPO_POLICY"
    bin_col = getattr(Config, "BIN_COLUMN", None) or "PO_BRK_ACC_NUM"
    pol_col = getattr(Config, "POLICY_POL_COLUMN", None) or "PO_POL_NUM"

    sql = f"UPDATE {table} SET {bin_col} = :bin_num WHERE {pol_col} = :pol_num"

    logger.info(f"Updating {len(rows)} bin numbers...")

    batch_data = [{"pol_num": row["pol_num"], "bin_num": row["bin_num"]} for row in rows]

    success_count = 0
    error_rows = []

    conn = get_connection()
    cursor = conn.cursor()

    batch_size = 50
    total = len(rows)

    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = batch_data[batch_start:batch_end]

        try:
            cursor.executemany(sql, batch)
            success_count += len(batch)
            logger.info(f"  Updated {batch_end}/{total} rows ({100*batch_end//total}%)")
        except Exception:
            # If batch fails, try one-by-one to identify failing rows
            for i, params in enumerate(batch):
                row_idx = batch_start + i + 1
                try:
                    cursor.execute(sql, params)
                    success_count += 1
                except Exception as row_err:
                    error_rows.append({"row": row_idx, "pol_num": params.get("pol_num"), "error": str(row_err)})
                    logger.warning(f"  Row {row_idx} failed: {row_err}")
            logger.info(f"  Processed {batch_end}/{total} rows ({100*batch_end//total}%)")

        if progress_callback:
            progress_callback(batch_end, success_count, len(error_rows))

    logger.info("Committing transaction...")
    conn.commit()
    cursor.close()
    conn.close()
    logger.info(f"Update complete: {success_count} success, {len(error_rows)} failed")

    return {
        "total": len(rows),
        "success": success_count,
        "failed": len(error_rows),
        "errors": error_rows,
    }


def insert_audit_log_entries(created_by: str, entries: list[dict]) -> None:
    """Insert audit rows using a single connection for efficiency."""
    if not entries:
        return
    table = getattr(Config, "AUDIT_TABLE", None) or "T_AUDIT_BIN_LOG"
    seq = getattr(Config, "AUDIT_SEQUENCE", None) or "T_AUDIT_BIN_LOG_SEQ"
    sql = (
        f"INSERT INTO {table} (AUDIT_ID, AUDIT_WHEN, AUDIT_WHO, IDENTIFIER, AUDIT_ACTION, STATUS, ERROR_MSG) "
        f"VALUES ({seq}.NEXTVAL, SYSTIMESTAMP, :who, :identifier, 'bin_number_update', :status, :error_msg)"
    )
    logger.info(f"Inserting {len(entries)} audit log entries...")
    batch_data = [
        {
            "who": (created_by or "unknown")[:100],
            "identifier": (e.get("identifier", "") or "")[:50],
            "status": (e.get("status", "unknown") or "unknown")[:30],
            "error_msg": (e.get("error_msg") or "")[:4000] if e.get("error_msg") else None,
        }
        for e in entries
    ]
    try:
        conn = get_connection()
        cursor = conn.cursor()
        batch_size = 500
        total = len(batch_data)
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            cursor.executemany(sql, batch_data[batch_start:batch_end])
            logger.info(f"  Audit: {batch_end}/{total} rows")
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Audit log insert complete: {total} rows")
    except Exception as ex:
        logger.error(f"Audit log insert failed: {ex}")


def get_audit_log_entries(
    page: int = 1, page_size: int = 50, contract: str | None = None
) -> tuple[list[dict], int]:
    """
    Return (rows for this page, total matching row count) from the audit log, newest first.
    If contract is given, filters IDENTIFIER by a case-insensitive substring match.
    """
    table = getattr(Config, "AUDIT_TABLE", None) or "T_AUDIT_BIN_LOG"
    contract = (contract or "").strip()
    where_clause = "WHERE UPPER(IDENTIFIER) LIKE UPPER(:contract)" if contract else ""
    params = {"contract": f"%{contract}%"} if contract else {}

    count_sql = f"SELECT COUNT(*) FROM {table} {where_clause}"
    page_sql = (
        f"SELECT AUDIT_ID, AUDIT_WHEN, AUDIT_WHO, IDENTIFIER, AUDIT_ACTION, STATUS, ERROR_MSG "
        f"FROM {table} {where_clause} ORDER BY AUDIT_ID DESC "
        "OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY"
    )
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(count_sql, params)
        total = cursor.fetchone()[0]

        page_params = dict(params, offset=max(page - 1, 0) * page_size, page_size=page_size)
        cursor.execute(page_sql, page_params)
        columns = [c[0] for c in cursor.description]
        rows = []
        for row in cursor:
            d = dict(zip(columns, row))
            for k in ("AUDIT_WHEN",):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            rows.append(d)
        cursor.close()
        conn.close()
        return rows, total
    except Exception:
        return [], 0
