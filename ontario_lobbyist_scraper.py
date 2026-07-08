import os
import sys
import re
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORICAL_DATA_FILE = os.path.join(CURRENT_DIR, "ontario_lobbyists_historical.csv")
BASE_URL = "https://lobbyist.oico.on.ca/Pages/Public/PublicSearch/"

def send_email_digest(html_content, subject_text="Daily Lobbyist Registry Update"):
    """Connects to Gmail SMTP backbone to transmit the compiled HTML dataset."""
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("NOTIFY_EMAIL")
    
    if not all([username, password, recipient]):
        print("[WARNING] Email credentials missing from GitHub secrets environment. Skipping notification.")
        return

    smtp_server = "smtp.gmail.com"
    smtp_port = 587

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject_text
    msg["From"] = username
    msg["To"] = recipient

    msg.attach(MIMEText(html_content, "html"))

    try:
        print(f"Opening secure encrypted transport channel to {smtp_server}...")
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, recipient, msg.as_string())
        print(f"Success! Daily update digest sent safely to target address: {recipient}")
    except Exception as email_fault:
        print(f"[ERROR] Mail pipeline transmission dropped: {str(email_fault)}")

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

def execute_daily_scrape():
    print("Initiating incremental Ontario lobbyist monitoring check...")
    
    if not os.path.exists(HISTORICAL_DATA_FILE):
        print(f"[FATAL] Reference historical ledger not found: {HISTORICAL_DATA_FILE}")
        sys.exit(1)
        
    historical_df = pd.read_csv(HISTORICAL_DATA_FILE)
    
    # We build existing signatures based on Registration Number + Filing Date + Registration Status
    if all(col in historical_df.columns for col in ["REGISTRATION NUMBER", "FILING DATE", "REGISTRATION STATUS"]):
        existing_signatures = set((
            historical_df["REGISTRATION NUMBER"].astype(str).str.strip() + "_" +
            historical_df["FILING DATE"].astype(str).str.strip() + "_" +
            historical_df["REGISTRATION STATUS"].astype(str).str.strip()
        ).tolist())
        print(f"[LOADED] Found archive. Loaded {len(existing_signatures)} historical filing signatures.")
    else:
        print("[FATAL] Structural anomalies located in historical CSV headers.")
        sys.exit(1)

    new_records_captured = []

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
        
        try:
            print(f"Navigating to live search page: {BASE_URL}")
            page.goto(BASE_URL, wait_until="networkidle")
            
            print("Clicking search to populate results...")
            page.click("#BodyContent_ucQuickSearch_btnSearch", no_wait_after=True)
            page.wait_for_url("**/SearchResults.aspx")
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            
            while True:
                matrix = get_row_data_matrix(page)
                if not matrix or len(matrix) < 2:
                    print("No search results found.")
                    break
                    
                try:
                    current_pagination_state = page.locator("a.rgCurrentPage").text_content().strip()
                except Exception:
                    current_pagination_state = str(page_number)
                print(f"\n--- SCRAPER ACTIVE: PAGE {page_number} (Active Index: {current_pagination_state}) ---")
                
                valid_rows_to_process = []
                contains_new_records = False
                
                # Fetch row elements to match grid indexes
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
                            
                            valid_rows_to_process.append({
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
                            if row_signature not in existing_signatures:
                                contains_new_records = True
                                
                if valid_rows_to_process and not contains_new_records:
                    print(f" >> [CATCH-UP COMPLETE] Hit baseline records on Page {page_number}. Halting search cleanly.")
                    break
                    
                if contains_new_records:
                    for row_info in valid_rows_to_process:
                        reg_token = row_info["reg_token"]
                        row_signature = row_info["signature"]
                        
                        if row_signature in existing_signatures:
                            continue
                            
                        print(f"  -> Frontier Alert: Syncing brand-new filing update: {row_signature}")
                        
                        row_selector = f"tr#ctl00_BodyContent_ucSearchResults_gridSearchResults_GridRegistrationList_ctl00__{row_info['index']}"
                        detail_success = False
                        detail_data = {}
                        
                        try:
                            page.click(row_selector, no_wait_after=True)
                            page.locator("#BodyContent_ucRegistrationSubmit_btnBackToResultsTop").wait_for(timeout=15000)
                            
                            html_content = page.content()
                            detail_data = parse_detail_html(html_content)
                            detail_success = True
                        except Exception as click_err:
                            print(f"      * Could not download details for {reg_token}: {str(click_err)}")
                            
                        # Go back to search results grid
                        try:
                            page.click("#BodyContent_ucRegistrationSubmit_btnBackToResultsTop", no_wait_after=True)
                            page.locator("tr[id*=GridRegistrationList]").first.wait_for(timeout=15000)
                            time.sleep(2)
                        except Exception as back_err:
                            print(f"      * Error returning to search results: {str(back_err)}")
                            # Recover page session if needed
                            page.goto(BASE_URL, wait_until="networkidle")
                            page.click("#BodyContent_ucQuickSearch_btnSearch", no_wait_after=True)
                            page.wait_for_url("**/SearchResults.aspx")
                            page.wait_for_load_state("networkidle")
                            time.sleep(3)
                            
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
                            new_records_captured.append(record)
                            
                        time.sleep(1.5)
                        
                # Next Page pagination
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
                        print("Timeout waiting for pagination transition. Stopping scraper.")
                        break
                else:
                    print("Reached end of registry list completely.")
                    break
                    
            # Process outputs and send emails if there are additions
            if new_records_captured:
                new_df = pd.DataFrame(new_records_captured, columns=global_headers)
                
                display_df = new_df.copy()
                if "EXTRACTED_PDF_DETAILS" in display_df.columns:
                    display_df["EXTRACTED_PDF_DETAILS"] = display_df["EXTRACTED_PDF_DETAILS"].str.slice(0, 180) + "..."
                
                html_table = display_df.to_html(index=False, classes="dataframe")
                
                email_body = f"""
                <html>
                <head>
                    <style>
                        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #333333; line-height: 1.5; }}
                        table.dataframe {{ border-collapse: collapse; width: 100%; margin-top: 15px; font-size: 13px; }}
                        th {{ background-color: #198754; color: white; border: 1px solid #198754; padding: 12px; text-align: left; font-weight: 600; }}
                        td {{ border: 1px solid #e0e0e0; padding: 10px; }}
                        tr:nth-child(even) {{ background-color: #f8f9fa; }}
                        .alert-header {{ color: #198754; font-weight: bold; font-size: 20px; border-bottom: 2px solid #198754; padding-bottom: 8px; }}
                    </style>
                </head>
                <body>
                    <div class="alert-header">Ontario Lobbyist Registry: New Disclosures Located</div>
                    <p>The daily monitor pipeline isolated the following brand-new filings within the live index:</p>
                    {html_table}
                    <br>
                    <p style="font-size: 11px; color: #888888; border-top: 1px solid #eeeeee; padding-top: 8px;">
                        This is an automated report delivered securely via your automated GitHub Actions infrastructure pipeline.
                    </p>
                </body>
                </html>
                """
                
                send_email_digest(email_body, subject_text=f"Alert: {len(new_records_captured)} New Ontario Lobbyist Registrations Detected")
                
                # Prepend to CSV
                consolidated_df = pd.concat([new_df, historical_df], ignore_index=True)
                consolidated_df.to_csv(HISTORICAL_DATA_FILE, index=False)
                print(f"[SUCCESS] Prepended {len(new_records_captured)} new files to top of ledger and triggered email.")
            else:
                print("[IDLE] Index clean. 0 new disclosures discovered today.")
                
        except Exception as e:
            print(f"[CRITICAL ERROR] Daily monitor execution fault: {str(e)}")
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    execute_daily_scrape()
