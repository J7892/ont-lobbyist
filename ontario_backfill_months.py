import os
import sys
import time
import re
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORICAL_DATA_FILE = os.path.join(CURRENT_DIR, "ontario_lobbyists_historical.csv")
BASE_URL = "https://lobbyist.oico.on.ca/Pages/Public/PublicSearch/"

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

def parse_detail_html(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
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
    
    first_name_el = soup.find(id=lambda val: val and "lblFirstNameValue" in val)
    last_name_el = soup.find(id=lambda val: val and "lblLastNameValue" in val)
    if first_name_el and last_name_el:
        filer = f"{first_name_el.text.strip()} {last_name_el.text.strip()}"
        
    subj_el = soup.find(id=lambda val: val and "lblSubjectMatter" in val and "OLSubjectMatters" in val)
    if not subj_el:
        subj_el = soup.find(id=lambda val: val and "lblSubjectMatter" in val)
    if subj_el:
        subjects = [s.strip() for s in subj_el.text.split(";") if s.strip()]
        
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

def load_existing_signatures():
    if not os.path.exists(HISTORICAL_DATA_FILE):
        return set()
    try:
        df = pd.read_csv(HISTORICAL_DATA_FILE)
        if all(col in df.columns for col in ["REGISTRATION NUMBER", "FILING DATE", "REGISTRATION STATUS"]):
            return set((
                df["REGISTRATION NUMBER"].astype(str).str.strip() + "_" +
                df["FILING DATE"].astype(str).str.strip() + "_" +
                df["REGISTRATION STATUS"].astype(str).str.strip()
            ).tolist())
    except Exception:
        pass
    return set()

def scrape_month(start_date, end_date):
    existing_signatures = load_existing_signatures()
    print(f"\n=======================================================")
    print(f"STARTING CRAWL CHUNK: {start_date} to {end_date}")
    print(f"Loaded {len(existing_signatures)} signatures from CSV.")
    print(f"=======================================================")
    
    new_records = []
    global_headers = [
        "FILING DATE", "TERMINATION DATE", "ORGANIZATION", "CLIENT NAME", "DESIGNATED FILER",
        "GOVERNMENT DEPARTMENT LOBBIED", "PRESCRIBED PROVINCIAL ENTITY LOBBIED", "SUBJECT MATTER OF LOBBYING",
        "REGISTRATION NUMBER", "TYPE OF LOBBYIST", "LOBBYISTS", "TYPE OF REGISTRATION",
        "REGISTRATION STATUS", "EXTRACTED_PDF_DETAILS"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            viewport={'width': 1440, 'height': 900}
        )
        page = context.new_page()
        
        try:
            page.goto(BASE_URL, wait_until="networkidle")
            
            # Select "Any Status"
            page.click("#BodyContent_ucQuickSearch_rdoAnyStatus")
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            
            # Select "Active Within Dates"
            page.click("#BodyContent_ucQuickSearch_rdoActiveWithinDates")
            page.wait_for_load_state("networkidle")
            time.sleep(2)
            
            # Fill From and To dates
            page.locator("#ctl00_BodyContent_ucQuickSearch_dpFromDate_dateInput_text").click()
            page.locator("#ctl00_BodyContent_ucQuickSearch_dpFromDate_dateInput_text").type(start_date, delay=100)
            page.keyboard.press("Tab")
            time.sleep(0.5)
            
            page.locator("#ctl00_BodyContent_ucQuickSearch_dpToDate_dateInput_text").click()
            page.locator("#ctl00_BodyContent_ucQuickSearch_dpToDate_dateInput_text").type(end_date, delay=100)
            page.keyboard.press("Tab")
            time.sleep(1)
            
            # Click search
            page.click("#BodyContent_ucQuickSearch_btnSearch", no_wait_after=True)
            page.wait_for_url("**/SearchResults.aspx", timeout=45000)
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            
            page_number = 1
            while True:
                matrix = get_row_data_matrix(page)
                if not matrix or len(matrix) < 2:
                    print("  No records found for this date range.")
                    break
                    
                # Active page info
                try:
                    current_pagination = page.locator("a.rgCurrentPage").text_content().strip()
                except Exception:
                    current_pagination = str(page_number)
                print(f"  [GRID] Page {page_number} (Active Index: {current_pagination})")
                
                valid_rows = []
                row_locators = page.locator("tr[id*=GridRegistrationList_ctl00__]")
                row_count = row_locators.count()
                
                for r_idx in range(row_count):
                    row_id = row_locators.nth(r_idx).get_attribute("id")
                    suffix = row_id.split("__")[-1]
                    
                    cells = row_locators.nth(r_idx).locator("td")
                    cell_texts = [cells.nth(c).text_content().strip() for c in range(cells.count())]
                    
                    if len(cell_texts) >= 8:
                        lobbyist = cell_texts[0]
                        amendment_date = reformat_date(cell_texts[1])
                        client_name = cell_texts[2] or "-"
                        org_name = cell_texts[3] or "-"
                        lobbyist_type = cell_texts[4]
                        reg_token = cell_texts[5]
                        doc_type = cell_texts[6] or "-"
                        status = cell_texts[7]
                        
                        if reg_token:
                            row_signature = f"{reg_token}_{amendment_date}_{status}"
                            valid_rows.append({
                                "index": suffix,
                                "lobbyist": lobbyist,
                                "filing_date": amendment_date,
                                "client_name": client_name,
                                "organization": org_name,
                                "lobbyist_type": lobbyist_type,
                                "reg_token": reg_token,
                                "doc_type": doc_type,
                                "status": status,
                                "signature": row_signature
                            })
                            
                page_records = []
                for row_info in valid_rows:
                    reg_token = row_info["reg_token"]
                    row_signature = row_info["signature"]
                    
                    if row_signature in existing_signatures:
                        continue
                        
                    print(f"    -> Crawling details: {reg_token} ({row_info['filing_date']})")
                    
                    row_selector = f"tr#ctl00_BodyContent_ucSearchResults_gridSearchResults_GridRegistrationList_ctl00__{row_info['index']}"
                    detail_success = False
                    detail_data = {}
                    
                    try:
                        page.click(row_selector, no_wait_after=True)
                        page.locator("#BodyContent_ucRegistrationSubmit_btnBackToResultsTop").wait_for(timeout=20000)
                        
                        html_content = page.content()
                        detail_data = parse_detail_html(html_content)
                        detail_success = True
                    except Exception as err:
                        print(f"        * Error loading details for {reg_token}: {err}")
                        
                    # Return back to grid
                    try:
                        page.click("#BodyContent_ucRegistrationSubmit_btnBackToResultsTop", no_wait_after=True)
                        page.locator("tr[id*=GridRegistrationList]").first.wait_for(timeout=20000)
                        time.sleep(1.5)
                    except Exception as back_err:
                        print(f"        * Error returning to grid: {back_err}")
                        # Force refresh/re-search if stuck
                        break
                        
                    if detail_success:
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
                        existing_signatures.add(row_signature)
                        
                    time.sleep(1)
                    
                # Save page records
                if page_records:
                    chunk_df = pd.DataFrame(page_records, columns=global_headers)
                    if not os.path.exists(HISTORICAL_DATA_FILE) or os.path.getsize(HISTORICAL_DATA_FILE) == 0:
                        chunk_df.to_csv(HISTORICAL_DATA_FILE, index=False)
                    else:
                        chunk_df.to_csv(HISTORICAL_DATA_FILE, mode='a', header=False, index=False)
                    print(f"    [CHECKPOINT] Saved page updates to CSV.")
                    
                # Paginate to next page
                has_next = page.locator("input.rgPageNext").count() > 0
                if has_next:
                    try:
                        current_num = page.locator("a.rgCurrentPage").text_content().strip()
                    except Exception:
                        current_num = str(page_number)
                        
                    page.click("input.rgPageNext", no_wait_after=True)
                    
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
                        time.sleep(1.5)
                    else:
                        print("    [PAGER] Timeout waiting for pagination transition. Halting page loop.")
                        break
                else:
                    break
                    
        except Exception as e:
            print(f"  [ERROR] Scraping failed for date range {start_date} to {end_date}: {e}")
        finally:
            browser.close()
            
    # Sleep to cool down session
    time.sleep(5)

def main():
    # Define monthly intervals from Jan 2025 to June 2026
    # Let's run a loop for the months.
    intervals = [
        ("2025-01-01", "2025-01-31"),
        ("2025-02-01", "2025-02-28"),
        ("2025-03-01", "2025-03-31"),
        ("2025-04-01", "2025-04-30"),
        ("2025-05-01", "2025-05-31"),
        ("2025-06-01", "2025-06-30"),
        ("2025-07-01", "2025-07-31"),
        ("2025-08-01", "2025-08-31"),
        ("2025-09-01", "2025-09-30"),
        ("2025-10-01", "2025-10-31"),
        ("2025-11-01", "2025-11-30"),
        ("2025-12-01", "2025-12-31"),
        ("2026-01-01", "2026-01-31"),
        ("2026-02-01", "2026-02-28"),
        ("2026-03-01", "2026-03-31"),
        ("2026-04-01", "2026-04-30"),
        ("2026-05-01", "2026-05-31"),
        ("2026-06-01", "2026-06-30"),
    ]
    
    print(f"Beginning monthly chunk backfill execution back to Jan 1 2025...")
    print(f"Intervals to process: {len(intervals)}")
    
    for start, end in intervals:
        scrape_month(start, end)
        
    print("\n[SUCCESS] Monthly backfill process completed successfully!")

if __name__ == "__main__":
    main()
