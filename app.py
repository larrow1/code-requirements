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


def _classify_code(code, header_hint):
    """Return 'CPT' or 'HCPCS' based on the code value and column header."""
    h = header_hint.lower()
    if "hcpcs" in h:
        return "HCPCS"
    if "cpt" in h:
        return "CPT"
    if _HCPCS_RE.match(code):
        return "HCPCS"
    if _CPT_RE.match(code):
        return "CPT"
    return "CPT"


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

                    # Build original headers (strip whitespace, collapse \n → space)
                    raw_headers = [
                        re.sub(r"\s+", " ", (cell or "").strip())
                        for cell in table[0]
                    ]
                    lower_headers = [h.lower() for h in raw_headers]

                    # Identify a code column (skip ICD-10)
                    code_col = None
                    for i, h in enumerate(lower_headers):
                        if "code" in h:
                            if "icd-10" in h or "icd10" in h:
                                continue
                            code_col = i
                            break
                    if code_col is None:
                        continue

                    code_header = raw_headers[code_col]

                    # Non-code columns with non-empty headers
                    other_indices = [
                        i for i in range(len(raw_headers))
                        if i != code_col and raw_headers[i]
                    ]
                    other_headers = [raw_headers[i] for i in other_indices]

                    # Extract rows, splitting newline-separated codes
                    seen = set()
                    data_rows = []
                    for row in table[1:]:
                        if not row:
                            continue
                        code_val = (row[code_col] or "").strip() if code_col < len(row) else ""
                        if not code_val:
                            continue
                        other_vals = [
                            (row[i] or "").strip() if i < len(row) else ""
                            for i in other_indices
                        ]
                        for code_line in code_val.split("\n"):
                            code_line = code_line.strip()
                            if not code_line:
                                continue
                            row_key = (code_line, *other_vals)
                            if row_key not in seen:
                                seen.add(row_key)
                                data_rows.append((code_line, other_vals))

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
