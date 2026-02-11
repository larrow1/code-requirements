# NC Medicaid Testing Limitations Extractor

Extract CPT/HCPCS code testing limitations from NC Medicaid clinical coverage policy PDFs.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your browser.

## Usage

1. Click **Load Policies** to fetch the list of NC Medicaid clinical coverage policies
2. Select one or more policies using the checkboxes
3. Click **Extract Testing Limitations** to download and parse the selected PDFs
4. View extracted codes and limitations in the results table
5. Click **Download CSV** to export results

Not all policies contain a Testing Limitations table — those are reported as "No table found."
