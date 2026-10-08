-- Run this once in your Oracle database (e.g. LCUAT) to create the audit log table.
-- One audit row is inserted for every contract/policy number processed (who, when, status).

CREATE SEQUENCE T_AUDIT_BIN_LOG_SEQ START WITH 1 INCREMENT BY 1 NOCACHE;

CREATE TABLE T_AUDIT_BIN_LOG (
    AUDIT_ID       NUMBER PRIMARY KEY,
    AUDIT_WHEN     TIMESTAMP DEFAULT SYSTIMESTAMP,
    AUDIT_WHO      VARCHAR2(100) NOT NULL,
    IDENTIFIER     VARCHAR2(50) NOT NULL,
    AUDIT_ACTION   VARCHAR2(50) DEFAULT 'bin_number_update',
    STATUS         VARCHAR2(30) NOT NULL,
    ERROR_MSG      VARCHAR2(4000)
);

CREATE OR REPLACE TRIGGER T_AUDIT_BIN_LOG_BI
BEFORE INSERT ON T_AUDIT_BIN_LOG
FOR EACH ROW
BEGIN
  IF :NEW.AUDIT_ID IS NULL THEN
    SELECT T_AUDIT_BIN_LOG_SEQ.NEXTVAL INTO :NEW.AUDIT_ID FROM DUAL;
  END IF;
END;
/

COMMENT ON TABLE T_AUDIT_BIN_LOG IS 'One row per contract/policy: who made the change, when, and status (updated/failed/skipped_not_in_db/skipped_duplicate).';
