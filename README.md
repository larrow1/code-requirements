# NC Medicaid Code Tables Extractor

Extract CPT/HCPCS code tables from NC Medicaid clinical coverage policy PDFs.

## How It Works

The application scrapes the [NC Medicaid clinical coverage policies page](https://medicaid.ncdhhs.gov/providers/program-specific-clinical-coverage-policies) for links to policy PDF documents. When a user selects policies to extract, each PDF is downloaded and parsed with [pdfplumber](https://github.com/jsvine/pdfplumber) to find tables that contain CPT or HCPCS codes.

**Table detection** — A table is included if any column header contains the word "code" (e.g. "Codes", "CPT Code", "HCPCS Code"). Tables with ICD-10 code headers are excluded.

**Code type classification** — Each code is classified as CPT or HCPCS. If the source column header explicitly says "HCPCS" or "CPT", that label is used. Otherwise the code value is pattern-matched: a letter followed by four digits (e.g. `E0100`) is HCPCS; five digits (e.g. `86003`) is CPT.

**Unified output** — Different policy PDFs contain different table formats (Testing Limitations, Unit of Service, Item Description, etc.). All non-code columns from every table found in a PDF are merged into a single flat row structure with empty values where a column does not apply. This makes the output easy to filter and pivot.

**Newline splitting and deduplication** — When a single table cell contains multiple codes separated by newlines, each code is extracted as its own row. Duplicate rows within a table are removed.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your browser.

## Usage

1. Click **Load Policies** to fetch the list of NC Medicaid clinical coverage policies
2. Select one or more policies using the checkboxes
3. Click **Extract Code Tables** to download and parse the selected PDFs
4. View extracted codes in the results table (grouped by policy, with Code, Code Type, and all applicable columns)
5. Click **Download CSV** to export results as a single flat CSV

Not all policies contain code tables — those are reported as "No code tables found."

## Tech Stack

- **Flask** — web framework and API routes
- **pdfplumber** — PDF table extraction
- **requests** — HTTP client for fetching the policy listing page and PDF downloads
- **BeautifulSoup** — HTML parsing for scraping policy links
