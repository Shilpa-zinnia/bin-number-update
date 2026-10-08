import logging
import os
import re
import threading
import uuid
from collections import Counter
from functools import wraps
from io import BytesIO

import pandas as pd
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from auth import detect_local_username, validate_lan_id
from config import Config
from db import (
    get_audit_log_entries,
    get_pol_nums_from_contracts,
    insert_audit_log_entries,
    update_bin_numbers,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s in %(name)s: %(message)s")

app = Flask(__name__)
app.config.from_object(Config)

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def current_user_display() -> str:
    return session.get("user_display", "system")


# preview_id -> {"rows": [{"contract_num", "bin_num", "pol_num"}], "missing": [...], "duplicates": [...]}
PREVIEWS: dict[str, dict] = {}
# job_id -> {"total", "processed", "success", "failed", "errors", "status"}
PROGRESS: dict[str, dict] = {}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    local_username = detect_local_username()
    if request.method == "POST":
        lan_id = (request.form.get("lan_id") or "").strip()
        if not lan_id:
            flash("LAN ID is required.", "error")
            return render_template("login.html", lan_id=lan_id)
        if local_username and lan_id.lower() != local_username.lower():
            flash(
                f"Entered LAN ID '{lan_id}' doesn't match this laptop's Windows login "
                f"('{local_username}'). Please use your own LAN ID.",
                "error",
            )
            return render_template("login.html", lan_id=local_username)
        try:
            ok, user_info = validate_lan_id(lan_id)
        except Exception as e:
            flash(f"Could not verify LAN ID (database error): {e}", "error")
            return render_template("login.html", lan_id=lan_id)
        if not ok:
            flash("LAN ID not found. Please check your credentials.", "error")
            return render_template("login.html", lan_id=lan_id)
        session["logged_in"] = True
        session["lan_id"] = user_info["lan_id"]
        session["user_display"] = user_info["lan_id"]
        session["name_id"] = user_info["name_id"]
        return redirect(url_for("index"))
    return render_template("login.html", lan_id=local_username)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


def _detect_columns(df: pd.DataFrame) -> dict:
    """
    A sheet has exactly two relevant columns: Contract Number and Bin Number.
    Header names are checked for hints ("cont" vs "bin"/"acct"/"account"/"brk_acc");
    falls back to column order when headers aren't descriptive.
    """
    columns = list(df.columns)
    contract_col = next((c for c in columns if "cont" in c.lower()), None)
    bin_col = next(
        (c for c in columns if any(k in c.lower() for k in ("bin", "brk_acc", "acct", "account"))), None
    )

    if contract_col is None:
        remaining = [c for c in columns if c != bin_col]
        contract_col = remaining[0] if remaining else columns[0]

    if bin_col is None or bin_col == contract_col:
        remaining = [c for c in columns if c != contract_col]
        bin_col = remaining[0] if remaining else columns[-1]

    return {"col_contract": contract_col, "col_bin": bin_col}


def _build_rows(df, col_contract, col_bin):
    """Build list of {"contract_num", "bin_num"} dicts, skipping rows with either value empty."""
    rows = []
    for _, r in df.iterrows():
        contract_num = str(r[col_contract]).strip() if pd.notna(r[col_contract]) else ""
        bin_num = str(r[col_bin]).strip() if pd.notna(r[col_bin]) else ""
        if not contract_num or not bin_num:
            continue
        rows.append({"contract_num": contract_num, "bin_num": bin_num})
    return rows


_BIN_RE = re.compile(r"^\d+$")
_CONTRACT_RE = re.compile(r"^[A-Za-z0-9]+$")


def _find_unusual_rows(rows: list[dict]) -> list[dict]:
    """
    Flag rows whose Bin Number isn't purely numeric, or whose Contract Number contains
    special characters (letters are fine for contract numbers, digits-only for bin numbers).
    """
    flagged = []
    for i, r in enumerate(rows):
        reasons = []
        if not _BIN_RE.match(r["bin_num"]):
            reasons.append("Bin Number contains letters or special characters")
        if not _CONTRACT_RE.match(r["contract_num"]):
            reasons.append("Contract Number contains special characters")
        if reasons:
            flagged.append({
                "index": i,
                "contract_num": r["contract_num"],
                "bin_num": r["bin_num"],
                "reason": "; ".join(reasons),
            })
    return flagged


def _validate_and_render(preview_id: str):
    """Run duplicate detection + Lifecad contract lookup on preview["rows"], then render validation.html."""
    preview = PREVIEWS[preview_id]
    rows = preview["rows"]

    contract_counts = Counter(r["contract_num"] for r in rows)
    duplicates = sorted([c for c, count in contract_counts.items() if count > 1])

    try:
        mapping, missing = get_pol_nums_from_contracts([r["contract_num"] for r in rows])
    except Exception as e:
        flash(f"Contract lookup failed: {e}", "error")
        return redirect(url_for("index"))

    for r in rows:
        r["pol_num"] = mapping.get(r["contract_num"], "")

    preview["missing"] = sorted(missing)
    preview["duplicates"] = duplicates

    return render_template(
        "validation.html",
        preview_id=preview_id,
        total_rows=len(rows),
        missing_contracts=preview["missing"],
        duplicate_contracts=duplicates,
    )


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    if "file" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    file = request.files["file"]
    if file.filename == "" or not allowed_file(file.filename):
        flash("Please upload a valid Excel file (.xlsx or .xls).", "error")
        return redirect(url_for("index"))

    filename = f"{uuid.uuid4().hex}_{file.filename}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    try:
        df = pd.read_excel(filepath, dtype=str)
    except Exception as e:
        flash(f"Failed to read Excel file: {e}", "error")
        return redirect(url_for("index"))
    finally:
        os.remove(filepath)

    if df.empty:
        flash("The uploaded file has no data rows.", "error")
        return redirect(url_for("index"))

    detected = _detect_columns(df)
    if detected["col_contract"] not in df.columns or detected["col_bin"] not in df.columns:
        flash("Could not find a Contract Number and Bin Number column in the file.", "error")
        return redirect(url_for("index"))

    rows = _build_rows(df, detected["col_contract"], detected["col_bin"])
    if not rows:
        flash("No valid rows found (Contract Number and Bin Number are both required).", "error")
        return redirect(url_for("index"))

    preview_id = uuid.uuid4().hex
    PREVIEWS[preview_id] = {"rows": rows}

    app.logger.info(
        f"[DETECT] columns={list(df.columns)} → contract='{detected['col_contract']}' bin='{detected['col_bin']}' "
        f"total={len(rows)}"
    )

    flagged = _find_unusual_rows(rows)
    if flagged:
        PREVIEWS[preview_id]["flagged"] = flagged
        return render_template(
            "review_unusual.html", preview_id=preview_id, flagged=flagged, total_flagged=len(flagged)
        )

    return _validate_and_render(preview_id)


@app.route("/review-unusual", methods=["POST"])
@login_required
def review_unusual():
    preview_id = request.form.get("preview_id")
    decision = request.form.get("decision")

    preview = PREVIEWS.get(preview_id)
    if not preview:
        flash("Session expired. Please upload the file again.", "error")
        return redirect(url_for("index"))

    if decision == "yes":
        return _validate_and_render(preview_id)

    return render_template("fix_unusual.html", preview_id=preview_id, flagged=preview.get("flagged", []))


@app.route("/apply-fixes", methods=["POST"])
@login_required
def apply_fixes():
    preview_id = request.form.get("preview_id")
    preview = PREVIEWS.get(preview_id)
    if not preview:
        flash("Session expired. Please upload the file again.", "error")
        return redirect(url_for("index"))

    rows = preview["rows"]
    for f in preview.get("flagged", []):
        idx = f["index"]
        new_contract = (request.form.get(f"contract_{idx}") or "").strip()
        new_bin = (request.form.get(f"bin_{idx}") or "").strip()
        if new_contract:
            rows[idx]["contract_num"] = new_contract
        if new_bin:
            rows[idx]["bin_num"] = new_bin

    return _validate_and_render(preview_id)


@app.route("/download-missing/<preview_id>")
@login_required
def download_missing(preview_id):
    preview = PREVIEWS.get(preview_id)
    if not preview:
        flash("Session expired. Please upload the file again.", "error")
        return redirect(url_for("index"))

    df = pd.DataFrame({"Contract Number": preview["missing"]})
    buffer = BytesIO()
    df.to_excel(buffer, index=False, sheet_name="Missing Contracts")
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="missing_contracts.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _run_update_job(job_id: str, rows: list[dict], created_by: str, missing: list[str], duplicates: list[str]):
    def on_progress(processed, success, failed):
        PROGRESS[job_id].update({"processed": processed, "success": success, "failed": failed})

    try:
        result = update_bin_numbers(rows, progress_callback=on_progress)

        failed_pol_nums = {e["pol_num"] for e in result["errors"]}
        updated_rows = [
            {"contract_num": row["contract_num"], "bin_num": row["bin_num"]}
            for row in rows
            if row["pol_num"] not in failed_pol_nums
        ]
        PROGRESS[job_id].update({
            "processed": result["total"],
            "success": result["success"],
            "failed": result["failed"],
            "status": "done",
            "updated_rows": updated_rows,
        })

        error_by_pol = {e["pol_num"]: e["error"] for e in result["errors"]}
        audit_entries = []
        for row in rows:
            if row["pol_num"] in failed_pol_nums:
                audit_entries.append({
                    "identifier": row["contract_num"],
                    "status": "failed",
                    "error_msg": error_by_pol.get(row["pol_num"]),
                })
            else:
                audit_entries.append({"identifier": row["contract_num"], "status": "updated"})
        for c in missing:
            audit_entries.append({"identifier": c, "status": "skipped_not_in_db"})
        for c in duplicates:
            audit_entries.append({"identifier": c, "status": "skipped_duplicate"})
        insert_audit_log_entries(created_by, audit_entries)
    except Exception as e:
        PROGRESS[job_id].update({"status": "error", "error_msg": str(e)})


@app.route("/confirm", methods=["POST"])
@login_required
def confirm():
    created_by = current_user_display()
    preview_id = request.form.get("preview_id")

    preview = PREVIEWS.get(preview_id)
    if not preview:
        flash("Session expired. Please upload the file again.", "error")
        return redirect(url_for("index"))

    missing_set = set(preview["missing"])
    duplicate_set = set(preview["duplicates"])

    seen = set()
    rows = []
    for r in preview["rows"]:
        c = r["contract_num"]
        if c in missing_set or c in duplicate_set or not r.get("pol_num") or c in seen:
            continue
        seen.add(c)
        rows.append(r)

    PREVIEWS.pop(preview_id, None)

    if not rows:
        flash("No rows left after excluding contracts not in Lifecad and duplicates.", "error")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex
    PROGRESS[job_id] = {"total": len(rows), "processed": 0, "success": 0, "failed": 0, "status": "running"}

    thread = threading.Thread(
        target=_run_update_job,
        args=(job_id, rows, created_by, preview["missing"], preview["duplicates"]),
        daemon=True,
    )
    thread.start()

    return render_template("progress.html", job_id=job_id, total=len(rows))


@app.route("/update-progress/<job_id>")
def update_progress(job_id):
    if not session.get("logged_in"):
        return jsonify({"error": "unauthorized"}), 401
    progress = PROGRESS.get(job_id)
    if not progress:
        return jsonify({"error": "not found"}), 404

    # Keep the poll payload light - the full updated_rows list can be thousands of rows;
    # only send a small sample here, the full list is served separately for download.
    updated_rows = progress.get("updated_rows", [])
    payload = {k: v for k, v in progress.items() if k != "updated_rows"}
    if progress.get("status") == "done":
        payload["sample"] = updated_rows[:5]
        payload["updated_count"] = len(updated_rows)
    return jsonify(payload)


@app.route("/download-updated/<job_id>")
@login_required
def download_updated(job_id):
    progress = PROGRESS.get(job_id)
    updated_rows = (progress or {}).get("updated_rows")
    if not updated_rows:
        flash("No updated contracts available to download for this job.", "error")
        return redirect(url_for("index"))

    df = pd.DataFrame(updated_rows).rename(
        columns={"contract_num": "Contract Number", "bin_num": "Bin Number"}
    )
    buffer = BytesIO()
    df.to_excel(buffer, index=False, sheet_name="Updated Contracts")
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="updated_contracts.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/audit")
@login_required
def audit_log():
    search = (request.args.get("contract") or "").strip()
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    page_size = 20

    entries, total = get_audit_log_entries(page=page, page_size=page_size, contract=search)
    total_pages = max((total + page_size - 1) // page_size, 1)
    if page > total_pages:
        page = total_pages
        entries, total = get_audit_log_entries(page=page, page_size=page_size, contract=search)

    return render_template(
        "audit_log.html",
        entries=entries,
        search=search,
        page=page,
        total_pages=total_pages,
        total=total,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001, use_reloader=False, threaded=True)
