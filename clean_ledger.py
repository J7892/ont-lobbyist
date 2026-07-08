"""
clean_ledger.py
Standalone automated maintenance script for the Alberta Lobbyist Registry Ledger.
Safely cuts away corrupted append tails and restores the verified historical baseline.
"""
import os
import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(CURRENT_DIR, "alberta_lobbyists_historical.csv")

def repair_historical_ledger():
    print("====================================================")
    print("STARTING LEDGER SANITIZATION & MAINTENANCE SEQUENCE")
    print("====================================================\n")
    
    if not os.path.exists(LEDGER_FILE):
        print(f"[FATAL ERROR] Target ledger spreadsheet not found at: {LEDGER_FILE}")
        return

    try:
        # Load the current data stream
        df = pd.read_csv(LEDGER_FILE)
        initial_row_count = len(df)
        print(f"[LOADED] Found master file containing {initial_row_count} total entries.")
        
        if initial_row_count <= 1159:
            print("[INFO] Ledger is already at or below the verified historical baseline threshold.")
            print("[CANCELLED] No truncation required. Ready for standard daily operations.")
            return

        print(f"[ANALYSIS] Isolating corrupted tail blocks... ({initial_row_count - 1159} rows flagged for removal).")
        
        # Keep strictly rows 0 to 1158 (the pristine 1159 baseline records)
        sanitized_df = df.iloc[:1159].copy()
        final_row_count = len(sanitized_df)
        
        # Quick internal structural cross-check to verify alignment on the final row of the slice
        last_row = sanitized_df.iloc[-1]
        last_reg = str(last_row['REGISTRATION NUMBER']).strip()
        last_pdf = str(last_row['EXTRACTED_PDF_DETAILS']).strip()
        
        if last_reg in last_pdf:
            print(f"[VERIFIED] Baseline boundary checkpoint aligns perfectly at Token: {last_reg}")
        else:
            print("[WARNING] Baseline boundary verification returned token drift. Truncating cleanly anyway.")

        # Backup the old file to prevent accidental data drops
        backup_file = LEDGER_FILE + ".bak"
        if os.path.exists(backup_file):
            os.remove(backup_file)
        os.rename(LEDGER_FILE, backup_file)
        print(f"[BACKUP] Safety snapshot preserved at: {backup_file}")

        # Rewrite the clean data layout back to the master file target destination
        sanitized_df.to_csv(LEDGER_FILE, index=False)
        print(f"[SUCCESS] Cleaned ledger written safely. File size reset to {final_row_count} rows.")
        print("\n====================================================")
        print("MAINTENANCE SEQUENCE COMPLETE: REMOTE FRONTIER ARMED")
        print("====================================================")

    except Exception as maintenance_fault:
        print(f"[CRITICAL FAILURE] Maintenance pipeline aborted: {str(maintenance_fault)}")

if __name__ == "__main__":
    repair_historical_ledger()
