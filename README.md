# Bin Number Updater

A Flask web application to bulk-update the bin number (`PO_BRK_ACC_NUM`) on policy records in Lifecad by uploading an Excel spreadsheet — built on the same stack and UI as the [Diary Note Bulk Updater](../lifecad-diary-updater).

## Features

- Upload `.xlsx` / `.xls` files with drag-and-drop
- **Contract number only**: matches rows by contract number (looked up in T_LIPO_POLICY: PO_CONT → PO_POL_NUM) — policy number upload is not supported
- **Validation**: shows contract numbers not found in T_LIPO_POLICY, and duplicate contract numbers in the file, before anything is updated
- Updates **T_LIPO_POLICY.PO_BRK_ACC_NUM** for the matched policy
- **Live progress**: a progress bar tracks the update as it runs, then shows how many rows were updated/failed with a button back to Home
- **Audit log**: Every update inserts one row into the database audit table (who, when, contract number, status). Run `create_audit_table.sql` once to create the table. View via **Audit log** in the nav.

## Setup

### 1. Prerequisites

- Python 3.10+
- **Oracle Instant Client** (required for this app: the LCUAT database uses a password verifier that only works in thick mode)
- Network access to the Lifecad Oracle database

#### Oracle Instant Client setup (Windows)

The database cannot use thin mode, so you must install Oracle Instant Client once:

1. **Download**
   [Oracle Instant Client Downloads](https://www.oracle.com/database/technologies/instant-client/winx64-64-downloads.html) → under "Basic" or "Basic Light", download the **64-bit** ZIP (e.g. "Instant Client Basic 64-bit - 21.x").

2. **Unzip**
   Extract to a folder such as `C:\oracle\instantclient_21_9` (no spaces in path is best).

3. **Configure the app**
   In your project `.env` file add (use your actual path):
   ```bash
   ORACLE_CLIENT_LIB_DIR=C:\oracle\instantclient_21_9
   ```

4. **Restart**
   Restart the Flask app and try logging in again.

### 2. Install Dependencies

```bash
cd bin-number-update
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
copy .env.example .env
```

Edit `.env`:

```
ORACLE_USER=dbo
ORACLE_PASSWORD=dbo
ORACLE_DSN=(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=...)(PORT=1721))(CONNECT_DATA=(SID=LCUAT)))
POLICY_TABLE=T_LIPO_POLICY
POLICY_POL_COLUMN=PO_POL_NUM
POLICY_CONT_COLUMN=PO_CONT
BIN_TABLE=T_LIPO_POLICY
BIN_COLUMN=PO_BRK_ACC_NUM
```

**Audit log:** Run `create_audit_table.sql` once in your Oracle database (e.g. as dbo on LCUAT) to create **T_AUDIT_BIN_LOG**. The **Audit log** link shows recent entries.

### 4. Run

```bash
python app.py
```

Open http://localhost:5001 in your browser (runs on a different port than the diary updater so both can run side by side).

## Usage

1. Log in with your LAN ID (pre-filled from your Windows login).
2. Upload an Excel file containing contract numbers and bin numbers.
3. Review the validation page: contract numbers not found in T_LIPO_POLICY and any duplicates are listed there; those rows are skipped.
4. Click **Proceed with valid rows** — a progress bar tracks the update live.
5. Once complete, the page shows how many rows were updated/failed. Click **Home** to return to the start.

## Excel File Format

Your Excel file should have columns like:

| Contract Number | Bin Number   |
|------------------|--------------|
| ABC1234567       | 987654321    |
| ABC7654321       | 123456789    |

Column headers are auto-detected (looking for "cont" and "bin"/"acct"/"account"); if neither header hints match, the first column is treated as the contract number and the second as the bin number.
