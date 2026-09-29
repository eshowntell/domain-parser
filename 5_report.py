#!/usr/bin/env python3
"""
5_report.py  —  Build the final audit deliverables.

Outputs:
  output/ftj_site_audit.xlsx   — 5-tab Excel workbook
  output/ftj_site_audit.html   — Standalone searchable HTML dashboard

Excel tabs:
  1. URL Inventory   — every page with key metadata + traffic
  2. Page Content    — full extracted text per page
  3. Similar Pages   — duplicate/near-duplicate content clusters
  4. Navigation      — master menu items and links
  5. Summary         — counts, quick stats

HTML Dashboard:
  — Single file (no external dependencies needed if offline)
  — Searchable, sortable table
  — Tab navigation between the four views
"""

import json
import os
import sys
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.formatting.rule import ColorScaleRule

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("5_report")

# ── Date-age colour helper ─────────────────────────────────────
from datetime import date as _date

def date_age_months(date_str: str) -> int | None:
    if not date_str:
        return None
    try:
        d = _date.fromisoformat(date_str[:10])
        today = _date.today()
        return (today.year - d.year) * 12 + (today.month - d.month)
    except Exception:
        return None

# Excel fills — semi-opaque achieved by mixing with white
DATE_FILLS = [
    (3,  PatternFill("solid", fgColor="BDD7EE")),   # ≤3 mo  — blue
    (6,  PatternFill("solid", fgColor="C6EFCE")),   # ≤6 mo  — green
    (12, PatternFill("solid", fgColor="FFEB9C")),   # ≤12 mo — yellow
    (18, PatternFill("solid", fgColor="FCE4D6")),   # ≤18 mo — orange
    (24, PatternFill("solid", fgColor="FFC7CE")),   # ≤24 mo — red
    (9999, PatternFill("solid", fgColor="808080")), # 24+    — dark grey
]

def date_fill(date_str: str):
    months = date_age_months(date_str)
    if months is None:
        return None
    for threshold, fill in DATE_FILLS:
        if months <= threshold:
            return fill
    return DATE_FILLS[-1][1]


def _avg_sess_per_day(sessions_lifetime: int, date_created: str):
    """
    Returns avg sessions/day since the page was created.
    Values < 1 return the string '<1'; missing data returns 0.
    Used for both the Excel cell value and the HTML JSON field.
    """
    if not date_created or not sessions_lifetime:
        return 0
    try:
        created = _date.fromisoformat(date_created[:10])
        days = (_date.today() - created).days
        if days <= 0:
            return sessions_lifetime
        avg = sessions_lifetime / days
        return "<1" if avg < 1 else int(round(avg))
    except Exception:
        return 0


def _compact_today() -> str:
    d = _date.today()
    return f"{d.month}/{d.day}/{str(d.year)[2:]}"


def _load_ga_dates() -> tuple:
    """Returns (ga_start, ga_end) as M/D/YY display strings."""
    try:
        meta = load_json(GA_META_FILE, fallback={})
        ga_start = (meta or {}).get("ga_start") or "1/1/22"
        ga_end   = (meta or {}).get("ga_end")   or _compact_today()
        return ga_start, ga_end
    except Exception:
        return "1/1/22", _compact_today()


def page_score(page: dict) -> tuple:
    """
    Score a page 0–100 for migration worthiness.
    Returns (score: int, label: str) where label is one of:
      Migrate / Improve / Review / Archive

    Weighting rationale
    ───────────────────
    Engagement quality (45 pts combined) is the strongest signal post-
    Google Helpful Content updates. Traffic (30 pts) is a lagging
    indicator — useful but not disqualifying on its own. Content depth
    (25 pts) provides the floor: a well-written page with no traffic
    history is still Review-worthy, not Archive. Images score modestly
    (7 pts) — they aid UX and conversion on a senior living site but are
    a weak direct ranking signal without verified alt text.

    Key interactions
    ────────────────
    • High traffic + low engagement (<30 %) gets a 0.75× penalty.
      The page is drawing but not serving users — migrating it carries
      that problem forward.
    • Rich content (600+ words) + high dwell time (≥90s) gets a 1.12×
      bonus: content quality confirmed by behaviour data.
    • Rich content + zero traffic gets a 1.06× bump: substance is a
      positive signal even without traffic history.
    • Zero-traffic pages get neutral scores on engagement metrics
      (12/25 and 10/20) rather than zero — we simply don't have the
      data; they shouldn't be penalised for a missing signal.
    """
    sessions = int(page.get("sessions_lifetime", 0) or 0)
    engaged  = int(page.get("engaged_sessions",  0) or 0)
    avg_time = float(page.get("avg_engagement_time", 0) or 0)
    words    = int(page.get("word_count",  0) or 0)
    images   = int(page.get("image_count", 0) or 0)

    has_traffic = sessions > 0

    # A. Traffic (0–30) — log-scaled
    if   sessions == 0:      t = 0
    elif sessions <= 25:     t = 5
    elif sessions <= 100:    t = 12
    elif sessions <= 500:    t = 19
    elif sessions <= 2000:   t = 25
    elif sessions <= 10000:  t = 28
    else:                    t = 30

    # B. Engaged-session rate (0–25)
    if not has_traffic:
        e = 12                              # neutral — no data
    else:
        rate = engaged / sessions
        if   rate < 0.25: e = 2
        elif rate < 0.40: e = 8
        elif rate < 0.55: e = 14
        elif rate < 0.70: e = 18
        else:             e = 25

    # C. Average engagement time (0–20)
    # GA4 benchmarks: <30 s ≈ bounce, 60 s = decent, 90 s+ = quality
    if not has_traffic:
        g = 10                              # neutral — no data
    else:
        if   avg_time < 15:  g = 1
        elif avg_time < 30:  g = 4
        elif avg_time < 60:  g = 9
        elif avg_time < 90:  g = 13
        elif avg_time < 150: g = 16
        else:                g = 20

    # D. Content depth — words (0–18)
    # Google HCU penalises thin pages; 600+ is a healthy floor
    if   words == 0:    w = 0
    elif words < 100:   w = 4
    elif words < 300:   w = 9
    elif words < 600:   w = 13
    elif words < 1000:  w = 16
    else:               w = 18

    # E. Images (0–7) — UX / conversion signal, weak direct rank signal
    if   images == 0:  i = 0
    elif images == 1:  i = 2
    elif images <= 3:  i = 4
    elif images <= 6:  i = 6
    else:              i = 7

    raw = t + e + g + w + i    # theoretical max = 100

    # Interaction: penalty — high traffic, low engagement
    if has_traffic and sessions > 100:
        eng_rate = engaged / sessions
        if eng_rate < 0.30:
            raw = int(raw * 0.75)

    # Interaction: bonus — rich content quality confirmed (or implied)
    if words >= 600:
        if has_traffic and avg_time >= 90:
            raw = int(raw * 1.12)   # confirmed by dwell data
        elif not has_traffic:
            raw = int(raw * 1.06)   # implied by substance alone

    score = max(0, min(100, raw))

    if   score >= 72: label = "Migrate"
    elif score >= 48: label = "Improve"
    elif score >= 25: label = "Review"
    else:             label = "Archive"

    return score, label

# ── Input paths ────────────────────────────────────────────────
DATA_PAGES    = os.path.join(config.DATA_DIR, "03_merged.json")
DATA_CONTENT  = os.path.join(config.DATA_DIR, "02_content.json")
DATA_MENUS    = os.path.join(config.DATA_DIR, "02_menus.json")
DATA_CLUSTERS = os.path.join(config.DATA_DIR, "04_clusters.json")
GA_META_FILE  = os.path.join(config.DATA_DIR, "03_ga_meta.json")

XL_OUT   = os.path.join(config.OUTPUT_DIR, "ftj_site_audit.xlsx")
HTML_OUT = os.path.join(config.OUTPUT_DIR, "ftj_site_audit.html")


# ── Excel helpers ──────────────────────────────────────────────

HEADER_FILL   = PatternFill("solid", fgColor="1F3864")   # dark navy
HEADER_FONT   = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BODY_FONT     = Font(name="Arial", size=9)
ALT_FILL      = PatternFill("solid", fgColor="F2F2F2")

STATUS_FILLS = {
    "publish":  PatternFill("solid", fgColor="C6EFCE"),   # green
    "draft":    PatternFill("solid", fgColor="FFEB9C"),   # yellow
    "private":  PatternFill("solid", fgColor="FFC7CE"),   # red
    "pending":  PatternFill("solid", fgColor="FFEB9C"),
    "trash":    PatternFill("solid", fgColor="E0E0E0"),   # grey
    "future":   PatternFill("solid", fgColor="DDEBF7"),
}

GRADE_FILLS = {
    "Migrate": PatternFill("solid", fgColor="C6EFCE"),   # green
    "Improve": PatternFill("solid", fgColor="DDEBF7"),   # blue
    "Review":  PatternFill("solid", fgColor="FFEB9C"),   # yellow
    "Archive": PatternFill("solid", fgColor="FFC7CE"),   # red/pink
}

GRADE_EMOJI = {
    "Migrate": "✅", "Improve": "🔵", "Review": "🟡", "Archive": "❌",
}

THIN = Side(style="thin", color="D0D0D0")
THIN_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def write_header_row(ws, headers: list[str], row: int = 1):
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
        cell.border = THIN_BORDER


def style_data_rows(ws, start_row: int, end_row: int, num_cols: int):
    for row in range(start_row, end_row + 1):
        fill = ALT_FILL if row % 2 == 0 else None
        for col in range(1, num_cols + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            if fill:
                cell.fill = fill
            cell.alignment = Alignment(vertical="top", wrap_text=False)


def auto_width(ws, min_w=10, max_w=60):
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            try:
                val = str(cell.value or "")
                max_len = max(max_len, len(val.split("\n")[0]))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(min_w, min(max_w, max_len + 2))


def add_table(ws, ref: str, name: str, style: str = "TableStyleMedium2"):
    table = Table(displayName=name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name=style, showFirstColumn=False,
        showLastColumn=False, showRowStripes=True, showColumnStripes=False
    )
    ws.add_table(table)


def load_json(path: str, fallback=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    log.warning(f"  Missing: {path}")
    return fallback


# ── TAB 1: URL Inventory ───────────────────────────────────────

def build_inventory_tab(wb: Workbook, pages: list[dict], ga_start: str, ga_end: str):
    ws = wb.create_sheet("URL Inventory")
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 20

    sess_label = f"Sessions {ga_start}–{ga_end}"

    headers = [
        "URL", "Title", "H1 (Best)", "Type", "Status",
        "Grade", "Score",
        "Date Created", "Date Modified",
        "Word Count", "Image Count",
        "Internal Links", "External Links",
        sess_label, "Engaged Sessions", "Avg Eng Time (s)",
        "Pageviews (Lifetime)",
        "Avg Sessions/Day", "Pageviews (90 Days)",
        "Slug", "Template",
    ]
    write_header_row(ws, headers)

    for r, page in enumerate(pages, start=2):
        score, label = page_score(page)
        row = [
            page.get("url", ""),
            page.get("title", ""),
            page.get("h1_best", "") or page.get("h1_title", ""),
            page.get("type", ""),
            page.get("status", ""),
            f"{GRADE_EMOJI.get(label, '')} {label}",
            score,
            page.get("date_created", ""),
            page.get("date_modified", ""),
            page.get("word_count", 0),
            page.get("image_count", 0),
            len(page.get("internal_links", [])),
            len(page.get("external_links", [])),
            page.get("sessions_lifetime", 0),
            page.get("engaged_sessions", 0),
            round(float(page.get("avg_engagement_time", 0) or 0), 1),
            page.get("pageviews_lifetime", 0),
            _avg_sess_per_day(page.get("sessions_lifetime", 0), page.get("date_created", "")),
            page.get("pageviews_90d", 0),
            page.get("slug", ""),
            page.get("template", ""),
        ]
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="top")
            # URL column: blue link colour (col 1)
            if c == 1 and val:
                cell.font = Font(name="Arial", size=9, color="1155CC")

        # Status colour (col 5)
        status = page.get("status", "")
        if status in STATUS_FILLS:
            ws.cell(row=r, column=5).fill = STATUS_FILLS[status]

        # Grade colour (col 6)
        if label in GRADE_FILLS:
            ws.cell(row=r, column=6).fill = GRADE_FILLS[label]

        # Date Modified colour (col 9) — age-based
        fill = date_fill(page.get("date_modified", ""))
        if fill:
            ws.cell(row=r, column=9).fill = fill

    style_data_rows(ws, 2, len(pages) + 1, len(headers))

    # Traffic heatmap on lifetime sessions column only (col 14)
    # Avg Sessions/Day (col 18) may contain '<1' strings — skip heatmap there
    last_row = len(pages) + 1
    for col in (14,):
        col_letter = get_column_letter(col)
        ws.conditional_formatting.add(
            f"{col_letter}2:{col_letter}{last_row}",
            ColorScaleRule(
                start_type="num",   start_value=0,    start_color="FFFFFF",
                mid_type="num",     mid_value=100,    mid_color="FFEB9C",
                end_type="max",                       end_color="63BE7B",
            )
        )

    auto_width(ws)
    add_table(ws, f"A1:{get_column_letter(len(headers))}{max(2, len(pages)+1)}", "InventoryTable")
    log.info(f"  Tab 'URL Inventory': {len(pages)} rows")
    return ws


# ── TAB 2: Page Content ────────────────────────────────────────

def build_content_tab(wb: Workbook, pages: list[dict]):
    ws = wb.create_sheet("Page Content")
    ws.freeze_panes = "A2"

    headers = ["URL", "Title", "H1 (Best)", "Status", "Word Count",
           "Headings (H2–H4)", "Image URLs", "Video URLs", "Full Body Text"]
    write_header_row(ws, headers)

    for r, page in enumerate(pages, start=2):
        # Headings summary (H2–H4 only)
        headings = page.get("headings", [])
        h_summary = " | ".join(
            f"[{h['level'].upper()}] {h['text']}"
            for h in headings if h.get("level") in ("h2", "h3", "h4")
        )[:500]

        text = page.get("body_text", "")[:32000]   # Excel cell limit ~32K chars

        row_vals = [
            page.get("url", ""),
            page.get("title", ""),
            page.get("h1_best", "") or page.get("h1_title", ""),
            page.get("status", ""),
            page.get("word_count", 0),
            h_summary,
            "\n".join(page.get("image_urls", [])),   # ← new
            "\n".join(page.get("video_urls", [])),   # ← new
            text,
        ]
        for c, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=(c == 7))
            cell.font = Font(name="Arial", size=9,
                             color="1155CC" if c == 1 else "000000")

    # Wider text column
    ws.column_dimensions["G"].width = 80
    ws.column_dimensions["H"].width = 80
    ws.column_dimensions["I"].width = 80
    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 35
    ws.column_dimensions["C"].width = 35
    for col in ("D", "E", "F"):
        ws.column_dimensions[col].width = 15

    log.info(f"  Tab 'Page Content': {len(pages)} rows")
    return ws


# ── TAB 3: Similar Pages ───────────────────────────────────────

def build_similarity_tab(wb: Workbook, sim_data: dict | None):
    ws = wb.create_sheet("Similar Pages")
    ws.freeze_panes = "A2"

    if not sim_data:
        ws["A1"] = "Similarity analysis not run. Execute 4_similarity.py first."
        return ws

    all_pairs = sim_data.get("all_pairs", [])
    clusters  = sim_data.get("clusters", [])

    # Section A: All similar pairs
    ws["A1"] = "ALL SIMILAR PAGE PAIRS"
    ws["A1"].font = Font(name="Arial", bold=True, size=11, color="1F3864")

    pair_headers = ["Score", "Cluster ID", "URL A", "Title A", "URL B", "Title B"]
    write_header_row(ws, pair_headers, row=2)

    # Build URL → cluster map
    url_cluster: dict[str, int] = {}
    for cl in clusters:
        for url in cl["urls"]:
            url_cluster[url] = cl["cluster_id"]

    row = 3
    for pair in all_pairs:
        score = pair.get("score", 0)
        fill = PatternFill("solid", fgColor=(
            "FFC7CE" if score >= 0.90 else
            "FFEB9C" if score >= 0.80 else
            "DDEBF7"
        ))
        vals = [
            round(score, 3),
            url_cluster.get(pair.get("url_a", ""), ""),
            pair.get("url_a", ""),
            pair.get("title_a", ""),
            pair.get("url_b", ""),
            pair.get("title_b", ""),
        ]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            cell.fill = fill
        row += 1

    auto_width(ws)
    ws.column_dimensions["C"].width = 50
    ws.column_dimensions["E"].width = 50

    # Section B: Cluster summary further down
    row += 2
    ws.cell(row=row, column=1, value="CLUSTER SUMMARY").font = Font(
        name="Arial", bold=True, size=11, color="1F3864"
    )
    row += 1
    cl_headers = ["Cluster ID", "Pages in Cluster", "Max Similarity", "URLs"]
    write_header_row(ws, cl_headers, row=row)
    row += 1
    for cl in clusters:
        ws.cell(row=row, column=1, value=cl["cluster_id"]).font = BODY_FONT
        ws.cell(row=row, column=2, value=cl["page_count"]).font = BODY_FONT
        ws.cell(row=row, column=3, value=cl["max_similarity"]).font = BODY_FONT
        ws.cell(row=row, column=4, value="\n".join(cl["urls"])).font = Font(name="Arial", size=8)
        ws.row_dimensions[row].height = max(15, 12 * len(cl["urls"]))
        row += 1

    log.info(f"  Tab 'Similar Pages': {len(all_pairs)} pairs, {len(clusters)} clusters")
    return ws


# ── TAB 4: Navigation ──────────────────────────────────────────

def build_nav_tab(wb: Workbook, menus: list[dict] | None):
    ws = wb.create_sheet("Navigation")
    ws.freeze_panes = "A2"

    if not menus:
        ws["A1"] = "Menu data not available. Check that 2_extract.py ran successfully."
        return ws

    headers = ["Menu Name", "Depth", "Label", "URL", "Parent Label", "Target"]
    write_header_row(ws, headers)

    cur_menu = None
    for r, item in enumerate(menus, start=2):
        if item.get("menu_name") != cur_menu:
            cur_menu = item["menu_name"]
        depth = item.get("depth", 0)
        indent = "  " * depth
        vals = [
            item.get("menu_name", ""),
            depth,
            indent + item.get("label", ""),
            item.get("url", ""),
            item.get("parent_text", ""),
            item.get("target", ""),
        ]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            if c == 4:
                cell.font = Font(name="Arial", size=9, color="1155CC")

        if depth == 0:
            for c in range(1, 7):
                ws.cell(row=r, column=c).font = Font(name="Arial", size=9, bold=True)

    auto_width(ws)
    add_table(ws, f"A1:{get_column_letter(len(headers))}{max(2, len(menus)+1)}", "NavTable")
    log.info(f"  Tab 'Navigation': {len(menus)} menu items")
    return ws

# ── TAB migration ─────────────────────────────────────────────

def build_migration_tab(wb: Workbook, pages: list[dict]):
    ws = wb.create_sheet("Migration Plan")
    ws.freeze_panes = "A2"

    headers = [
        "URL (Old)",
        "Redirect From",        # explicit old path for .htaccess export
        "New Slug",
        "New Title",
        "Layout Hint",
        "Parent Slug",
        "Priority",
        "Note",
        "Grade",
        "Score",
        "Sessions (LT)",
        "Word Count",
        "Images",
        "Has Video",
    ]
    write_header_row(ws, headers)

    for r, page in enumerate(pages, start=2):
        score, label = page_score(page)
        row = [
            page.get("url", ""),
            page.get("redirect_from", page.get("url", "")),
            page.get("new_slug", ""),
            page.get("new_title", ""),
            page.get("layout_hint", ""),
            page.get("parent_slug", ""),
            page.get("priority", ""),
            page.get("note", ""),
            f"{GRADE_EMOJI.get(label,'')} {label}",
            score,
            page.get("sessions_lifetime", 0),
            page.get("word_count", 0),
            page.get("image_count", 0),
            "yes" if page.get("video_urls") else "",
        ]
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="top",
                                       wrap_text=(c == 8))  # wrap Note column
            if c == 1:
                cell.font = Font(name="Arial", size=9, color="1155CC")
        if label in GRADE_FILLS:
            ws.cell(row=r, column=9).fill = GRADE_FILLS[label]

    # Widen the editable columns so they're usable
    ws.column_dimensions["A"].width = 50
    ws.column_dimensions["C"].width = 35   # new_slug
    ws.column_dimensions["D"].width = 35   # new_title
    ws.column_dimensions["H"].width = 40   # note

    auto_width(ws)
    add_table(ws, f"A1:{get_column_letter(len(headers))}{max(2, len(pages)+1)}", "MigrationTable")
    log.info(f"  Tab 'Migration Plan': {len(pages)} rows")
    return ws

# ── TAB 5: Summary ─────────────────────────────────────────────

def build_summary_tab(wb: Workbook, pages: list[dict], sim_data: dict | None):
    ws = wb.create_sheet("Summary", 0)   # First tab

    def section(title, row):
        cell = ws.cell(row=row, column=1, value=title)
        cell.font = Font(name="Arial", bold=True, size=12, color="1F3864")
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
        ws.merge_cells(f"A{row}:C{row}")
        return row + 1

    def kv(label, value, row, note=""):
        ws.cell(row=row, column=1, value=label).font  = Font(name="Arial", bold=True, size=10)
        ws.cell(row=row, column=2, value=value).font  = Font(name="Arial", size=10)
        if note:
            ws.cell(row=row, column=3, value=note).font = Font(name="Arial", size=9, italic=True, color="666666")
        return row + 1

    r = 1
    ws.cell(row=r, column=1, value=f"FTJ Site Audit — {config.SITE_URL}").font = \
        Font(name="Arial", bold=True, size=14, color="1F3864")
    ws.merge_cells(f"A{r}:F{r}")
    r += 1
    ws.cell(row=r, column=1, value=f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}").font = \
        Font(name="Arial", size=9, italic=True, color="666666")
    r += 2

    # ── Page counts ────────────────────────────────────────────
    r = section("PAGE INVENTORY", r)

    by_status = {}
    by_type   = {}
    for p in pages:
        by_status[p.get("status","?")] = by_status.get(p.get("status","?"), 0) + 1
        by_type[p.get("type","?")]     = by_type.get(p.get("type","?"), 0) + 1

    r = kv("Total URLs found", len(pages), r)
    for s, c in sorted(by_status.items()):
        r = kv(f"  Status: {s}", c, r)
    r += 1
    for t, c in sorted(by_type.items()):
        r = kv(f"  Type: {t}", c, r)
    r += 1

    # ── Content health ────────────────────────────────────────
    r = section("CONTENT HEALTH", r)
    no_text   = sum(1 for p in pages if p.get("word_count", 0) == 0)
    thin      = sum(1 for p in pages if 0 < p.get("word_count", 0) < 100)
    avg_words = int(sum(p.get("word_count", 0) for p in pages) / max(1, len(pages)))
    total_img = sum(p.get("image_count", 0) for p in pages)
    no_h1     = sum(1 for p in pages if not (p.get("h1_best") or p.get("h1_title", "")))

    r = kv("Pages with no extractable text",    no_text,   r, "review manually")
    r = kv("Thin pages (< 100 words)",          thin,      r)
    r = kv("Average word count",                avg_words, r)
    r = kv("Total images across all pages",     total_img, r)
    r = kv("Pages missing H1",                  no_h1,     r)
    r += 1

    # ── Traffic ───────────────────────────────────────────────
    r = section("TRAFFIC (GA4)", r)
    lt_total = sum(p.get("sessions_lifetime", 0) for p in pages)
    d90_total = sum(p.get("sessions_90d", 0) for p in pages)
    zero_traffic = sum(1 for p in pages if p.get("sessions_lifetime", 0) == 0 and p.get("status") == "publish")

    r = kv("Total sessions (lifetime)",   lt_total,     r, "0 = GA4 not merged yet")
    r = kv("Total sessions (90 days)",    d90_total,    r)
    r = kv("Published pages: zero traffic", zero_traffic, r, "possible candidates for removal")

    # Top 10 pages by lifetime sessions
    top10 = sorted(pages, key=lambda x: x.get("sessions_lifetime", 0), reverse=True)[:10]
    if any(p.get("sessions_lifetime", 0) > 0 for p in top10):
        r += 1
        ws.cell(row=r, column=1, value="Top 10 Pages by Lifetime Sessions").font = \
            Font(name="Arial", bold=True, size=10)
        r += 1
        for p in top10:
            ws.cell(row=r, column=1, value=p.get("url","")).font = Font(name="Arial", size=9, color="1155CC")
            ws.cell(row=r, column=2, value=p.get("sessions_lifetime",0)).font = Font(name="Arial", size=9)
            r += 1
    r += 1

    # ── Similarity ────────────────────────────────────────────
    r = section("DUPLICATE / SIMILAR CONTENT", r)
    if sim_data:
        pairs   = sim_data.get("all_pairs", [])
        near_dupe = sum(1 for p in pairs if p["score"] >= 0.90)
        high_sim  = sum(1 for p in pairs if 0.80 <= p["score"] < 0.90)
        mod_sim   = sum(1 for p in pairs if 0.70 <= p["score"] < 0.80)
        r = kv("Near-duplicate pairs (≥ 0.90)",  near_dupe, r, "strong consolidation candidates")
        r = kv("High similarity pairs (0.80–0.89)", high_sim, r)
        r = kv("Moderate similarity (0.70–0.79)",  mod_sim,  r)
        r = kv("Total similarity clusters",       len(sim_data.get("clusters",[])), r)
    else:
        r = kv("Similarity data", "Not available — run 4_similarity.py", r)

    # ── Column widths ─────────────────────────────────────────

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 35
    log.info("  Tab 'Summary' built")
    return ws



# ── WP Artifact types ─────────────────────────────────────────
ARTIFACT_TYPES = {
    "wp_template","wp_template_part","wp_global_styles","wp_navigation",
    "wp_font_family","wp_font_face","wp_block","nav_menu_item",
    "bold-timeline","sitemap-only",
}

def is_artifact(page):
    t, url, st = page.get("type",""), page.get("url",""), page.get("status","")
    return t in ARTIFACT_TYPES or "?post_type=" in url or st in ("auto-draft","inherit")


def build_html(pages, menus, sim_data):
    from datetime import datetime as _dt
    import json as _j

    ga_start, ga_end = _load_ga_dates()
    generated = _dt.now().strftime("%Y-%m-%d %H:%M")
    site_url  = config.SITE_URL

    # ── Build URL meta for enriching similarity pairs ──────────
    url_meta = {p.get("url","").rstrip("/"): {
        "sess":p.get("sessions_lifetime",0),"words":p.get("word_count",0),
        "modified":p.get("date_modified","")} for p in pages}

    # ── INV (no body text — keeps JSON small) ──────────────────
    inv_rows = []
    for p in pages:
        score, label = page_score(p)
        inv_rows.append({
            "url":      p.get("url",""),
            "title":    p.get("title",""),
            "h1":       p.get("h1_best","") or p.get("h1_title",""),
            "type":     p.get("type",""),
            "status":   p.get("status",""),
            "grade":    label,
            "score":    score,
            "modified": p.get("date_modified",""),
            "words":    p.get("word_count",0),
            "images":   p.get("image_count",0),
            "sess_lt":  p.get("sessions_lifetime",0),
            "engaged":  p.get("engaged_sessions",0),
            "avg_time": round(float(p.get("avg_engagement_time",0) or 0), 1),
            "avg_spd":  _avg_sess_per_day(p.get("sessions_lifetime",0), p.get("date_created","")),
            "artifact": is_artifact(p),
                "image_urls":   p.get("image_urls", []),
                "video_urls":   p.get("video_urls", []),
                "redirect_from": p.get("redirect_from", p.get("url", "")),
                "new_slug":     p.get("new_slug", ""),
                "new_title":    p.get("new_title", ""),
                "layout_hint":  p.get("layout_hint", ""),
                "parent_slug":  p.get("parent_slug", ""),
                "note":         p.get("note", ""),
                "priority":     p.get("priority", ""),
            })

    # ── Body text in separate lookup (url -> text) ─────────────
    text_map = {}
    for p in pages:
        t = (p.get("body_text","") or "")[:10000]
        if t:
            text_map[p.get("url","")] = t

    # ── Similar pairs ──────────────────────────────────────────
    pair_rows = []
    if sim_data:
        for pair in (sim_data.get("all_pairs") or [])[:500]:
            ua,ub = pair.get("url_a","").rstrip("/"), pair.get("url_b","").rstrip("/")
            ma,mb = url_meta.get(ua,{}), url_meta.get(ub,{})
            sa,sb = ma.get("sess",0), mb.get("sess",0)
            wa,wb = ma.get("words",0), mb.get("words",0)
            score = round(pair.get("score",0),3)
            if   score>=0.97: rec="🔴 Near-identical — redirect one"
            elif score>=0.90: rec="🟠 Strong duplicate — consolidate"
            elif score>=0.80: rec="🟡 High overlap — review & merge"
            else:             rec="🔵 Similar topic"
            if   sa>sb*1.5:   keep="Keep A (traffic)"
            elif sb>sa*1.5:   keep="Keep B (traffic)"
            elif wa>wb*1.5:   keep="Keep A (content)"
            elif wb>wa*1.5:   keep="Keep B (content)"
            else:             keep="Toss-up"
            pair_rows.append({
                "score":score,"rec":rec,"keep":keep,
                "url_a":pair.get("url_a",""),"title_a":pair.get("title_a",""),
                "sess_a":sa,"words_a":wa,"mod_a":ma.get("modified",""),
                "url_b":pair.get("url_b",""),"title_b":pair.get("title_b",""),
                "sess_b":sb,"words_b":wb,"mod_b":mb.get("modified",""),
            })

    # ── Nav ────────────────────────────────────────────────────
    nav_rows = []
    if menus:
        for m in menus:
            nav_rows.append({"menu":m.get("menu_name",""),"depth":m.get("depth",0),
                "label":m.get("label",""),"url":m.get("url",""),"parent":m.get("parent_text","")})
    else:
        nav_items = sorted([p for p in pages if p.get("type")=="nav_menu_item"],key=lambda x:x.get("menu_order",0))
        id_title  = {p.get("wp_id"):p.get("title","") for p in nav_items}
        for p in nav_items:
            pt = id_title.get(p.get("parent_id")) if p.get("parent_id") else ""
            nav_rows.append({"menu":"WP Menu (API)","depth":1 if p.get("parent_id") else 0,
                "label":p.get("title",""),"url":p.get("url",""),"parent":pt or ""})

    # Replace </ with <\/ so </script> in page text can't close the HTML script tag
    def _jsafe(obj):
        s = _j.dumps(obj, ensure_ascii=False)
        return (s.replace("&", "\\u0026")
                 .replace("<", "\\u003c")
                 .replace(">", "\\u003e")
                 .replace("\u2028", "\\u2028")   # ← add these two
                 .replace("\u2029", "\\u2029"))  # ← add these two



    inv_json  = _jsafe(inv_rows)
    text_json = _jsafe(text_map)
    pair_json = _jsafe(pair_rows)
    nav_json  = _jsafe(nav_rows)

    return _build_template(site_url, generated, ga_start, ga_end, inv_json, text_json, pair_json, nav_json), inv_json, text_json, pair_json, nav_json


def _build_template(site_url, generated, ga_start, ga_end, inv_json, text_json, pair_json, nav_json):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>FTJ Site Audit</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;font-size:13px;background:#f0f2f5;color:#222;padding-bottom:56px}
/* sticky chrome */
#chrome{position:sticky;top:0;z-index:200;box-shadow:0 2px 8px rgba(0,0,0,.3)}
#header{background:#1F3864;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:12px}
#header h1{font-size:16px;font-weight:bold;flex:1}
#header .sub{font-size:10px;opacity:.65}
.hbtn{padding:5px 12px;border:1px solid rgba(255,255,255,.4);border-radius:4px;font-size:11px;
      cursor:pointer;background:rgba(255,255,255,.1);color:#fff}
.hbtn:hover{background:rgba(255,255,255,.2)}
.hbtn-red{border-color:#e74c3c;background:#c0392b}.hbtn-red:hover{background:#e74c3c}
#tabs{background:#163060;display:flex;padding:0;overflow-x:auto}
.tab{padding:13px 0;cursor:pointer;color:rgba(255,255,255,.6);font-size:13px;font-weight:700;
     border-bottom:3px solid transparent;white-space:nowrap;user-select:none;transition:all .15s;
     flex:1;text-align:center;letter-spacing:.01em}
.tab:hover{color:rgba(255,255,255,.9);background:rgba(255,255,255,.07)}
.tab.active{color:#fff;border-bottom-color:#5B9BD5;background:rgba(255,255,255,.12)}
.tab .tc{display:inline-block;background:rgba(255,255,255,.2);border-radius:8px;
         padding:1px 6px;font-size:10px;margin-left:4px}
#controls{background:#fff;border-bottom:2px solid #dde;padding:8px 14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#controls input{padding:6px 10px;border:1px solid #ccc;border-radius:4px;font-size:12px;width:220px}
select{padding:5px 8px;border:1px solid #ccc;border-radius:4px;font-size:12px;background:#fff}
.legend{display:flex;gap:5px;align-items:center;font-size:10px;color:#555}
.leg{width:11px;height:11px;border-radius:2px;flex-shrink:0}
/* sel-bar — lives above the table inside the panel, not in sticky chrome */
#sel-bar{background:#e8f4fd;border:1px solid #bee3f8;border-radius:6px;padding:6px 12px;
         font-size:11px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}
#sel-bar strong{color:#1F3864}
.sbtn{padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;border:1px solid}
.sbtn-keep{background:#d4edda;color:#155724;border-color:#c3e6cb}.sbtn-keep:hover{background:#c3e6cb}
.sbtn-dismiss{background:#f8d7da;color:#721c24;border-color:#f5c6cb}.sbtn-dismiss:hover{background:#f5c6cb}
.sbtn-clear{background:#e2e3e5;color:#383d41;border-color:#d6d8db}.sbtn-clear:hover{background:#d6d8db}
/* quick filters — now inline within #controls */
.qf{padding:2px 9px;border-radius:10px;font-size:11px;cursor:pointer;border:1px solid #ccc;
    background:#fff;white-space:nowrap;transition:all .15s}
.qf:hover,.qf.active{background:#1F3864;color:#fff;border-color:#1F3864}
/* content */
#content{padding:12px 16px}
.panel{display:none}.panel.active{display:block}
/* stats strip — single compact summary row */
#stats-strip{display:flex;gap:0;flex-wrap:wrap;margin-bottom:8px;background:#fff;
  border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}
.ss-item{padding:5px 14px;font-size:11px;cursor:pointer;border-right:1px solid #eee;
  white-space:nowrap;display:flex;align-items:center;gap:5px;transition:background .12s}
.ss-item:last-child{border-right:none}
.ss-item:hover,.ss-item.ss-active{background:#f0f4ff}
.ss-n{font-weight:bold;font-size:14px;color:#1F3864;line-height:1}
.ss-l{color:#777;font-size:10px;margin-top:1px}
.ss-item.ss-warn .ss-n{color:#e74c3c}
.ss-item.ss-info .ss-n{color:#27ae60}
.ss-item.ss-art  .ss-n{color:#e67e22}
/* table */
#tbl-wrap{max-height:calc(100vh - 200px);overflow-y:auto;overflow-x:auto;border-radius:6px}
table{border-collapse:collapse;width:100%;background:#fff;min-width:1000px}
th{background:#1F3864;color:#fff;padding:7px 9px;text-align:left;font-size:11px;font-weight:bold;
   position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap;z-index:2}
th:hover{background:#2E75B6}
td{padding:5px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:11px}
tr:hover td{background:#f0f4ff!important}
tr.selected td{background:#dceeff!important}
tr.kept-row td{background:#f0fff4!important}
tr.dismissed-row{display:none}
tr.artifact-row td{opacity:.55}
a{color:#1155cc;text-decoration:none}a:hover{text-decoration:underline}
.badge{display:inline-block;padding:1px 5px;border-radius:8px;font-size:10px;font-weight:bold}
.bp{background:#C6EFCE;color:#276221}.bd{background:#FFEB9C;color:#7d6608}
.bpr{background:#FFC7CE;color:#9c0006}.bt{background:#e0e0e0;color:#555}
.ba{background:#fde8c8;color:#874d00}
/* date age */
.dc{display:inline-block;padding:2px 6px;border-radius:3px;font-size:11px}
.da0{background:rgba(59,130,246,.35);color:#1e3a5f}.da1{background:rgba(34,197,94,.35);color:#14532d}
.da2{background:rgba(234,179,8,.4);color:#713f12}.da3{background:rgba(249,115,22,.4);color:#7c2d12}
.da4{background:rgba(239,68,68,.4);color:#7f1d1d}.da5{background:rgba(80,80,80,.5);color:#f0f0f0}
/* row action btns */
.rbtn{border:none;border-radius:3px;padding:2px 6px;cursor:pointer;font-size:10px;margin:0 1px}
.rbtn-k{background:#d4edda;color:#155724}.rbtn-k:hover{background:#b8e0c0}
.rbtn-d{background:#f8d7da;color:#721c24}.rbtn-d:hover{background:#f0b8bc}
.rbtn-t{background:#1F3864;color:#fff}.rbtn-t:hover{background:#2E75B6}
/* notice */
.notice{background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:7px 12px;
        font-size:11px;color:#664d03;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:8px}
/* drawer */
#drawer{position:fixed;top:0;right:-560px;width:540px;height:100vh;background:#fff;
        box-shadow:-4px 0 24px rgba(0,0,0,.2);z-index:1000;transition:right .25s ease;display:flex;flex-direction:column}
#drawer.open{right:0}
#dhdr{background:#1F3864;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-shrink:0}
#dhdr .dt{font-size:13px;font-weight:bold;line-height:1.4}
#dhdr .du{font-size:10px;opacity:.8;word-break:break-all;margin-top:2px}
#dclose{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;line-height:1;flex-shrink:0;padding:0 4px}
#dmeta{padding:7px 16px;background:#f4f6f9;border-bottom:1px solid #ddd;font-size:11px;display:flex;gap:12px;flex-wrap:wrap;flex-shrink:0}
#dnotes{padding:8px 16px;border-bottom:1px solid #eee;flex-shrink:0}
#dnotes label{font-size:10px;color:#666;display:block;margin-bottom:4px}
#dnotes textarea{width:100%;border:1px solid #ccc;border-radius:4px;padding:6px;font-size:11px;resize:vertical;min-height:52px}
#dbody{flex:1;overflow-y:auto;padding:16px;font-size:12px;line-height:1.8;color:#333;white-space:pre-wrap;word-break:break-word}
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.15);z-index:999}
#overlay.open{display:block}
/* sim */
.rec-r{color:#9c0006;font-weight:bold}.rec-o{color:#b45309;font-weight:bold}.rec-y{color:#713f12}.rec-b{color:#1e3a5f}
.keep-tag{background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-size:10px}
/* nav tree */
.nav-sec{background:#fff;border-radius:8px;padding:12px 16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.nav-sec h3{font-size:12px;font-weight:bold;color:#1F3864;margin-bottom:8px;padding-bottom:5px;border-bottom:1px solid #eee}
.tree-row{display:flex;align-items:center;padding:3px 0;font-size:11px}
.tree-row a{color:#1155cc}.tree-row .url-hint{color:#aaa;font-size:10px;margin-left:8px}
/* nav sub-tabs */
.stab-bar{display:flex;border-bottom:2px solid #dde;margin-bottom:12px;background:#f4f6f9;border-radius:6px 6px 0 0;overflow:hidden}
.stab{padding:9px 20px;cursor:pointer;font-size:12px;font-weight:600;color:#666;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}
.stab:hover{color:#1F3864;background:rgba(31,56,100,.06)}
.stab.active{color:#1F3864;border-bottom-color:#2E75B6;background:#fff}
.sub-panel{display:none}.sub-panel.active{display:block}
/* remap helper */
.remap-node{display:flex;align-items:center;gap:8px;padding:5px 8px;font-size:11px;border-bottom:1px solid #f0f0f0}
.remap-node.missing{background:#fff5f5;border-left:3px solid #e74c3c;padding-left:8px;color:#9c0006;font-style:italic}
.remap-node.kept{background:#f0fff4;border-left:3px solid #27ae60;padding-left:8px}
.remap-node.dismissed{background:#fff8f8;border-left:3px solid #e74c3c;opacity:.6}
.remap-slug{border:1px solid #ccc;border-radius:3px;padding:2px 6px;font-size:11px;width:300px;font-family:monospace;color:#1F3864}
.remap-slug:focus{border-color:#2E75B6;outline:none;background:#f0f8ff}
.remap-orig{color:#aaa;font-size:10px;margin-left:4px;text-decoration:line-through}
.orphan-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;margin-top:8px}
.oc{background:#fff;border-radius:6px;padding:9px 12px;font-size:11px;box-shadow:0 1px 3px rgba(0,0,0,.08);border-left:4px solid #e74c3c}
.oc.has-t{border-left-color:#f39c12}.oc.art{border-left-color:#95a5a6}
.oc-r{font-size:10px;color:#888;margin-top:3px}
/* keeper/dismissed list */
.decision-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:8px;margin-top:8px}
.dec-card{background:#fff;border-radius:6px;padding:10px 14px;font-size:11px;box-shadow:0 1px 3px rgba(0,0,0,.08);border-left:4px solid #27ae60;position:relative}
.dec-card.dis{border-left-color:#e74c3c}
.dec-card .dc-title{font-weight:bold;margin-bottom:2px}
.dec-card .dc-url{color:#1155cc;font-size:10px;word-break:break-all}
.dec-card .dc-note{color:#666;font-size:10px;margin-top:4px;font-style:italic}
.dec-card .dc-undo{position:absolute;top:8px;right:10px;background:none;border:1px solid #ccc;border-radius:3px;padding:1px 6px;font-size:10px;cursor:pointer}
/* drawer media section */
#dmedia{padding:10px 16px;border-bottom:1px solid rgba(255,255,255,.12)}
#dmedia .dm-label{font-size:10px;font-weight:bold;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.dm-thumbs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.dm-thumb{width:72px;height:56px;object-fit:cover;border-radius:4px;border:1px solid rgba(255,255,255,.15);cursor:pointer;transition:opacity .15s}
.dm-thumb:hover{opacity:.8}
.dm-video-list{display:flex;flex-direction:column;gap:4px}
.dm-video-list a{color:#7ec8f8;font-size:10px;word-break:break-all;text-decoration:none}
.dm-video-list a:hover{text-decoration:underline}
.dm-none{color:rgba(255,255,255,.3);font-size:10px;font-style:italic}
/* bottom bar */
#bbar{position:fixed;bottom:0;left:0;right:0;background:#1F3864;color:#fff;padding:7px 16px;
      display:flex;align-items:center;gap:12px;font-size:11px;z-index:300;box-shadow:0 -2px 8px rgba(0,0,0,.2)}
#bbar span{opacity:.7}
</style>
</head>
<body>
<div id="chrome">
<div id="header">
  <div>
    <div class="sub">""" + site_url + """ &nbsp;·&nbsp; Generated """ + generated + """</div>
    <h1>FTJ Site Audit Dashboard</h1>
  </div>
  <button class="hbtn" onclick="showDismissed()" id="btn-show-dismissed">👁 Show dismissed</button>
  <button class="hbtn" onclick="exportKept()">⬇ Export kept/selected</button>
  <button class="hbtn" onclick="exportDecisions()">⬇ Export all decisions</button>
  <label class="hbtn" style="cursor:pointer">⬆ Import <input type="file" accept=".csv" onchange="importDecisions(event)" style="display:none" /></label>
  <button class="hbtn hbtn-red" onclick="clearAll()">🗑 Clear all</button>
</div>
<div id="tabs">
  <div class="tab active" onclick="switchTab('inventory',this)">📋 URL Inventory <span class="tc" id="tc-inv">0</span></div>
  <div class="tab" onclick="switchTab('similar',this)">🔄 Similar Pages <span class="tc" id="tc-sim">0</span></div>
  <div class="tab" onclick="switchTab('keeper',this)">✅ Keeper List <span class="tc" id="tc-keep">0</span></div>
  <div class="tab" onclick="switchTab('dismissed',this)">🗑 Dismissed <span class="tc" id="tc-dis">0</span></div>
  <div class="tab" onclick="switchTab('nav',this)">🗂 Navigation</div>
  <div class="tab" onclick="switchTab('remap',this)">🗺 Remap Helper</div>
</div>
<div id="controls">
  <input type="text" id="search" placeholder="Search URLs, titles, text…" oninput="filterTable()" />
  <select id="status-f" onchange="filterTable()"><option value="">All statuses</option>
    <option>publish</option><option>draft</option><option>private</option><option>trash</option></select>
  <select id="type-f" onchange="filterTable()"><option value="">All types</option></select>
  <select id="artifact-f" onchange="filterTable()">
    <option value="">All (incl. artifacts)</option>
    <option value="real">Real pages only</option>
    <option value="artifact">Artifacts only</option>
  </select>
  <div class="legend" id="dl">
    <b style="font-size:10px">Modified:</b>
    <span class="leg" style="background:rgba(59,130,246,.5)"></span><span>≤3mo</span>
    <span class="leg" style="background:rgba(34,197,94,.5)"></span><span>≤6mo</span>
    <span class="leg" style="background:rgba(234,179,8,.5)"></span><span>≤12mo</span>
    <span class="leg" style="background:rgba(249,115,22,.5)"></span><span>≤18mo</span>
    <span class="leg" style="background:rgba(239,68,68,.5)"></span><span>≤24mo</span>
    <span class="leg" style="background:rgba(80,80,80,.5)"></span><span>24mo+</span>
  </div>
  <span id="qfilters" style="display:flex;gap:4px;flex-wrap:wrap;align-items:center;margin-left:6px">
    <button class="qf" id="qf-orphan" onclick="quickFilter('orphan')">🔍 Orphans <span id="qfc-orphan"></span></button>
    <button class="qf" id="qf-blog" onclick="quickFilter('blog')">📝 Posts <span id="qfc-blog"></span></button>
    <button class="qf" id="qf-notraffic" onclick="quickFilter('notraffic')">📉 No Traffic <span id="qfc-notraffic"></span></button>
    <button class="qf" id="qf-migrate" onclick="quickFilter('migrate')">✅ Migrate <span id="qfc-migrate"></span></button>
    <button class="qf" id="qf-archive" onclick="quickFilter('archive')">❌ Archive <span id="qfc-archive"></span></button>
    <button class="qf" id="qf-kept" onclick="quickFilter('kept')">⭐ Kept <span id="qfc-kept"></span></button>
  </span>
  <span id="row-count" style="margin-left:auto;font-size:11px;color:#666"></span>
</div>
</div><!-- #chrome -->

<!-- Drawer -->
<div id="overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div id="dhdr">
    <div><div class="dt" id="d-title"></div>
    <div class="du"><a id="d-url" href="#" target="_blank" style="color:rgba(255,255,255,.8)"></a></div></div>
    <button id="dclose" onclick="closeDrawer()">✕</button>
  </div>
  <div id="dmeta"></div>
  <div id="dmedia"></div>
  <div id="dnotes"><label>📝 Notes (auto-saved)</label>
    <textarea id="d-note-ta" placeholder="Add notes about this page…" oninput="saveNote(this)"></textarea>
  </div>
  <div id="dbody"></div>
</div>

<div id="content">

<!-- URL INVENTORY -->
<div id="panel-inventory" class="panel active">
  <div id="stats-strip"></div>
  <div id="bold-notice" class="notice" style="display:none">
    ⚠ <b>bold-timeline</b> entries are decade-slide plugin components, not real pages.
    <button onclick="document.getElementById('artifact-f').value='real';filterTable()"
      style="background:#ffc107;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px">Show pages only</button>
    <button onclick="this.parentElement.style.display='none'" style="background:none;border:none;cursor:pointer;font-size:18px">✕</button>
  </div>
  <div id="sel-bar">
    <span>Selected: <strong id="sel-n">0</strong></span>
    <span>Sessions (sel): <strong id="sel-sess">0</strong></span>
    <span>Kept: <strong id="kept-n">0</strong></span>
    <span>Dismissed: <strong id="dis-n">0</strong></span>
    <button class="sbtn sbtn-keep" onclick="bulkKeep()">✅ Keep selected</button>
    <button class="sbtn sbtn-dismiss" onclick="bulkDismiss()">🗑 Dismiss selected</button>
    <button class="sbtn sbtn-clear" onclick="clearSel()">✕ Clear selection</button>
  </div>
  <div id="tbl-wrap">
  <table>
    <thead><tr>
      <th style="width:24px"><input type="checkbox" id="sel-all" onchange="selAll(this.checked)" title="Select all visible" /></th>
      <th style="width:22px">#</th>
      <th style="width:64px"></th>
      <th onclick="sortInv(3)">URL ↕</th>
      <th onclick="sortInv(4)">Title ↕</th>
      <th onclick="sortInv(5)">Type ↕</th>
      <th onclick="sortInv(6)">Status ↕</th>
      <th onclick="sortInv(7)">Grade ↕</th>
      <th onclick="sortInv(8)">Modified ↕</th>
      <th onclick="sortInv(9)">Words ↕</th>
      <th onclick="sortInv(10)">Images ↕</th>
      <th onclick="sortInv(11)">Sessions """ + ga_start + """–""" + ga_end + """ ↕</th>
      <th onclick="sortInv(12)">Engaged ↕</th>
      <th onclick="sortInv(13)">Avg Time(s) ↕</th>
    </tr></thead>
    <tbody id="inv-body"></tbody>
  </table>
  </div>
</div>

<!-- SIMILAR PAGES -->
<div id="panel-similar" class="panel">
  <p style="font-size:11px;color:#555;margin-bottom:8px">💡 Score 1.0 = identical text. Auto-recommendation based on traffic & word count.</p>
  <div id="tbl-wrap-sim">
  <table>
    <thead><tr>
      <th>Score</th><th>Recommendation</th><th>Keep</th>
      <th>URL A</th><th>Title A</th><th>Sess A</th><th>Words A</th><th>Mod A</th>
      <th>URL B</th><th>Title B</th><th>Sess B</th><th>Words B</th><th>Mod B</th>
    </tr></thead>
    <tbody id="sim-body"></tbody>
  </table>
  </div>
</div>

<!-- KEEPER LIST -->
<div id="panel-keeper" class="panel">
  <p style="font-size:11px;color:#555;margin-bottom:10px">Pages you've marked ✅ Keep. These will be recreated in the new site.</p>
  <div id="keeper-grid" class="decision-grid"></div>
  <p id="keeper-empty" style="color:#888;padding:20px;text-align:center">No pages kept yet. Use the ✅ buttons in URL Inventory.</p>
</div>

<!-- DISMISSED -->
<div id="panel-dismissed" class="panel">
  <p style="font-size:11px;color:#555;margin-bottom:10px">Pages marked for removal. Click Undo to restore.</p>
  <div id="dismissed-grid" class="decision-grid"></div>
  <p id="dismissed-empty" style="color:#888;padding:20px;text-align:center">No pages dismissed yet.</p>
</div>

<!-- NAV & ORPHANS -->
<div id="panel-nav" class="panel">
  <div class="stab-bar">
    <div class="stab active" onclick="switchNavTab('navtree',this)">🌿 Nav Tree</div>
    <div class="stab" onclick="switchNavTab('orphans',this)">🔍 Orphans <span id="orphan-stab-count" style="font-size:10px;color:#e74c3c;font-weight:normal"></span></div>
    <div class="stab" onclick="switchNavTab('sitemap',this)">🗺 Visual Sitemap</div>
  </div>
  <!-- Nav Tree sub-panel -->
  <div id="subpanel-navtree" class="sub-panel active">
    <div style="background:#fff;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:11px;color:#555;box-shadow:0 1px 3px rgba(0,0,0,.08)">
      <b>Reading this view:</b> The tree shows your nav menus with indented sub-items.
    </div>
    <div id="nav-api-notice" class="notice" style="display:none">
      ℹ Nav data from WP API records. Re-run <code>2_extract.py</code> for the fully-rendered menu tree.
    </div>
    <div id="nav-tree-wrap"></div>
  </div>
  <!-- Orphans sub-panel -->
  <div id="subpanel-orphans" class="sub-panel">
    <div style="font-size:10px;color:#666;margin-bottom:8px;padding:8px 0">
      Every published real page not linked from any menu. &nbsp;
      <span style="border-left:4px solid #e74c3c;padding-left:5px;margin-right:10px">No menu link, no traffic</span>
      <span style="border-left:4px solid #f39c12;padding-left:5px;margin-right:10px">No menu link, has traffic</span>
      <span style="border-left:4px solid #95a5a6;padding-left:5px">Artifact</span>
    </div>
    <h3 style="font-size:13px;color:#1F3864;margin-bottom:6px">🔍 Orphan &amp; Missing Pages <span id="orphan-hdr" style="font-weight:normal;color:#666;font-size:11px"></span></h3>
    <div id="orphan-grid" class="orphan-grid"></div>
  </div>
  <!-- Visual Sitemap sub-panel -->
  <div id="subpanel-sitemap" class="sub-panel">
    <div style="background:#fff;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:11px;color:#555;box-shadow:0 1px 3px rgba(0,0,0,.08)">
      All published pages arranged by URL structure. <span style="color:#e74c3c;font-weight:bold">Red = missing parent page</span> (has child pages but no page at that path itself).
    </div>
    <div id="sitemap-tree"></div>
  </div>
</div>

<!-- REMAP HELPER -->
<div id="panel-remap" class="panel">
  <div style="background:#fff;border-radius:6px;padding:12px 16px;margin-bottom:12px;font-size:11px;color:#555;box-shadow:0 1px 3px rgba(0,0,0,.08)">
    <b>🗺 Redirect / Remap Helper</b> — Your <b>Kept</b> pages arranged by URL hierarchy.
    Set a new path for any page. <span style="background:#fff5f5;color:#9c0006;padding:1px 5px;border-radius:3px;font-weight:bold">Red rows</span> = missing parent pages (child pages exist but no page at that path — e.g. <code>/blog</code> has dozens of posts but no landing page).
    Export when ready.
  </div>
  <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
    <button class="sbtn sbtn-keep" onclick="exportHtaccess()">&darr; Export .htaccess (301s)</button>
    <button class="sbtn sbtn-dismiss" onclick="exportWpRedirection()">&darr; Export WP Redirection CSV</button>
    <span id="remap-summary" style="font-size:11px;color:#888"></span>
  </div>
  <div id="remap-tree" style="background:#fff;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden"></div>
  <p id="remap-empty" style="color:#888;padding:20px;text-align:center;display:none">No pages kept yet. Mark pages ✅ Keep in URL Inventory first.</p>
</div>

</div><!-- #content -->

<div id="bbar">
  <span>💾 Decisions auto-save to browser storage</span>
  <span id="bbar-count" style="background:rgba(255,255,255,.2);padding:1px 8px;border-radius:8px"></span>
  <span style="margin-left:auto;opacity:.4;font-size:10px">key: ftj-audit-v2</span>
</div>

<script>
const INV = """ + inv_json + """;
const TEXT = """ + text_json + """;
const PAIR = """ + pair_json + """;
const NAV = """ + nav_json + """;

// ── Persistence ──────────────────────────────────────────────
const LS = 'ftj-audit-v2';
let D = {};
try { D = JSON.parse(localStorage.getItem(LS)||'{}'); } catch(e){}
function persist(){ try{localStorage.setItem(LS,JSON.stringify(D));}catch(e){} updateCounts(); }
function getD(url){ return D[url]||(D[url]={}); }

// Row button handlers (use data attrs to avoid URL escaping issues)
function rowKeep(btn){ const url=btn.dataset.url, v=btn.dataset.val==='true'; setKept(url,v); }
function rowDismiss(btn){ const url=btn.dataset.url, v=btn.dataset.val==='true'; setDismissed(url,v); }
function rowOpen(btn){ const i=parseInt(btn.dataset.idx); openDrawer(INV_F[i],btn.closest('tr')); }

function setKept(url,v){
  getD(url).kept=v; if(v) delete getD(url).dismissed;
  persist(); refreshDecisionLists(); renderInventory(INV_F);
}
function setDismissed(url,v){
  getD(url).dismissed=v; if(v) delete getD(url).kept;
  persist(); refreshDecisionLists(); renderInventory(INV_F);
}
function saveNote(ta){ getD(ta.dataset.url).note=ta.value; persist(); }
function clearAll(){ if(!confirm('Clear ALL decisions? Cannot undo.')) return; D={}; localStorage.removeItem(LS); persist(); renderInventory(INV_F); refreshDecisionLists(); }

function updateCounts(){
  const kept=Object.values(D).filter(d=>d.kept).length;
  const dis=Object.values(D).filter(d=>d.dismissed).length;
  document.getElementById('kept-n').textContent=kept;
  document.getElementById('dis-n').textContent=dis;
  document.getElementById('tc-keep').textContent=kept;
  document.getElementById('tc-dis').textContent=dis;
  document.getElementById('bbar-count').textContent=kept+' kept · '+dis+' dismissed';
}

// ── Export / Import ──────────────────────────────────────────
function exportKept(){
  const rows=[['URL','Title','Type','Status','Words','Sessions LT','Note']];
  INV.forEach(r=>{
    const d=D[r.url]||{};
    if(d.kept || SELECTED.has(r.url)) rows.push([r.url,r.title,r.type,r.status,r.words,r.sess_lt,d.note||'']);
  });
  dlCsv(rows,'ftj_keeper_list.csv');
}
function exportDecisions(){
  const rows=[['URL','Title','Decision','Note']];
  INV.forEach(r=>{ const d=D[r.url]||{}; if(d.kept||d.dismissed) rows.push([r.url,r.title,d.kept?'Keep':'Dismiss',d.note||'']); });
  dlCsv(rows,'ftj_decisions.csv');
}
function dlCsv(rows,fname){
  const csv=rows.map(r=>r.map(c=>'"'+String(c||'').replace(/"/g,'""')+'"').join(',')).join('\\r\\n');
  const a=document.createElement('a'); a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download=fname; a.click();
}
function importDecisions(evt){
  const f=evt.target.files[0]; if(!f) return;
  const rd=new FileReader(); rd.onload=e=>{
    e.target.result.trim().split('\\n').slice(1).forEach(line=>{
      const c=line.match(/"(?:[^"]|"")*"|[^,]*/g)||[];
      const un=s=>s.replace(/^"|"$/g,'').replace(/""/g,'"');
      const url=un(c[0]||''), dec=un(c[2]||''), note=un(c[3]||'');
      if(url){ getD(url).note=note; if(dec==='Keep') getD(url).kept=true; else if(dec==='Dismiss') getD(url).dismissed=true; }
    });
    persist(); renderInventory(INV_F); refreshDecisionLists(); alert('Imported.');
  }; rd.readAsText(f);
}
function showDismissed(){
  const b=document.getElementById('btn-show-dismissed');
  document.querySelectorAll('tr.dismissed-row').forEach(r=>{ r.style.display=r.style.display==='none'?'table-row':'none'; });
  b.textContent=b.textContent.includes('Show')?'👁 Hide dismissed':'👁 Show dismissed';
}

// ── Selection ────────────────────────────────────────────────
let SELECTED=new Set();
function selAll(v){ INV_F.forEach(r=>{ if(v) SELECTED.add(r.url); else SELECTED.delete(r.url); }); renderInventory(INV_F); }
function toggleSel(url){ if(SELECTED.has(url)) SELECTED.delete(url); else SELECTED.add(url); updateSelBar(); }
function clearSel(){ SELECTED.clear(); renderInventory(INV_F); }
function bulkKeep(){ SELECTED.forEach(url=>{ getD(url).kept=true; delete getD(url).dismissed; }); persist(); SELECTED.clear(); renderInventory(INV_F); refreshDecisionLists(); }
function bulkDismiss(){ SELECTED.forEach(url=>{ getD(url).dismissed=true; delete getD(url).kept; }); persist(); SELECTED.clear(); renderInventory(INV_F); refreshDecisionLists(); }
function updateSelBar(){
  const sel=INV_F.filter(r=>SELECTED.has(r.url));
  document.getElementById('sel-n').textContent=sel.length;
  document.getElementById('sel-sess').textContent=fmt(sel.reduce((s,r)=>s+(r.sess_lt||0),0));
}

// ── Quick filters ────────────────────────────────────────────
let QF=null;
function quickFilter(f){ QF=(QF===f)?null:f; document.querySelectorAll('.qf').forEach(b=>b.classList.remove('active')); if(QF) document.getElementById('qf-'+QF).classList.add('active'); filterTable(); }
function navUrls(){ return new Set(NAV.map(r=>r.url.replace(/\/$/,''))); }
// ── Date helpers ─────────────────────────────────────────────
function dageCls(d){ if(!d) return ''; const m=(Date.now()-new Date(d))/(1000*60*60*24*30.44); return m<=3?'da0':m<=6?'da1':m<=12?'da2':m<=18?'da3':m<=24?'da4':'da5'; }
function dcell(d){
  if(!d) return '—';
  const p=d.split('-'); const disp=`${parseInt(p[1])}/${parseInt(p[2])}/${p[0].slice(2)}`;
  return `<span class="dc ${dageCls(d)}" title="${d}">${disp}</span>`;
}
function badge(s){ const c={publish:'bp',draft:'bd',private:'bpr',trash:'bt'}; return `<span class="badge ${c[s]||'ba'}">${s||'—'}</span>`; }
function fmt(n){ return n?Number(n).toLocaleString():'0'; }

// ── Drawer ───────────────────────────────────────────────────
function openDrawer(row, tr){
  document.querySelectorAll('tr.selected').forEach(r=>r.classList.remove('selected'));
  if(tr) tr.classList.add('selected');
  document.getElementById('d-title').textContent=row.title||row.h1||'(no title)';
  const lnk=document.getElementById('d-url'); lnk.href=row.url; lnk.textContent=row.url;
  document.getElementById('dmeta').innerHTML=
    `<span>Status: ${badge(row.status)}</span><span>Words: ${fmt(row.words)}</span>`+
    `<span>Sessions: ${fmt(row.sess_lt)}</span><span>Modified: ${row.modified||'—'}</span>`;

  // ── Media section ────────────────────────────────────────────
  const imgs  = (row.image_urls  || []).filter(Boolean);
  const vids  = (row.video_urls  || []).filter(Boolean);
  let mediaHtml = '';
  if(imgs.length){
    mediaHtml += `<div class="dm-label">🖼 Images (${imgs.length})</div><div class="dm-thumbs">`;
    imgs.forEach(src=>{
      mediaHtml += `<a href="${src}" target="_blank" title="${src}">` +
        `<img class="dm-thumb" src="${src}" loading="lazy" onerror="this.style.display='none'" /></a>`;
    });
    mediaHtml += '</div>';
  }
  if(vids.length){
    mediaHtml += `<div class="dm-label">🎬 Videos / Embeds (${vids.length})</div><div class="dm-video-list">`;
    vids.forEach(v=>{ mediaHtml += `<a href="${v}" target="_blank">${v}</a>`; });
    mediaHtml += '</div>';
  }
  if(!imgs.length && !vids.length){
    mediaHtml = '<span class="dm-none">No media detected for this page</span>';
  }
  document.getElementById('dmedia').innerHTML = mediaHtml;

  const ta=document.getElementById('d-note-ta'); ta.dataset.url=row.url; ta.value=(D[row.url]||{}).note||'';
  document.getElementById('dbody').textContent=TEXT[row.url]||'(no text extracted for this page)';
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('open');
}
function closeDrawer(){
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('open');
  document.querySelectorAll('tr.selected').forEach(r=>r.classList.remove('selected'));
}

// ── Render inventory ─────────────────────────────────────────
let INV_F=INV;
function renderInventory(rows){
  INV_F=rows;
  document.getElementById('inv-body').innerHTML=rows.map((r,i)=>{
    const d=D[r.url]||{}, isK=d.kept, isDis=d.dismissed, isSel=SELECTED.has(r.url);
    const rowCls=(isK?'kept-row':'')+(isDis?' dismissed-row':'')+(isSel?' selected':'');
    const disp=isDis?'style="display:none"':'';
    return `<tr class="${rowCls}" ${disp}>
      <td><input type="checkbox" ${isSel?'checked':''} data-url="${r.url}" onchange="toggleSel(this.dataset.url);renderInventory(INV_F)" /></td>
      <td style="color:#999;text-align:right">${i+1}</td>
      <td>
        <button class="rbtn rbtn-k" data-url="${r.url}" data-val="${!isK}" onclick="rowKeep(this)" title="${isK?'Un-keep':'Keep'}">${isK?'★':'✅'}</button>
        <button class="rbtn rbtn-d" data-url="${r.url}" data-val="${!isDis}" onclick="rowDismiss(this)" title="${isDis?'Restore':'Dismiss'}">${isDis?'↩':'🗑'}</button>
        <button class="rbtn rbtn-t" data-idx="${i}" onclick="rowOpen(this)" title="Read text">📄</button>
      </td>
      <td><a href="${r.url}" target="_blank">${r.url}</a></td>
      <td>${r.title}</td>
      <td><code style="font-size:10px">${r.type}</code>${r.artifact?' <span class="badge ba">artifact</span>':''}</td>
      <td>${badge(r.status)}</td>
      <td style="white-space:nowrap;font-weight:bold;color:${r.grade==='Migrate'?'#27ae60':r.grade==='Improve'?'#2E75B6':r.grade==='Review'?'#b45309':'#c0392b'}">${r.grade==='Migrate'?'✅':r.grade==='Improve'?'🔵':r.grade==='Review'?'🟡':'❌'} ${r.grade} <small style="color:#aaa;font-weight:normal">${r.score}</small></td>
      <td>${dcell(r.modified)}</td>
      <td>${fmt(r.words)}</td>
      <td>${fmt(r.images)}</td>
      <td>${fmt(r.sess_lt)}</td>
      <td>${fmt(r.engaged)}</td>
      <td>${r.avg_time>0?r.avg_time:'—'}</td>
    </tr>`;
  }).join('');
  document.getElementById('row-count').textContent=rows.length+' rows';
  updateSelBar(); updateCounts();
}

function renderSimilar(rows){
  document.getElementById('sim-body').innerHTML=rows.map(r=>`<tr>
    <td style="font-weight:bold;color:${r.score>=0.9?'#9c0006':r.score>=0.8?'#b45309':'#1e3a5f'}">${r.score}</td>
    <td class="${r.rec.startsWith('🔴')?'rec-r':r.rec.startsWith('🟠')?'rec-o':r.rec.startsWith('🟡')?'rec-y':'rec-b'}">${r.rec}</td>
    <td><span class="keep-tag">${r.keep}</span></td>
    <td><a href="${r.url_a}" target="_blank">${r.url_a}</a></td><td>${r.title_a}</td>
    <td>${fmt(r.sess_a)}</td><td>${fmt(r.words_a)}</td><td>${dcell(r.mod_a)}</td>
    <td><a href="${r.url_b}" target="_blank">${r.url_b}</a></td><td>${r.title_b}</td>
    <td>${fmt(r.sess_b)}</td><td>${fmt(r.words_b)}</td><td>${dcell(r.mod_b)}</td>
  </tr>`).join('');
  document.getElementById('row-count').textContent=rows.length+' pairs';
}

function refreshDecisionLists(){
  const kept=INV.filter(r=>(D[r.url]||{}).kept);
  const dis=INV.filter(r=>(D[r.url]||{}).dismissed);
  const kGrid=document.getElementById('keeper-grid');
  const dGrid=document.getElementById('dismissed-grid');
  kGrid.innerHTML=kept.map(r=>`<div class="dec-card">
    <div class="dc-title">${r.title||'(no title)'}</div>
    <div class="dc-url"><a href="${r.url}" target="_blank">${r.url}</a></div>
    <div style="font-size:10px;color:#666;margin-top:3px">${fmt(r.sess_lt)} sessions · ${fmt(r.words)} words</div>
    ${(D[r.url]||{}).note?`<div class="dc-note">📝 ${D[r.url].note}</div>`:''}
    <button class="dc-undo" data-url="${r.url}" onclick="setKept(this.dataset.url,false)">↩ Undo</button>
  </div>`).join('');
  dGrid.innerHTML=dis.map(r=>`<div class="dec-card dis">
    <div class="dc-title">${r.title||'(no title)'}</div>
    <div class="dc-url"><a href="${r.url}" target="_blank">${r.url}</a></div>
    <div style="font-size:10px;color:#666;margin-top:3px">${fmt(r.sess_lt)} sessions · ${fmt(r.words)} words</div>
    ${(D[r.url]||{}).note?`<div class="dc-note">📝 ${D[r.url].note}</div>`:''}
    <button class="dc-undo" data-url="${r.url}" onclick="setDismissed(this.dataset.url,false)">↩ Restore</button>
  </div>`).join('');
  document.getElementById('keeper-empty').style.display=kept.length?'none':'';
  document.getElementById('dismissed-empty').style.display=dis.length?'none':'';
  updateCounts();
}

function renderNav(){
  const nu=navUrls(), menus={};
  NAV.forEach(r=>{ (menus[r.menu]||(menus[r.menu]=[])).push(r); });
  const wrap=document.getElementById('nav-tree-wrap');
  if(!NAV.length){ wrap.innerHTML='<div style="color:#888;padding:20px;background:#fff;border-radius:6px">No nav data. Re-run 2_extract.py.</div>'; }
  else{
    wrap.innerHTML=Object.entries(menus).map(([name,items])=>
      `<div class="nav-sec"><h3>${name} (${items.length} items)</h3><div>${items.map(item=>
        `<div class="tree-row" style="padding-left:${item.depth*20}px">
          ${item.depth>0?'<span style="color:#bbb;margin-right:6px;font-size:12px">└─</span>':''}
          <a href="${item.url}" target="_blank">${item.label}</a>
          <span class="url-hint">${item.url}</span>
        </div>`).join('')}</div></div>`
    ).join('');
    if(NAV.length&&NAV[0].menu==='WP Menu (API)') document.getElementById('nav-api-notice').style.display='flex';
  }
  const real=INV.filter(r=>!r.artifact&&r.status==='publish');
  const orphans=real.filter(r=>!nu.has(r.url.replace(/\/$/,'')));
  document.getElementById('orphan-hdr').textContent=`(${orphans.length} of ${real.length} published real pages)`;
  document.getElementById('orphan-stab-count').textContent=`(${orphans.length})`;
  document.getElementById('orphan-grid').innerHTML=orphans.map(r=>{
    const hasT=r.sess_lt>0, cls=r.artifact?'art':hasT?'has-t':'';
    let why=r.words===0?'No text — may be a redirect or media page':
             hasT?`Has ${fmt(r.sess_lt)} sessions — reachable via direct or internal links`:
             'No traffic — likely orphaned';
    return `<div class="oc ${cls}"><a href="${r.url}" target="_blank" style="font-weight:bold">${r.title||r.url}</a>
      <div style="font-size:10px;color:#888;word-break:break-all">${r.url}</div>
      <div class="oc-r">⚠ ${why}</div></div>`;
  }).join('');
}

// ── Visual Sitemap ────────────────────────────────────────────
let _sitemapBuilt=false;
function renderSitemap(){
  if(_sitemapBuilt) return; _sitemapBuilt=true;
  const host=new URL(INV[0]&&INV[0].url||location.href).origin;
  const real=INV.filter(r=>!r.artifact&&r.status==='publish');
  // Build set of known paths
  const pageByPath={};
  real.forEach(r=>{ try{ const p=new URL(r.url).pathname.replace(/\/$/,'')||'/'; pageByPath[p]=r; }catch(e){} });
  // Collect all path segments to find missing parents
  const allPaths=new Set(Object.keys(pageByPath));
  const missingPaths=new Set();
  allPaths.forEach(p=>{
    const parts=p.split('/').filter(Boolean);
    for(let i=1;i<parts.length;i++){
      const parent='/'+parts.slice(0,i).join('/');
      if(!allPaths.has(parent)&&parent!=='/') missingPaths.add(parent);
    }
  });
  // Build tree nodes
  const allNodes=[...allPaths,...missingPaths].sort();
  // Render as indented tree
  const wrap=document.getElementById('sitemap-tree');
  const lines=allNodes.map(p=>{
    const depth=p.split('/').filter(Boolean).length;
    const r=pageByPath[p];
    const missing=missingPaths.has(p);
    const label=missing?p.split('/').pop():(r.title||p.split('/').pop()||'/');
    const sessions=r?r.sess_lt:0;
    return `<div class="remap-node ${missing?'missing':''}" style="padding-left:${8+depth*20}px">
      ${depth>0?'<span style="color:#ccc;margin-right:4px;font-size:11px;flex-shrink:0">└─</span>':''}
      ${missing
        ?`<span style="color:#e74c3c;font-weight:bold">⚠ ${label}</span><span style="font-size:10px;color:#e74c3c;margin-left:6px">[missing page — ${host}${p}]</span>`
        :`<a href="${r.url}" target="_blank" style="font-weight:${depth===0?'bold':'normal'}">${label}</a>
          <span class="url-hint">${p}</span>
          ${sessions>0?`<span style="margin-left:8px;font-size:10px;color:#27ae60;flex-shrink:0">${fmt(sessions)} sess</span>`:''}`
      }
    </div>`;
  });
  wrap.innerHTML=lines.join('')||'<div style="padding:20px;color:#888">No published pages found.</div>';
}

// ── Remap Helper ─────────────────────────────────────────────
const REMAPS={};
function setRemap(url,val){ REMAPS[url]=val.trim(); }
function renderRemap(){
  const host=new URL(INV[0]&&INV[0].url||location.href).origin;
  const kept=INV.filter(r=>(D[r.url]||{}).kept);
  const el=document.getElementById('remap-tree');
  const empty=document.getElementById('remap-empty');
  if(!kept.length){ el.style.display='none'; empty.style.display=''; return; }
  el.style.display=''; empty.style.display='none';
  // Build path→row map for kept pages
  const keptByPath={};
  kept.forEach(r=>{ try{ const p=new URL(r.url).pathname.replace(/\/$/,'')||'/'; keptByPath[p]=r; }catch(e){} });
  // Find missing parent paths within kept set
  const keptPaths=new Set(Object.keys(keptByPath));
  const missingPaths=new Set();
  keptPaths.forEach(p=>{
    const parts=p.split('/').filter(Boolean);
    for(let i=1;i<parts.length;i++){
      const parent='/'+parts.slice(0,i).join('/');
      if(!keptPaths.has(parent)&&parent!=='/') missingPaths.add(parent);
    }
  });
  // All paths sorted
  const allNodes=[...keptPaths,...missingPaths].sort();
  const totalRemaps=Object.values(REMAPS).filter(v=>v).length;
  document.getElementById('remap-summary').textContent=`${kept.length} kept pages · ${missingPaths.size} missing parents · ${totalRemaps} remaps set`;
  // Render header row
  const header=`<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:#1F3864;color:#fff;font-size:11px;font-weight:bold">
    <span style="flex:1">Current URL / Path</span>
    <span style="width:300px">New path (leave blank = keep as-is)</span>
    <span style="width:80px;text-align:right">Sessions</span>
  </div>`;
  const rows=allNodes.map(p=>{
    const r=keptByPath[p];
    const missing=missingPaths.has(p);
    const depth=p.split('/').filter(Boolean).length;
    const esc=p.replace(/"/g,'&quot;');
    if(missing){
      return `<div class="remap-node missing" style="padding-left:${8+depth*20}px">
        ${depth>0?'<span style="color:#e74c3c;margin-right:4px;flex-shrink:0">└─</span>':''}
        <span style="flex:1;color:#e74c3c">⚠ <b>${p}</b> <small>(no page here — needed as parent)</small></span>
        <span style="width:300px;font-size:10px;color:#e74c3c">Create this page in new site</span>
        <span style="width:80px"></span>
      </div>`;
    }
    const cur=REMAPS[r.url]||'';
    return `<div class="remap-node kept" style="padding-left:${8+depth*20}px">
      ${depth>0?'<span style="color:#bbb;margin-right:4px;flex-shrink:0">└─</span>':''}
      <span style="flex:1;min-width:0">
        <a href="${r.url}" target="_blank" style="font-weight:${depth===0?'bold':'normal'};font-size:11px">${r.title||p.split('/').pop()||'/'}</a>
        <span class="remap-orig">${p}</span>
      </span>
      <input class="remap-slug" placeholder="${p}" value="${cur}"
        data-url="${r.url.replace(/"/g,'&quot;')}"
        oninput="setRemap(this.dataset.url,this.value);document.getElementById('remap-summary').textContent='${kept.length} kept · ${missingPaths.size} missing · '+(Object.values(REMAPS).filter(v=>v).length)+' remaps set'" />
      <span style="width:80px;text-align:right;font-size:11px;color:#27ae60">${fmt(r.sess_lt)}</span>
    </div>`;
  });
  el.innerHTML=header+rows.join('');
}
function exportHtaccess(){
  const lines=['# FTJ Site Redirect Map — generated '+new Date().toLocaleDateString(),'# Format: Redirect 301 /old-path /new-path',''];
  INV.forEach(r=>{
    const d=D[r.url]||{};
    const newPath=REMAPS[r.url]||'';
    if(!newPath) return;
    try{ const old=new URL(r.url).pathname; lines.push(`Redirect 301 ${old} ${newPath}`); }catch(e){}
  });
  if(lines.length<=3){ alert('No remaps set yet. Fill in new paths in the table first.'); return; }
  const a=document.createElement('a'); a.href='data:text/plain;charset=utf-8,'+encodeURIComponent(lines.join('\\n'));
  a.download='ftj_redirects.htaccess'; a.click();
}
function exportWpRedirection(){
  const rows=[['source','target','regex','priority']];
  INV.forEach(r=>{
    const newPath=REMAPS[r.url]||'';
    if(!newPath) return;
    try{ const old=new URL(r.url).pathname; rows.push([old,newPath,'0','10']); }catch(e){}
  });
  if(rows.length<=1){ alert('No remaps set yet. Fill in new paths in the table first.'); return; }
  dlCsv(rows,'ftj_wp_redirection.csv');
}

// ── Filter ───────────────────────────────────────────────────
let sortCol=-1, sortAsc=true, currentPanel='inventory';
function filterTable(){
  const q=document.getElementById('search').value.toLowerCase();
  const st=document.getElementById('status-f').value;
  const ty=document.getElementById('type-f').value;
  const af=document.getElementById('artifact-f').value;
  if(currentPanel==='inventory'){
    let r=INV;
    if(st) r=r.filter(x=>x.status===st);
    if(ty) r=r.filter(x=>x.type===ty);
    if(af==='real')     r=r.filter(x=>!x.artifact);
    if(af==='artifact') r=r.filter(x=>x.artifact);
    if(QF==='orphan')   { const nu=navUrls(); r=r.filter(x=>!x.artifact&&x.status==='publish'&&!nu.has(x.url.replace(/\/$/,''))); }
    if(QF==='blog')     r=r.filter(x=>x.type==='post');
    if(QF==='notraffic')r=r.filter(x=>x.sess_lt===0&&!x.artifact&&x.status==='publish');
    if(QF==='migrate')  r=r.filter(x=>x.grade==='Migrate');
    if(QF==='archive')  r=r.filter(x=>x.grade==='Archive');
    if(QF==='kept')     r=r.filter(x=>(D[x.url]||{}).kept);
    if(q) r=r.filter(x=>(x.url+x.title+x.h1+x.type+x.status).toLowerCase().includes(q));
    renderInventory(r);
  } else if(currentPanel==='similar'){
    let r=PAIR; if(q) r=r.filter(x=>(x.url_a+x.title_a+x.url_b+x.title_b).toLowerCase().includes(q));
    renderSimilar(r);
  }
}
function switchTab(p,el){
  currentPanel=p;
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.getElementById('panel-'+p).classList.add('active'); el.classList.add('active');
  const inv=p==='inventory';
  ['status-f','type-f','artifact-f','dl'].forEach(id=>{
    const el2=document.getElementById(id); if(el2) el2.style.display=inv?'':'none';
  });
  const qf=document.getElementById('qfilters');
  if(qf) qf.style.display=inv?'flex':'none';
  closeDrawer();
  if(p==='nav'){renderNav();return;}
  if(p==='remap'){renderRemap();return;}
  if(p==='keeper'||p==='dismissed'){refreshDecisionLists();return;}
  filterTable();
}
function switchNavTab(p,el){
  document.querySelectorAll('#panel-nav .stab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('#panel-nav .sub-panel').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('subpanel-'+p).classList.add('active');
  if(p==='sitemap') renderSitemap();
}
function sortInv(col){
  if(sortCol===col) sortAsc=!sortAsc; else{sortCol=col;sortAsc=true;}
  const keys=[null,null,null,'url','title','type','status','score','modified','words','images','sess_lt','engaged','avg_time'];
  INV.sort((a,b)=>{const k=keys[col],av=a[k]??'',bv=b[k]??'';
    return(typeof av==='number'?av-bv:String(av).localeCompare(String(bv)))*(sortAsc?1:-1);});
  filterTable();
}

// ── Stats ────────────────────────────────────────────────────
function buildStats(){
  const nu=navUrls();
  const real=INV.filter(r=>!r.artifact), arts=INV.filter(r=>r.artifact);
  const pub=real.filter(r=>r.status==='publish');
  const noText=real.filter(r=>r.words===0&&r.status==='publish');
  const lt=real.reduce((s,r)=>s+(r.sess_lt||0),0);
  const orphans=pub.filter(r=>!nu.has(r.url.replace(/\/$/,'')));
  const blogs=INV.filter(r=>r.type==='post');
  const noTraff=pub.filter(r=>r.sess_lt===0);
  const migrate=INV.filter(r=>r.grade==='Migrate');
  const archive=INV.filter(r=>r.grade==='Archive');

  // Update quick filter counts
  document.getElementById('qfc-orphan').textContent='('+orphans.length+')';
  document.getElementById('qfc-blog').textContent='('+blogs.length+')';
  document.getElementById('qfc-notraffic').textContent='('+noTraff.length+')';
  document.getElementById('qfc-migrate').textContent='('+migrate.length+')';
  document.getElementById('qfc-archive').textContent='('+archive.length+')';
  document.getElementById('qfc-kept').textContent='('+Object.values(D).filter(d=>d.kept).length+')';
  document.getElementById('tc-inv').textContent=pub.length;
  document.getElementById('tc-sim').textContent=PAIR.length;

  // Compact strip — one row of clickable stat tiles
  const items=[
    {n:INV.length,   l:'Total URLs',  cls:'',        qf:''},
    {n:pub.length,   l:'Published',   cls:'',        qf:''},
    {n:arts.length,  l:'Artifacts',   cls:'ss-art',  qf:''},
    {n:noText.length,l:'No Text',     cls:'ss-warn', qf:''},
    {n:lt.toLocaleString(), l:'Sessions """ + ga_start + """–""" + ga_end + """', cls:'', qf:''},
    {n:migrate.length,l:'✅ Migrate',  cls:'ss-info', qf:'migrate'},
    {n:archive.length,l:'❌ Archive',  cls:'ss-warn', qf:'archive'},
    {n:orphans.length,l:'Orphans',    cls:'ss-warn', qf:'orphan'},
    {n:blogs.length, l:'Blog Posts',  cls:'',        qf:'blog'},
    {n:noTraff.length,l:'No Traffic', cls:'ss-warn', qf:'notraffic'},
  ];
  const el=document.getElementById('stats-strip');
  el.innerHTML='';
  items.forEach(({n,l,cls,qf})=>{
    const d=document.createElement('div');
    d.className='ss-item'+(cls?' '+cls:'');
    d.innerHTML=`<span class="ss-n">${n}</span><span class="ss-l">${l}</span>`;
    if(qf) d.onclick=()=>quickFilter(qf);
    el.appendChild(d);
  });
}

// ── Init ─────────────────────────────────────────────────────
try {
  [...new Set(INV.map(r=>r.type))].sort().forEach(t=>{
    const o=document.createElement('option'); o.value=t; o.textContent=t;
    document.getElementById('type-f').appendChild(o);
  });
  if(INV.some(r=>r.type==='bold-timeline')) document.getElementById('bold-notice').style.display='flex';
  document.getElementById('qfilters').style.display='flex';
  buildStats(); updateCounts(); INV_F=INV; filterTable();
} catch(e) {
  console.error('Init error:', e);
  const tb = document.getElementById('inv-body');
  if(tb) tb.innerHTML = '<tr><td colspan="12" style="color:#c0392b;padding:20px;font-size:13px;font-weight:bold">'+
    '\u26a0 JS init error \u2014 check browser console (F12):<br><code style="font-size:11px">'+
    e.message+'</code></td></tr>';
}
</script>
</body>
</html>"""
    return html
# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 65)
    log.info("STEP 5 — Building reports")
    log.info("=" * 65)

    # Load data — use the most complete file available
    pages = (
        load_json(DATA_PAGES)    or
        load_json(DATA_CONTENT)  or []
    )
    if not pages:
        log.error("No page data found. Run at least 2_extract.py first.")
        sys.exit(1)

    menus    = load_json(DATA_MENUS,    fallback=[])
    sim_data = load_json(DATA_CLUSTERS, fallback=None)

    log.info(f"Pages: {len(pages)}  |  Menu items: {len(menus)}  |  "
             f"Similarity data: {'yes' if sim_data else 'no'}")

    # ── Excel ──────────────────────────────────────────────────
    log.info("\nBuilding Excel workbook …")
    wb = Workbook()
    wb.remove(wb.active)   # remove default blank sheet

    build_summary_tab(wb, pages, sim_data)
    ga_start, ga_end = _load_ga_dates()
    build_inventory_tab(wb, pages, ga_start, ga_end)
    build_content_tab(wb, pages)
    build_migration_tab(wb, pages)
    build_similarity_tab(wb, sim_data)
    build_nav_tab(wb, menus)

    wb.save(XL_OUT)
    log.info(f"  Saved → {XL_OUT}")

    # ── HTML ───────────────────────────────────────────────────
    log.info("\nBuilding HTML dashboard …")
    html, inv_json, text_json, pair_json, nav_json = build_html(pages, menus, sim_data)

    # ── Sanity check: </script> in a <script> block kills the page ──
    import re as _re
    script_blocks = _re.findall(r'<script[^>]*>(.*?)</script>', html, _re.DOTALL)
    for blk in script_blocks:
        # Simpler and actually correct: just search the raw string
        if '</script>' in html[html.index('<script>') + 8 : html.rindex('</script>')].lower():
            raise RuntimeError("Unescaped </script> inside the <script> block")
            log.error("The HTML will render blank. Check _jsafe() escaping in build_html.")
            raise RuntimeError("Unescaped </script> in generated HTML")
    log.info("  ✓ Script block safety check passed")
    # ── Write self-contained HTML ──────────────────────────────
    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"  Saved → {HTML_OUT}")

    log.info(f"\n{'='*65}")
    log.info("All done! Your deliverables are in the output/ folder:")
    log.info(f"  {XL_OUT}")
    log.info(f"  {HTML_OUT}")


if __name__ == "__main__":
    main()
