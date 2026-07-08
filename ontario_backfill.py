import os
import time
import signal
import re
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORICAL_DATA_FILE = os.path.join(CURRENT_DIR, "ontario_lobbyists_historical.csv")
BASE_URL = "https://lobbyist.oico.on.ca/Pages/Public/PublicSearch/"

class TimeoutException(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutException("Detail parsing took too long")

def reformat_date(date_str):
    if not date_str or date_str == "-":
        return "-"
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            m, d, y = parts
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            month_str = months[int(m) - 1]
            return f"{int(d):02d}-{month_str}-{y}"
    except Exception:
        pass
    return date_str

def is_before_2025(date_str):
    if not date_str or date_str == "-":
        return False
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            year = int(parts[2])
            if year < 2025:
                return True
    except Exception:
        pass
    return False

def parse_detail_html(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Get clean page text for EXTRACTED_PDF_DETAILS
    form = soup.find("form")
    form_text = ""
    if form:
        form_text = form.get_text(separator=" ").replace("\n", " ").replace("\t", " ")
        form_text = " ".join(form_text.split())
        
    filer = ""
    departments = []
    entities = []
    subjects = []
    lobbyists = []
    termination_date = "-"
    
    # 1. Designated Filer / Senior Officer (lblFirstNameValue and lblLastNameValue)
    first_name_el = soup.find(id=lambda val: val and "lblFirstNameValue" in val)
    last_name_el = soup.find(id=lambda val: val and "lblLastNameValue" in val)
    if first_name_el and last_name_el:
        filer = f"{first_name_el.text.strip()} {last_name_el.text.strip()}"
        
    # 2. Subject Matter
    subj_el = soup.find(id=lambda val: val and "lblSubjectMatter" in val and "OLSubjectMatters" in val)
    if not subj_el:
        subj_el = soup.find(id=lambda val: val and "lblSubjectMatter" in val)
    if subj_el:
        subjects = [s.strip() for s in subj_el.text.split(";") if s.strip()]
        
    # 3. Lobbyists List
    emp_tables = soup.find_all("table", id=lambda val: val and "grdEmployeeNameAndTitle" in val)
    if emp_tables:
        for t in emp_tables:
            rows = t.find_all("tr")[1:]
            for r in rows:
                cells = r.find_all("td")
                if cells:
                    name = cells[0].text.strip()
                    if name and name not in lobbyists and "please click" not in name.lower() and "no records" not in name.lower():
                        lobbyists.append(name)
    else:
        if filer:
            lobbyists.append(filer)
            
    other_emp_tables = soup.find_all("table", id=lambda val: val and "grdEmployeeNameDescription" in val)
    if other_emp_tables:
        for t in other_emp_tables:
            rows = t.find_all("tr")[1:]
            for r in rows:
                cells = r.find_all("td")
                if cells:
                    name = cells[0].text.strip()
                    if name and name not in lobbyists and "no records" not in name.lower():
                        lobbyists.append(name)
                        
    # 4. Lobbying Targets
    min_tables = soup.find_all("table", id=lambda val: val and "tblMinistersOfficesAndMinistries" in val)
    for t in min_tables:
        for cell in t.find_all("td"):
            lines = [line.strip() for line in cell.get_text().split("\n") if line.strip()]
            for line in lines:
                if "Lobbying Targets:" not in line and "Ministers' Offices" not in line and "Ministries" not in line and line != "-":
                    if line not in departments:
                        departments.append(line)
                        
    mpp_tables = soup.find_all("table", id=lambda val: val and "tblMPPAndAgencies" in val)
    for t in mpp_tables:
        for cell in t.find_all("td"):
            lines = [line.strip() for line in cell.get_text().split("\n") if line.strip()]
            for line in lines:
                if "Members of Provincial Parliament" not in line and "Agencies" not in line and line != "-":
                    if line not in entities:
                        entities.append(line)
                        
    # 5. Termination Date
    term_pattern = re.search(r'(?:Ceased|Termination|Cease Date|Ceased Date|Date Ceased|Date the registration ceased)\s*:\s*([0-9]{2}-[0-9]{2}-[0-9]{4})', form_text, re.IGNORECASE)
    if term_pattern:
        termination_date = reformat_date(term_pattern.group(1))
        
    return {
        "FILER": filer,
        "SUBJECTS": "; ".join(subjects),
        "LOBBYISTS": ", ".join(lobbyists),
        "DEPARTMENTS": ", ".join(departments),
        "ENTITIES": ", ".join(entities),
        "TERMINATION_DATE": termination_date,
        "FULL_TEXT": form_text
    }

def get_row_data_matrix(page):
    """Extracts rows from Telerik RadGrid table."""
    try:
        return page.evaluate("""() => {
            const table = document.querySelector('table[id*=GridRegistrationList_ctl00]');
            if (!table) return null;
            const rows = Array.from(table.querySelectorAll('tr'));
            return rows.map(tr => 
                Array.from(tr.querySelectorAll('td, th')).map(c => (c.innerText || '').trim())
            ).filter(row => row.length > 0);
        }""")
    except Exception:
        return None

def backfill_historical_registry():
    print("Initializing Ontario skip-scanning backfill pipeline...")
    
    # Configure Unix SIGALRM for timeout safety
    signal.signal(signal.SIGALRM, timeout_handler)
    
    existing_tokens = set()
    if os.path.exists(HISTORICAL_DATA_FILE) and os.path.getsize(HISTORICAL_DATA_FILE) > 0:
        try:
            existing_df = pd.read_csv(HISTORICAL_DATA_FILE)
            if "REGISTRATION NUMBER" in existing_df.columns:
                existing_tokens = set(existing_df["REGISTRATION NUMBER"].astype(str).tolist())
            print(f"[RESUME] Loaded {len(existing_tokens)} unique keys from historical ledger.")
        except Exception as e:
            print(f"Note: Could not parse existing ledger ({str(e)}). Starting fresh.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            viewport={'width': 1440, 'height': 900}
        )
        page = context.new_page()
        
        global_headers = [
            "FILING DATE", "TERMINATION DATE", "ORGANIZATION", "CLIENT NAME", "DESIGNATED FILER",
            "GOVERNMENT DEPARTMENT LOBBIED", "PRESCRIBED PROVINCIAL ENTITY LOBBIED", "SUBJECT MATTER OF LOBBYING",
            "REGISTRATION NUMBER", "TYPE OF LOBBYIST", "LOBBYISTS", "TYPE OF REGISTRATION",
            "REGISTRATION STATUS", "EXTRACTED_PDF_DETAILS"
        ]
        
        page_number = 1
        fresh_pages_processed = 0
        MAX_FRESH_PAGES_PER_RUN = 1000 # Crawl up to 1000 pages
        
        try:
            print(f"Navigating to registry endpoint: {BASE_URL}")
            page.goto(BASE_URL, wait_until="networkidle")
            
            print("Clicking Search to retrieve active records...")
            page.click("#BodyContent_ucQuickSearch_btnSearch", no_wait_after=True)
            page.wait_for_url("**/SearchResults.aspx")
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            
            while True:
                matrix = get_row_data_matrix(page)
                if not matrix or len(matrix) < 2:
                    print("No search result rows found. Exiting pagination loop.")
                    break
                
                # Active page info
                try:
                    current_pagination_state = page.locator("a.rgCurrentPage").text_content().strip()
                except Exception:
                    current_pagination_state = str(page_number)
                print(f"\n--- SCANNING ONTARIO GRID: PAGE {page_number} (Active Index: {current_pagination_state}) ---")
                
                valid_rows_to_process = []
                contains_new_records = False
                
                # Iterate through grid rows. Data rows have IDs like ctl00_..._ctl00__X
                # Let's query them from DOM to match Row indices.
                row_locators = page.locator("tr[id*=GridRegistrationList_ctl00__]")
                row_count = row_locators.count()
                
                for r_idx in range(row_count):
                    row_id = row_locators.nth(r_idx).get_attribute("id")
                    # Suffix represents data row index
                    suffix = row_id.split("__")[-1]
                    
                    # Extract text values of cells from DOM
                    cells = row_locators.nth(r_idx).locator("td")
                    cell_texts = [cells.nth(c).text_content().strip() for c in range(cells.count())]
                    
                    if len(cell_texts) >= 8:
                        lobbyist = cell_texts[0]
                        amendment_date = reformat_date(cell_texts[1])
                        client_name = cell_texts[2] or "-"
                        org_name = cell_texts[3] or "-"
                        lobbyist_type = cell_texts[4]
                        reg_token = cell_texts[5] # Registration No.
                        doc_type = cell_texts[6] or "-"
                        status = cell_texts[7] # Status
                        
                        if reg_token:
                            valid_rows_to_process.append({
                                "index": suffix,
                                "lobbyist": lobbyist,
                                "filing_date": amendment_date,
                                "client_name": client_name,
                                "organization": org_name,
                                "lobbyist_type": lobbyist_type,
                                "reg_token": reg_token,
                                "doc_type": doc_type,
                                "status": status
                            })
                            if reg_token not in existing_tokens:
                                contains_new_records = True
                                
                if not valid_rows_to_process:
                    print(f" >> Page {page_number} contains no valid rows.")
                elif not contains_new_records:
                    print(f" >> [FAST-FORWARD] All {len(valid_rows_to_process)} records on Page {page_number} already cached.")
                else:
                    print(f" Isolated {len(valid_rows_to_process)} records on page {page_number}. Syncing unindexed targets...")
                    page_records = []
                    reached_cutoff = False
                    
                    for row_info in valid_rows_to_process:
                        reg_token = row_info["reg_token"]
                        if is_before_2025(row_info["filing_date"]):
                            print(f" >> [CUTOFF] Reached record older than 2025: {reg_token} ({row_info['filing_date']}). Halting backfill.")
                            reached_cutoff = True
                            break
                        if reg_token in existing_tokens:
                            print(f"  -> [{reg_token}] already cached in ledger.")
                            continue
                            
                        print(f"  -> Extracting details for registration: {reg_token}")
                        
                        # Click the row and wait for detail content to load
                        row_selector = f"tr#ctl00_BodyContent_ucSearchResults_gridSearchResults_GridRegistrationList_ctl00__{row_info['index']}"
                        
                        detail_success = False
                        detail_data = {}
                        
                        try:
                            page.click(row_selector, no_wait_after=True)
                            
                            # Wait for detail to render
                            page.locator("#BodyContent_ucRegistrationSubmit_btnBackToResultsTop").wait_for(timeout=15000)
                            
                            signal.alarm(10) # 10s alarm timeout
                            try:
                                html_content = page.content()
                                detail_data = parse_detail_html(html_content)
                                detail_success = True
                            except TimeoutException:
                                print(f"      * Detail parsing timed out for {reg_token}")
                            finally:
                                signal.alarm(0)
                                
                        except Exception as click_err:
                            print(f"      * Could not navigate to details for {reg_token}: {str(click_err)}")
                            
                        # Navigate back to results grid
                        try:
                            page.click("#BodyContent_ucRegistrationSubmit_btnBackToResultsTop", no_wait_after=True)
                            page.locator("tr[id*=GridRegistrationList]").first.wait_for(timeout=15000)
                            time.sleep(2)
                        except Exception as back_err:
                            print(f"      * Error returning to search results: {str(back_err)}")
                            # Force reload/search if navigation gets stuck
                            page.goto(BASE_URL, wait_until="networkidle")
                            page.click("#BodyContent_ucQuickSearch_btnSearch", no_wait_after=True)
                            page.wait_for_url("**/SearchResults.aspx")
                            page.wait_for_load_state("networkidle")
                            time.sleep(3)
                            
                        if detail_success:
                            # Map extracted details into CSV schema format
                            record = [
                                row_info["filing_date"],                # FILING DATE
                                detail_data.get("TERMINATION_DATE", "-"), # TERMINATION DATE
                                row_info["organization"],               # ORGANIZATION
                                row_info["client_name"],                # CLIENT NAME
                                detail_data.get("FILER", "-"),          # DESIGNATED FILER
                                detail_data.get("DEPARTMENTS", "-"),    # GOVERNMENT DEPARTMENT LOBBIED
                                detail_data.get("ENTITIES", "-"),       # PRESCRIBED PROVINCIAL ENTITY LOBBIED
                                detail_data.get("SUBJECTS", "-"),       # SUBJECT MATTER OF LOBBYING
                                reg_token,                             # REGISTRATION NUMBER
                                row_info["lobbyist_type"],              # TYPE OF LOBBYIST
                                detail_data.get("LOBBYISTS", "-"),      # LOBBYISTS
                                row_info["doc_type"],                   # TYPE OF REGISTRATION
                                row_info["status"],                     # REGISTRATION STATUS
                                detail_data.get("FULL_TEXT", "")        # EXTRACTED_PDF_DETAILS
                            ]
                            page_records.append(record)
                            existing_tokens.add(reg_token)
                        
                        time.sleep(1.5)
                        
                    if page_records:
                        chunk_df = pd.DataFrame(page_records, columns=global_headers)
                        if not os.path.exists(HISTORICAL_DATA_FILE) or os.path.getsize(HISTORICAL_DATA_FILE) == 0:
                            chunk_df.to_csv(HISTORICAL_DATA_FILE, index=False)
                        else:
                            chunk_df.to_csv(HISTORICAL_DATA_FILE, mode='a', header=False, index=False)
                        print(f"[CHECKPOINT] Saved Page {page_number} updates to ledger.")
                        
                    fresh_pages_processed += 1
                    if reached_cutoff:
                        print(" >> Cutoff reached. Exiting cleanly.")
                        break
                        
                    if fresh_pages_processed >= MAX_FRESH_PAGES_PER_RUN:
                        print(f"\n[SYSTEM] Reached maximum page threshold ({MAX_FRESH_PAGES_PER_RUN}). Pausing run.")
                        break
                        
                # Paginate to next page
                has_next = page.locator("input.rgPageNext").count() > 0
                if has_next:
                    try:
                        current_num = page.locator("a.rgCurrentPage").text_content().strip()
                    except Exception:
                        current_num = str(page_number)
                        
                    page.click("input.rgPageNext", no_wait_after=True)
                    
                    # Wait for current page indicator to shift
                    shifted = False
                    for _ in range(20):
                        time.sleep(0.5)
                        try:
                            new_num = page.locator("a.rgCurrentPage").text_content().strip()
                            if new_num != current_num:
                                shifted = True
                                break
                        except Exception:
                            pass
                            
                    if shifted:
                        page_number += 1
                        time.sleep(2)
                    else:
                        print("Timeout waiting for pagination transition. Halting crawler.")
                        break
                else:
                    print("Reached end of registry list completely.")
                    break
                    
        except Exception as e:
            print(f"[FATAL] Backfill pipeline execution fault: {str(e)}")
        finally:
            browser.close()

if __name__ == "__main__":
    backfill_historical_registry()
