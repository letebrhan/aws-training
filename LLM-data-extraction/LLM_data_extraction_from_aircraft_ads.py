#!/usr/bin/env python3

"""
compute_full_engine_metrics.py

1. Loads Test42Inputs.xlsx (sheet 'Sheet1') with columns:
     - ID
     - Description (raw ad text)

2. Extracts, for each ad:
     • TTAF         – Time To Aircraft Frame (alias for total hours)
     • TSN          – Time Since New
     • CSN          – Cycles Since New
     • TSOH         – Time Since Overhaul (if given)
     • Early TBO    – Mid‐life threshold (fixed at 4000h)
     • Hours since HSI
     • Date of Last HSI
     • Time remaining before overhaul
     • On Condition_R (boolean)
     • Basis of Calculation (which field drove the “remaining”)
     • Date of Last Overhaul
     • Date of Overhaul Due
     • years_left_for_operation
     • Avg Hours left for operation according to 450 h/yr
     • Engine Program Name Ongoing or enrolled_1

3. Applies business rules:
     • TBO = 8000h, mid‐life (HSI) at 4000h or 10 yr
     • Overhaul due at earlier of (TBO OR 20 years) OR (HSI + 4000h OR 10 yr)
     • “On Condition” engines → flag special
     • “Corporate Care/JSSI/etc” paid programs → treat as full life

4. Writes out a new workbook TestAnswers_updated_full.xlsx with these fields.

Dependencies:
    pip install pandas openpyxl python-dateutil
"""

"""
compute_engine_metrics.py

Reads ads from Interview20250501_result/Test42Inputs.xlsx,
extracts engine metrics via OpenAI and regex fallbacks,
computes all derived fields, and writes results to
Interview20250501_result/TestAnswers_filled.xlsx.
"""




import os
import re
import json
from datetime import datetime, timedelta
import pandas as pd
from dateutil import parser as date_parser
import openai

# Add this at the top of your script
from dotenv import load_dotenv

# Load .env file
load_dotenv()
# Now read the API key from the environment
openai.api_key = os.getenv("OPENAI_API_KEY")


# 1) Working directory & paths
cwd  = os.getcwd()
path = os.path.join(cwd, 'Interview20250501_output')
INPUT_ADS    = os.path.join(path, 'Test42Inputs.xlsx')
INPUT_ANS    = os.path.join(path, 'TestAnswers.xlsx')
OUTPUT_FILE  = os.path.join(path, 'TestAnswers_LLM_filled.xlsx')
ADS_SHEET    = 'Sheet1'
ANS_SHEET    = 'Answers'

# 2) Engine lifecycle rules(Constants)

tbo_hours = 8000
midlife_hours = 4000
annual_usage=450

TODAY = datetime.today().date()

LEFT_PATTERNS = [r'(\\d+)\\s+hrs?\\s+left', r'Remaining[:\\s]+(\\d+)\\s+hrs?']
PROG_PATTERN = r'(JSSI|MSP|Corporate Care|Rolls Royce Corporate Care|Honeywell HAPP)'
DATE_PATTERNS = [r'Last HSI[:\\s]+([A-Za-z0-9 ,/-]+)', r'Date of HSI[:\\s]+([A-Za-z0-9 ,/-]+)']

# System prompt used for LLM
SYSTEM_PROMPT = (
    "You are an expert JSON extraction assistant specialized in parsing Gulfstream G-IV, G-IVSP, and G450 aircraft ads.\n\n"
    "Your task is to extract detailed engine data for BOTH LEFT and RIGHT engines from free-form ad text.  "
    "Return ONLY a JSON object with two keys—\"LEFT\" and \"RIGHT\"—each containing an object with exactly these fields in this order:\n\n"

    "  • TTAF: total airframe hours (integer). Extract this only once from the ad. Shared between LEFT and RIGHT. Look anywhere in the ad for patterns like:\n"
    "       - 'Airframe Total Time (\\d+)'\n"
    "       - 'TTAF:\\s*(\\d+)\\s*Hrs'\n"
    "       - 'Airframe:\\s*(\\d+)\\s*Hrs'\n\n"

    "  • TSN: time since new (integer).\n"
    "       - Extract from the engine table rows only — never from the TTAF.\n"
    "       - In engine rows, the value immediately following the engine model (e.g., “611-8”, “611-8 (GI V)”) is the **Serial Number** (e.g., “16455”), not TSN.\n"
    "       - TSN and CSN follow the Serial Number.\n"
    "       - Example: Rolls Royce TAY 611-8 16455 8467 2654\n"
    "         → Serial# = 16455, TSN = 8467, CSN = 2654\n"

    "  • CSN: cycles since new (integer)\n"
    "  • TSML: time since mid-life or HSI (integer)\n"
    "  • TSOH: time since last overhaul (float)\n"
    "  • CSML: cycles since mid-life (integer)\n"
    "  • CSOH: cycles since overhaul (integer)\n"
    "  • EarlyTBO: planned mid-life interval (usually 4000), or null if unavailable\n"
    "  • HoursSinceHSI: same as TSML\n"
    "  • DateOfLastHSI: ISO 8601 format date parsed from 'Midlife c/w <Month> <Year>' or similar\n"
    "  • TimeRemainingBeforeOverhaul: EarlyTBO - TSML\n"
    "  • OnCondition_R: true if 'On Condition' appears anywhere in ad\n"
    "  • BasisOfCalculation: one of 'TSN', 'TSML', 'explicit', 'program', or 'on_condition'\n"
    "  • DateOfLastOverhaul: ISO 8601, or null\n"
    "  • DateOfOverhaulDue: ISO 8601, or null\n"
    "  • years_left_for_operation: TimeRemainingBeforeOverhaul / 450\n"
    "  • AvgHoursLeft_450h_per_year: same as TimeRemainingBeforeOverhaul\n"
    "  • EngineProgramNameOngoingOrEnrolled_1: the name of the engine program, or null\n\n"

    "**Formatting rules:**\n"
    "  - JSON numbers must not be quoted\n"
    "  - Strings use double quotes\n"
    "  - Dates in 'YYYY-MM-DD' format\n"
    "  - true/false for booleans\n"
    "  - null for missing data\n\n"

    "**Engine-table header variants:**\n"
    "  1. Loc. Make Model Serial# TSN CSN TSML L\n"
    "  2. Loc. Make Model Serial# TSN CSN L\n"
    "  3. Loc. Make Model Serial# TSN CSN TSOH L\n"
    "  4. Loc. Make Model Serial# TSML L\n"
    "  5. Loc. Make Model Serial# TSN CSN CSOH TSML CSML TSOH L\n"
    "  6. Loc. Model Serial# TSN CSN L\n"
    "  7. Loc. TSN CSN TSOH CSOH L\n\n"

    "**Parsing steps:**\n"
    "  1. Extract TTAF using regex from the entire ad.\n"
    "  2. Find the first line containing 'TSN' and 'CSN' (case-insensitive).\n"
    "  3. Record the positions (indices) of TSN, CSN, TSML, TSOH, CSOH, CSML, EarlyTBO (if present).\n"
    "  4. Extract the 2 lines immediately below as engine rows.\n"
    "  5. Use the label BEFORE TSN (e.g. 'L', 'Left', 'Engine 1') to assign LEFT; use 'R', 'Right', 'Engine 2' to assign RIGHT.\n"
    "     – If label is ambiguous or missing, match TSN/CSN/Serial# positionally or narratively.\n"
    "  6. Slice engine row using column indices, map values to fields accordingly.\n"
    "  7. Extract HSI date using '<Month> <Year>' after 'Midlife c/w' or 'Ten Year Calendar:', convert to 'YYYY-MM-01'.\n"
    "  8. Detect if 'On Condition' is present in ad.\n"
    "  9. Compute:\n"
    "     – TimeRemainingBeforeOverhaul = max(0, EarlyTBO - TSML)\n"
    "     – years_left_for_operation = TimeRemainingBeforeOverhaul / 450\n"
    "     – AvgHoursLeft_450h_per_year = TimeRemainingBeforeOverhaul\n"
    " 10. If no table is found, search narrative sections for patterns matching 'engine serial', 'TSN:', 'TSML:', etc.\n"
    "     – Use fallback values only when confident.\n"
    " 11. Return ONLY valid JSON with 'LEFT' and 'RIGHT' keys. No extra text or explanation.\n\n"

    "Now extract structured JSON for both engines from this ad text:"
)



def extract_ttaf_from_ad(ad_text):
    # Define regex patterns for TTAF
    patterns = [
        r"TTAF:\s*(\d+)\s*Hrs",
        r"Airframe Total Time\s*(\d+)",
        r"Airframe:\s*(\d+)\s*Hrs"
    ]
    for pattern in patterns:
        match = re.search(pattern, ad_text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None

# Function to extract data from ad text
def call_llm_extraction(ad_text, ad_id=None):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Ad text:\n\"\"\"\n{ad_text}\n\"\"\"\n\nJSON:"}
            ],
            temperature=0,
            max_tokens=1500,
            stop=["\n\n"]
        )
        raw = response.choices[0].message.content
        match = re.search(r'{.*}', raw, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}

        left = parsed.get("LEFT", {})
        right = parsed.get("RIGHT", {})
        # If TTAF is in the global response, inject it into both engines
        if "TTAF" in parsed['LEFT'] and "TTAF" in parsed['RIGHT']:
            parsed['RIGHT'].pop('TTAF')
        else:
            # Inject TTAF into both LEFT and RIGHT
            ttaf_value = extract_ttaf_from_ad(ad_text)
            if ttaf_value is not None:
                left["TTAF"] = ttaf_value

        for engine, position in [(left, "LEFT"), (right, "RIGHT")]:
            engine["Position"] = position
            engine["ID"] = ad_id
           
        return [left, right]

    except Exception as e:
        print(f"Error parsing ad (ID={ad_id}): {e}")
        return []
    

# 6) Helper extraction functions
def extract_number(text, patterns):
    """
    Try each regex in patterns (a list of strings), return first match as int or None.
    """
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


# 7) Compute all metrics for one ad
def compute_metrics(ad_text, ad_id):
    # 1) Raw extraction via LLM
    engine_data = call_llm_extraction(ad_text, ad_id)

    results = []
    for fields in engine_data:

        # 2) Fallback regex for numeric fields
        tsn  = fields.get('TSN') 
        tsml = fields.get('TSML') 
        csn  = fields.get('CSN') 
        tsoh = fields.get('TSOH') 

        # 3) Normalize LastOverhaulDate to a datetime.date
        last_ov_raw = fields.get('DateOfLastOverhaul')
        if isinstance(last_ov_raw, str):
            try:
                last_ov = date_parser.parse(last_ov_raw).date()
            except:
                last_ov = None
        else:
            last_ov = last_ov_raw

        # 4) OnCondition and MaintenanceProgram
        on_cond = bool(fields.get('OnCondition_R'))
        prog    = fields.get('EngineProgramNameOngoingOrEnrolled_1') or ''
        pmatch  = re.search(PROG_PATTERN, prog or ad_text, re.IGNORECASE)
        program_name = pmatch.group(1) if pmatch else None

        # 5) Derive basics
        early_tbo       =  fields.get('EarlyTBO')
        hours_since_hsi =  fields.get('HoursSinceHSI') 
        date_last_hsi   = fields.get('DateOfLastHSI')  # not in text

        # 6) Compute remaining & basis
        time_remaining_by_tsn = tbo_hours - tsn if tsn is not None else float("inf")
        time_remaining_by_tsoh = midlife_hours - tsoh if tsoh is not None else float("inf")

        time_remaining_before_overhaul, basis = None, None
        if program_name:
            time_remaining_before_overhaul, basis = tbo_hours, 'program'
        else:
            explicit = extract_number(ad_text, LEFT_PATTERNS)
            if explicit is not None:
                time_remaining_before_overhaul = explicit
                basis = 'explicit'
            elif tsml is not None:
                time_remaining_before_overhaul = max(0, midlife_hours - tsml)
                basis = 'Time Since Mid-Life(TSML)'
            else:
                if time_remaining_by_tsoh < time_remaining_by_tsn:
                    time_remaining_before_overhaul = max(0, time_remaining_by_tsoh)
                    basis = 'time since last overhaul(TSOH)'
                else:
                    time_remaining_before_overhaul = max(0, time_remaining_by_tsn)
                    basis = 'Time Since New(TSN)' 
                            

        years_left = round(time_remaining_before_overhaul / annual_usage, 2) if time_remaining_before_overhaul is not None else None
        avg_hours_left_450h_per_year = time_remaining_before_overhaul
        
        # Date of Overhaul Due
        due_date = fields.get('DateOfOverhaulDue')
        if isinstance(due_date, str):
            try:
                due_date = date_parser.parse(due_date).date()
            except:
                due_date = None
        else:
            due_date = due_date

        if not due_date:
            candidates = []
            if last_ov:
                candidates.append(last_ov + timedelta(days=20 * 365))
            if tsml is not None or tsn is not None:
                used = tsml if tsml is not None else tsn
                hrs_left = tbo_hours - used
                yrs = hrs_left / annual_usage
                candidates.append(TODAY + timedelta(days=yrs * 365))
            due_date = min(candidates) if candidates else None

            # 8) Years & avg-hours left
            years_left = ((due_date - TODAY).days / 365.25) if due_date else None
            avg_hours_left_450h_per_year   = years_left * annual_usage if years_left else None

        # 9) Return all metrics
        results.append({
            'ID': ad_id,
            'Text': ad_text,
            'TTAF': fields.get('TTAF'),
            'Position' : fields.get('Position'),
            'TSN': tsn,
            'CSN': csn,
            'TSOH': tsoh,
            'Early TBO': early_tbo,
            'Hours since HSI': hours_since_hsi,
            'Date of Last HSI': date_last_hsi,
            'Time remaining before overhaul': time_remaining_before_overhaul,
            'On Condition_R': on_cond,
            'Basis of Calculation': basis,
            'Date of Last Overhaul': fields.get('DateOfLastOverhaul'),
            'Date of Overhaul Due': fields.get('DateOfOverhaulDue'),
            'years_left_for_operation': years_left,
            'Avg Hours left for operation according to 450 hours annual usage': avg_hours_left_450h_per_year,
            'Engine Program Name Ongoing or enrolled_1': prog
        })
    
    return results


# 8) Main execution
df_ads = pd.read_excel(INPUT_ADS, sheet_name=ADS_SHEET)
df_ans = pd.read_excel(INPUT_ANS, sheet_name=ANS_SHEET)

all_records = []
for _, row in df_ads.iterrows():
    metrics = compute_metrics(str(row['Description']), row['ID'])
    #metrics['ID'] = [row['ID'], None]
    all_records.extend(metrics)


df_computed = pd.DataFrame(all_records)
#df_merged   = df_ans.merge(df_computed, on='ID', how='left')

# Write out the filled Answers + Computed sheet
with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
    #df_merged.to_excel(writer, sheet_name=ANS_SHEET, index=False)
    df_computed.to_excel(writer, sheet_name='Computed', index=False)

print(f"✅ Completed. Outputs in:\n  {OUTPUT_FILE}")















