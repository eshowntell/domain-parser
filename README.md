# FTJ Site Audit Tools
**franketobeyjones.com — Content & Structure Audit**

---

## What this produces

| Output file | Contents |
|---|---|
| `output/ftj_site_audit.xlsx` | 5-tab Excel: Inventory · Page Text · Similar Pages · Navigation · Summary |
| `output/ftj_site_audit.html` | Standalone searchable dashboard (open in any browser, no server needed) |

---

## Setup (one-time)

### 1. Python 3.10+
```
python --version   # must be 3.10 or higher
```

### 2. Install dependencies
```bash
cd ftj-audit
pip install -r requirements.txt
```

### 3. Create a WordPress Application Password
1. Log in to **WP Admin → Users → Your Profile**
2. Scroll to **Application Passwords**
3. Type a name (e.g. `Site Audit`) and click **Add New Application Password**
4. Copy the generated password (looks like `AbCd EfGh IjKl MnOp QrSt Uv`)

### 4. Edit `config.py`
```python
SITE_URL        = "https://franketobeyjones.com"
WP_USERNAME     = "your_actual_wp_username"
WP_APP_PASSWORD = "AbCd EfGh IjKl MnOp QrSt Uv"   # paste as-is
```

---

## Running the scripts

Run them in order. Each step saves intermediate data to `data/` so you can
re-run any single step without starting over.

### Step 1 — Crawl: build URL inventory
```bash
python 1_crawl.py
```
- Discovers all pages, posts, and custom post types via WP REST API
- Supplements with sitemap.xml
- Saves → `data/01_pages.json`
- Typical time: 2–5 minutes for 800 pages

### Step 2 — Extract: pull content from each page
```bash
python 2_extract.py
```
- Uses `content.rendered` from WP REST API (clean, no boilerplate)
- Falls back to live-page scrape for page-builder pages
- Also extracts navigation/menu structure from homepage
- Saves → `data/02_content.json`, `data/02_menus.json`
- **Resume-safe**: restarts from where it left off if interrupted
- Typical time: 20–60 minutes for 800 pages (0.5s delay per page)

### Step 3 — GA4 merge: add traffic data
```bash
# Export from GA4 first (see instructions below), then:
python 3_ga4_merge.py --lifetime ga4_all_time.csv --ninety ga4_90days.csv

# Or skip traffic data for now:
python 3_ga4_merge.py
```
- Saves → `data/03_merged.json`

#### How to export from GA4
1. Go to **GA4 → Reports → Engagement → Pages and screens**
2. Set date range to **All time**
3. Click the ⬇ download icon (top right of the report) → **Download CSV**
4. Rename file to `ga4_all_time.csv` and place in the `ftj-audit/` folder
5. Repeat with **Last 90 days** → rename to `ga4_90days.csv`

### Step 4 — Similarity: find duplicate/redundant content
```bash
python 4_similarity.py
```
- Runs TF-IDF cosine similarity across all pages
- Flags pairs scoring ≥ 0.70 (configurable in `config.py`)
- Saves → `data/04_clusters.json`
- Typical time: 1–3 minutes

### Step 5 — Report: build the final deliverables
```bash
python 5_report.py
```
- Builds Excel + HTML
- Saves → `output/ftj_site_audit.xlsx`, `output/ftj_site_audit.html`
- Typical time: under 1 minute

---

## Typical total run time
~30–60 minutes for 800 pages (most time is in Step 2 due to the polite 0.5s
request delay). You can lower `REQUEST_DELAY` in `config.py` if the server can
handle it.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 Unauthorized` from WP API | Re-check `WP_USERNAME` and `WP_APP_PASSWORD` in config.py. Username is the login name, not display name. |
| Content looks like shortcodes `[vc_row]...` | Site uses WPBakery / Elementor. Script falls back to live scrape automatically, but check `source` column in output. |
| GA4 column not detected | Set `GA4_PATH_COLUMN`, `GA4_SESSIONS_COLUMN` manually in config.py |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` again |
| Step 2 stalls on a page | It auto-retries 3× then marks as `scrape_failed` and moves on |

---

## Output columns reference

**URL Inventory tab**
| Column | Description |
|---|---|
| URL | Full page URL |
| H1 (Best) | Live H1 > content H1 > WP title, in that priority order |
| Status | publish / draft / private / trash |
| Date Modified | Last edit date from WP |
| Word Count | Words in body text (boilerplate excluded) |
| Sessions (Lifetime) | Total GA4 sessions since site launch |
| Sessions (90 Days) | GA4 sessions in the last 90 days |

**Similar Pages tab** — Score legend:
- **≥ 0.90** (red): Near-duplicate — strong consolidation or redirect candidate
- **0.80–0.89** (yellow): Highly similar — review for content overlap
- **0.70–0.79** (blue): Moderately similar — may share a topic area

---

## File structure
```
ftj-audit/
├── config.py          ← edit this first
├── utils.py           ← shared HTTP helpers (don't edit)
├── requirements.txt
├── README.md
├── 1_crawl.py
├── 2_extract.py
├── 3_ga4_merge.py
├── 4_similarity.py
├── 5_report.py
├── data/              ← created automatically (intermediate files)
│   ├── 01_pages.json
│   ├── 02_content.json
│   ├── 02_menus.json
│   ├── 03_merged.json
│   └── 04_clusters.json
└── output/            ← created automatically (final deliverables)
    ├── ftj_site_audit.xlsx
    └── ftj_site_audit.html
```
