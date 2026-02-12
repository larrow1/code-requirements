import csv
import io
import re

import pdfplumber
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

SOURCE_URL = "https://medicaid.ncdhhs.gov/providers/program-specific-clinical-coverage-policies"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/policies")
def get_policies():
    """Scrape the NC Medicaid policy listing page and return policy names + PDF URLs."""
    try:
        resp = requests.get(SOURCE_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to fetch policy listing: {e}"}), 502

    soup = BeautifulSoup(resp.text, "html.parser")
    policies = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "/download" not in href:
            continue
        # Build absolute URL
        if href.startswith("/"):
            url = "https://medicaid.ncdhhs.gov" + href
        else:
            url = href
        # Ensure download attachment parameter
        if "attachment" not in url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        name = a_tag.get_text(strip=True)
        if not name:
            continue

        policies.append({"name": name, "url": url})

    # Sort by name for readability
    policies.sort(key=lambda p: p["name"])
    return jsonify({"policies": policies})


_HCPCS_RE = re.compile(r"^[A-Za-z]\d{4}$")
_CPT_RE = re.compile(r"^\d{5}$")
_REVENUE_RE = re.compile(r"^\d{3,4}$")
# Broad pattern for peek/validation: matches CPT (5 digits),
# HCPCS (letter + 4 digits), revenue codes (3-4 digits or
# RC + 4 digits), and similar short alphanumeric codes.
_CODE_LIKE_RE = re.compile(r"^[A-Za-z]{0,2}\d{3,5}$")
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_MAX_CODE_HEADER_LEN = 50


def _classify_code(code, header_hint):
    """Return 'CPT', 'HCPCS', or 'Revenue' based on the code value and column header."""
    h = header_hint.lower()
    if "hcpcs" in h:
        return "HCPCS"
    if "cpt" in h:
        return "CPT"
    if "revenue" in h:
        return "Revenue"
    if _HCPCS_RE.match(code):
        return "HCPCS"
    if _CPT_RE.match(code):
        return "CPT"
    if _REVENUE_RE.match(code):
        return "Revenue"
    return "CPT"


def _is_policy_revision_table(table):
    """Detect policy implementation/revision history tables.

    These tables have headers like ['Date', 'Section Revised', 'Change'] on
    their first page, and on continuation pages the first data row becomes
    ``table[0]`` with a date in the first cell.
    """
    if not table or not table[0]:
        return False
    first_row = [(cell or "").strip().lower() for cell in table[0]]
    # Explicit header pattern: Date + Section Revised / Change
    if any("date" in c for c in first_row) and any(
        "section" in c or "change" in c or "revised" in c for c in first_row
    ):
        return True
    # Continuation page: first cell is a date like 10/01/2014
    first_cell = (table[0][0] or "").strip()
    if _DATE_RE.match(first_cell):
        return True
    return False


def extract_code_tables(pdf_url):
    """Download a PDF and extract all CPT/HCPCS code-bearing tables.

    Returns a dict with unified ``headers`` and ``rows`` across all tables,
    or None if no code tables are found.  On error, returns an error dict.
    """
    try:
        resp = requests.get(pdf_url, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"Failed to download PDF: {e}"}

    # First pass: collect per-table data with their non-code column names
    raw_tables = []  # list of (code_header, other_headers, data_rows)
    try:
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    continue
                for table in tables:
                    if not table or not table[0]:
                        continue

                    # Skip policy implementation / revision history tables
                    if _is_policy_revision_table(table):
                        continue

                    # Build original headers (strip whitespace, collapse \n → space)
                    raw_headers = [
                        re.sub(r"\s+", " ", (cell or "").strip())
                        for cell in table[0]
                    ]
                    lower_headers = [h.lower() for h in raw_headers]

                    # Identify a code column (skip ICD-10)
                    # Also reject headers that are too long — real code column
                    # headers (e.g. "CPT Code") are short, while long text is a
                    # data cell that happens to mention the word "code".
                    code_col = None
                    is_continuation = False
                    for i, h in enumerate(lower_headers):
                        if "code" in h:
                            if "icd-10" in h or "icd10" in h:
                                continue
                            if len(raw_headers[i]) > _MAX_CODE_HEADER_LEN:
                                continue
                            code_col = i
                            break

                    if code_col is None:
                        # Fallback: detect headerless continuation
                        # tables where rows are data, not headers.
                        first_cell_val = (table[0][0] or "").strip()
                        first_line = first_cell_val.split("\n")[0].strip()

                        if _CODE_LIKE_RE.match(first_line):
                            # Row 0 starts with a code → continuation
                            code_col = 0
                            is_continuation = True
                        elif not first_cell_val:
                            # Row 0 cell 0 is empty → may be a page
                            # break mid-row; check later rows.
                            for probe_row in table[1:]:
                                if not probe_row:
                                    continue
                                pv = (probe_row[0] or "").strip()
                                pl = pv.split("\n")[0].strip()
                                if _CODE_LIKE_RE.match(pl):
                                    code_col = 0
                                    is_continuation = True
                                    break

                        if code_col is None:
                            continue

                    # For continuation tables the first row is data,
                    # so derive code_header from the first actual code.
                    if is_continuation:
                        for probe_row in table:
                            if not probe_row:
                                continue
                            pv = (probe_row[0] or "").split("\n")[0].strip()
                            if _CODE_LIKE_RE.match(pv):
                                code_header = _classify_code(pv, "")
                                break
                        else:
                            code_header = ""
                    else:
                        code_header = raw_headers[code_col]

                    # Rows to iterate: skip header row for normal
                    # tables; include row 0 for continuation tables.
                    data_start = 0 if is_continuation else 1

                    # Check if all non-code column headers are empty.
                    # For continuation tables raw_headers holds data,
                    # so this is always effectively True.
                    all_other_empty = is_continuation or all(
                        not raw_headers[i]
                        for i in range(len(raw_headers))
                        if i != code_col
                    )

                    # When all other headers are blank (or this is a
                    # continuation table), peek at the data to decide
                    # if those columns hold additional codes
                    # (multi-column layout) or descriptions.
                    multi_col_codes = False
                    if all_other_empty:
                        for row in table[data_start:]:
                            if not row:
                                continue
                            for i in range(len(row)):
                                if i == code_col:
                                    continue
                                cell = (row[i] or "").strip()
                                if not cell:
                                    continue
                                first_line = cell.split("\n")[0].strip()
                                multi_col_codes = bool(
                                    _CODE_LIKE_RE.match(first_line)
                                )
                                break
                            else:
                                continue
                            break

                    seen = set()
                    data_rows = []

                    if multi_col_codes:
                        # --- Multi-column code table ---
                        table_note = ""
                        for row in table[data_start:]:
                            if not row:
                                continue
                            cells = [
                                (row[i] or "").strip()
                                for i in range(len(row))
                            ]
                            non_empty = [c for c in cells if c]
                            if not non_empty:
                                continue

                            # A row with a single non-empty cell whose first
                            # line doesn't look like a code is descriptive
                            # text that applies to every code in the table.
                            first_token = non_empty[0].split("\n")[0].strip()
                            looks_like_code = bool(
                                _CODE_LIKE_RE.match(first_token)
                            )
                            if len(non_empty) == 1 and not looks_like_code:
                                table_note = re.sub(
                                    r"\s+", " ", non_empty[0]
                                )
                                continue

                            for cell in cells:
                                if not cell:
                                    continue
                                for code_line in cell.split("\n"):
                                    code_line = code_line.strip()
                                    if not code_line:
                                        continue
                                    # Only accept lines that look
                                    # like valid codes.
                                    if not _CODE_LIKE_RE.match(code_line):
                                        continue
                                    if code_line not in seen:
                                        seen.add(code_line)
                                        data_rows.append(
                                            (code_line, [])
                                        )

                        # Apply the note uniformly to all rows.
                        if table_note:
                            data_rows = [
                                (c, [table_note]) for c, _ in data_rows
                            ]
                        other_headers = (
                            ["Note"] if table_note else []
                        )
                    else:
                        # --- Single code-column table ---
                        # Include unnamed columns as "Description".
                        # For continuation tables raw_headers holds
                        # data, not real headers, so always label
                        # non-code columns "Description".
                        other_indices = [
                            i for i in range(len(raw_headers))
                            if i != code_col
                            and (raw_headers[i]
                                 or all_other_empty
                                 or is_continuation)
                        ]
                        if is_continuation:
                            other_headers = [
                                "Description" for _ in other_indices
                            ]
                        else:
                            other_headers = [
                                raw_headers[i] or "Description"
                                for i in other_indices
                            ]

                        for row in table[data_start:]:
                            if not row:
                                continue
                            code_cell = (
                                (row[code_col] or "").strip()
                                if code_col < len(row) else ""
                            )
                            if not code_cell:
                                continue
                            other_vals = [
                                (row[i] or "").strip()
                                if i < len(row) else ""
                                for i in other_indices
                            ]
                            # Extract only lines that look like
                            # valid codes; the code cell may
                            # contain embedded descriptions
                            # (e.g. "0651\nRoutine Home\nCare").
                            for code_line in code_cell.split("\n"):
                                code_line = code_line.strip()
                                if not code_line:
                                    continue
                                if not _CODE_LIKE_RE.match(code_line):
                                    continue
                                row_key = (code_line, *other_vals)
                                if row_key not in seen:
                                    seen.add(row_key)
                                    data_rows.append(
                                        (code_line, other_vals)
                                    )

                    if data_rows:
                        raw_tables.append((code_header, other_headers, data_rows))
    except Exception as e:
        return {"error": f"Failed to parse PDF: {e}"}

    if not raw_tables:
        return None

    # Build union of all non-code column names (preserve first-seen order)
    all_other = []
    seen_cols = set()
    for _, other_headers, _ in raw_tables:
        for h in other_headers:
            if h not in seen_cols:
                seen_cols.add(h)
                all_other.append(h)

    headers = ["Code", "Code Type"] + all_other

    # Flatten into unified rows
    rows = []
    for code_header, other_headers, data_rows in raw_tables:
        # Map this table's columns to positions in the unified header
        col_map = {h: all_other.index(h) for h in other_headers}
        for code_val, other_vals in data_rows:
            code_type = _classify_code(code_val, code_header)
            unified = [""] * len(all_other)
            for j, h in enumerate(other_headers):
                unified[col_map[h]] = other_vals[j]
            rows.append([code_val, code_type] + unified)

    return {"headers": headers, "rows": rows}


@app.route("/api/extract", methods=["POST"])
def extract():
    """Accept a list of policies and extract code tables from each."""
    data = request.get_json()
    if not data or "policies" not in data:
        return jsonify({"error": "Missing 'policies' in request body"}), 400

    results = []
    for policy in data["policies"]:
        name = policy.get("name", "Unknown")
        url = policy.get("url", "")
        extraction = extract_code_tables(url)

        if extraction is None:
            results.append({
                "name": name,
                "url": url,
                "status": "no_table",
                "headers": [],
                "rows": [],
            })
        elif isinstance(extraction, dict) and "error" in extraction:
            results.append({
                "name": name,
                "url": url,
                "status": "error",
                "error": extraction.get("error", "Unknown error"),
                "headers": [],
                "rows": [],
            })
        else:
            results.append({
                "name": name,
                "url": url,
                "status": "success",
                "headers": extraction["headers"],
                "rows": extraction["rows"],
            })

    return jsonify({"results": results})


@app.route("/api/export-csv", methods=["POST"])
def export_csv():
    """Accept extracted results and return a downloadable CSV."""
    data = request.get_json()
    if not data or "results" not in data:
        return jsonify({"error": "Missing 'results' in request body"}), 400

    # Build a global union of all non-code/type columns across all policies
    all_extra = []
    seen_extra = set()
    for result in data["results"]:
        for h in result.get("headers", [])[2:]:  # skip Code, Code Type
            if h not in seen_extra:
                seen_extra.add(h)
                all_extra.append(h)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Policy Name", "PDF URL", "Code", "Code Type"] + all_extra)

    for result in data["results"]:
        name = result.get("name", "")
        url = result.get("url", "")
        result_extra = result.get("headers", [])[2:]
        col_map = {h: i for i, h in enumerate(result_extra)}
        for row in result.get("rows", []):
            code = row[0] if len(row) > 0 else ""
            code_type = row[1] if len(row) > 1 else ""
            row_extra = row[2:]
            unified = [""] * len(all_extra)
            for h, i in col_map.items():
                pos = all_extra.index(h)
                unified[pos] = row_extra[i] if i < len(row_extra) else ""
            writer.writerow([name, url, code, code_type] + unified)

    csv_content = output.getvalue()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=medicaid_code_tables.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
