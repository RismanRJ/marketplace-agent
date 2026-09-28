#!/usr/bin/env python3
"""
Marketplace agent telemetry dashboard builder.
CLI: dashboard.py build [--out PATH] [--artifact]
     dashboard.py selfcheck
"""

import json
import html
import os
import sys
import glob
from pathlib import Path
from datetime import datetime
import tempfile
import shutil


def get_project_root():
    """Resolve project root from CLAUDE_PROJECT_DIR env or cwd."""
    if 'CLAUDE_PROJECT_DIR' in os.environ:
        return Path(os.environ['CLAUDE_PROJECT_DIR'])
    return Path.cwd()


def load_jsonl_file(path):
    """Load JSONL file, skip malformed lines, return (records, skipped_count)."""
    records = []
    skipped = 0
    if not Path(path).exists():
        return records, skipped

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    return records, skipped


PROVENANCE = {
    "amazon": ("Live API (SP-API / Ads API)", "amazon"),
    "flipkart": ("Autonomous Browser Session", "flipkart"),
}


def provenance(channel):
    """(label, css_class) for a channel. Provenance is never inferred silently."""
    return PROVENANCE.get(str(channel).lower(), ("Unknown provenance", "unknown"))


def provenance_label(channel):
    return provenance(channel)[0]


def get_provenance_class(channel):
    return provenance(channel)[1]


def render_mutations_table(mutations):
    """Render mutations as HTML table."""
    if not mutations:
        return '<p class="no-data">No mutations found.</p>'

    rows = []
    for m in mutations:
        channel = m.get('channel')
        is_dry_run = m.get('dry_run', False)
        class_attr = ' class="dry-run"' if is_dry_run else ''

        prov_text = provenance_label(channel)
        prov_class = get_provenance_class(channel)

        before_val = str(m.get('before', '-'))[:50]
        after_val = str(m.get('after', '-'))[:50]

        rows.append(f"""  <tr{class_attr}>
    <td>{html.escape(str(m.get('ts', '-')))}</td>
    <td><code>{html.escape(str(m.get('run_id', '-'))[:8])}</code></td>
    <td><span class="badge {prov_class}">{html.escape(prov_text)}</span></td>
    <td>{html.escape(str(m.get('kind', '-')))}</td>
    <td>{html.escape(str(m.get('entity_id', '-')))}</td>
    <td><code>{html.escape(before_val)}</code></td>
    <td><code>{html.escape(after_val)}</code></td>
    <td>{html.escape(str(m.get('reason', '-')))}</td>
    <td>{'🔒 dry-run' if is_dry_run else 'applied'}</td>
  </tr>
""")

    table_html = f"""<div class="table-wrap"><table>
  <thead>
    <tr>
      <th>Timestamp</th>
      <th>Run ID</th>
      <th>Provenance</th>
      <th>Kind</th>
      <th>Entity ID</th>
      <th>Before</th>
      <th>After</th>
      <th>Reason</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
{''.join(rows)}  </tbody>
</table></div>
"""
    return table_html


def render_rejection_reasons(reasons):
    """Render top rejection reasons."""
    if not reasons:
        return '<p class="no-data">No rejection reasons.</p>'

    items = ''.join([f'<li>{html.escape(r)}</li>' for r in reasons])
    return f'<p><strong>Recent rejection reasons:</strong></p><ul>{items}</ul>'


def render_blockers_section(blockers, degraded):
    """Render blockers and degraded channel records."""
    if not blockers and not degraded:
        return '<p class="no-data">✓ No blockers or degraded channels detected.</p>'

    html_parts = []

    if blockers:
        html_parts.append(f'<p><strong>⚠ {len(blockers)} Blocker(s):</strong></p>')
        items = ''.join([f'<li>{html.escape(b.get("kind", "unknown"))} on {html.escape(str(b.get("entity_id", "-")))}</li>' for b in blockers[:10]])
        html_parts.append(f'<ul>{items}</ul>')

    if degraded:
        html_parts.append(f'<p><strong>⚠ {len(degraded)} Degraded channel(s):</strong></p>')
        items = ''.join([f'<li>{html.escape(d.get("channel", "unknown"))} at {html.escape(str(d.get("ts", "-")))}</li>' for d in degraded[:10]])
        html_parts.append(f'<ul>{items}</ul>')

    return ''.join(html_parts)


DASHBOARD_TITLE = "Marketplace Agent Telemetry"

DASHBOARD_CSS = """
:root {
  --bg: #ffffff;
  --fg: #333333;
  --border: #dddddd;
  --header-bg: transparent;
  --th-bg: #f5f5f5;
  --row-alt-bg: #fafafa;
  --dry-run-bg: rgba(150, 150, 200, 0.2);
  --badge-fallback-bg: rgba(0, 0, 0, 0.06);
  --amazon-bg: #FF9900;
  --amazon-fg: #ffffff;
  --flipkart-bg: #1e50dc;
  --flipkart-fg: #ffffff;
  --unknown-bg: #999999;
  --unknown-fg: #ffffff;
  --accent: #0066cc;
  --footer-fg: #666666;
  --no-data-fg: #999999;
  --code-bg: #f5f5f5;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #1e1e1e;
    --fg: #e0e0e0;
    --border: #444444;
    --th-bg: #333333;
    --row-alt-bg: #262626;
    --dry-run-bg: rgba(100, 100, 150, 0.3);
    --badge-fallback-bg: rgba(255, 255, 255, 0.1);
    --footer-fg: #aaaaaa;
    --no-data-fg: #888888;
    --code-bg: #333333;
  }
}

:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #1e1e1e;
  --fg: #e0e0e0;
  --border: #444444;
  --th-bg: #333333;
  --row-alt-bg: #262626;
  --dry-run-bg: rgba(100, 100, 150, 0.3);
  --badge-fallback-bg: rgba(255, 255, 255, 0.1);
  --footer-fg: #aaaaaa;
  --no-data-fg: #888888;
  --code-bg: #333333;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 0;
  padding: 20px 16px;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.5;
}

.header {
  background: var(--header-bg);
  border: 1px solid var(--border);
  padding: 15px;
  margin-bottom: 20px;
  border-radius: 4px;
}

.header h1 {
  margin: 0 0 10px 0;
}

.header p {
  margin: 5px 0;
  font-size: 0.9em;
}

.section {
  margin-bottom: 30px;
}

.section h2 {
  margin-top: 0;
  border-bottom: 2px solid var(--accent);
  padding-bottom: 8px;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  border: 1px solid var(--border);
  font-size: 0.9em;
  background: var(--bg);
}

th, td {
  border: 1px solid var(--border);
  padding: 6px;
  text-align: left;
}

th {
  background: var(--th-bg);
  font-weight: bold;
}

tr:nth-child(even) {
  background: var(--row-alt-bg);
}

.dry-run {
  background: var(--dry-run-bg);
}

.badge {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 0.8em;
  font-weight: bold;
  white-space: nowrap;
  background: var(--badge-fallback-bg);
}

.amazon {
  background: var(--amazon-bg);
  color: var(--amazon-fg);
}

.flipkart {
  background: var(--flipkart-bg);
  color: var(--flipkart-fg);
}

.unknown {
  background: var(--unknown-bg);
  color: var(--unknown-fg);
}

.footer {
  margin-top: 40px;
  padding: 10px;
  border-top: 1px solid var(--border);
  font-size: 0.85em;
  color: var(--footer-fg);
}

.no-data {
  font-style: italic;
  color: var(--no-data-fg);
}

code {
  background: var(--code-bg);
  padding: 2px 4px;
  border-radius: 2px;
  font-size: 0.85em;
  font-variant-numeric: tabular-nums;
}
"""


def render_dashboard_body(now_utc, run_ids, mutations, total_skipped,
                           bid_approved, bid_amended, bid_rejected,
                           image_approved, image_rejected,
                           rejection_reasons, blockers, degraded):
    """Render the dashboard's body markup (no <html>/<head>/<body> wrapper)."""
    return f"""<div class="header">
  <h1>Marketplace Agent Telemetry Dashboard</h1>
  <p><strong>Generated:</strong> {html.escape(now_utc)}</p>
  <p><strong>Unique runs:</strong> {len(run_ids)}</p>
</div>

<div class="section">
  <h2>Recent Mutations ({len(mutations)} total, capped at 200)</h2>
  {render_mutations_table(mutations)}
</div>

<div class="section">
  <h2>Decision Outcomes</h2>
  <p><strong>Bid Reviews:</strong> {bid_approved} approved, {bid_amended} amended, {bid_rejected} rejected</p>
  <p><strong>Image Approvals:</strong> {image_approved} approved, {image_rejected} rejected</p>
  {render_rejection_reasons(rejection_reasons)}
</div>

<div class="section">
  <h2>Operational Status</h2>
  {render_blockers_section(blockers, degraded)}
</div>

<div class="footer">
  {f"Skipped {total_skipped} malformed JSON lines." if total_skipped > 0 else "All JSON lines parsed successfully."}
</div>
"""


def build_dashboard(out_path=None, artifact=False):
    """Build the unified telemetry dashboard HTML.

    Default emits a complete standalone document. With artifact=True, emits a
    fragment (title + style + body content only) suitable for publishing as a
    Claude Artifact, which wraps the fragment in its own document skeleton.
    """
    project_root = get_project_root()

    if out_path is None:
        out_path = project_root / "dashboard" / "unified_view.html"
    else:
        out_path = Path(out_path)

    # Ensure output directory exists
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Load audit mutations
    audit_dir = project_root / "analytics" / "audit"
    mutations = []
    total_skipped = 0
    run_ids = set()

    if audit_dir.exists():
        for jsonl_file in sorted(audit_dir.glob("*.jsonl")):
            records, skipped = load_jsonl_file(str(jsonl_file))
            mutations.extend(records)
            total_skipped += skipped
            for rec in records:
                if 'run_id' in rec:
                    run_ids.add(rec['run_id'])

    # Sort mutations by timestamp (newest first)
    mutations.sort(key=lambda x: x.get('ts', ''), reverse=True)
    mutations = mutations[:200]  # Cap at 200 rows

    # Load decision outcomes
    bid_reviews = []
    bid_reviews_path = project_root / "analytics" / "decisions" / "bid_reviews.jsonl"
    if bid_reviews_path.exists():
        bid_reviews, skipped = load_jsonl_file(str(bid_reviews_path))
        total_skipped += skipped

    image_approvals = []
    image_approvals_path = project_root / "analytics" / "decisions" / "image_approvals.jsonl"
    if image_approvals_path.exists():
        image_approvals, skipped = load_jsonl_file(str(image_approvals_path))
        total_skipped += skipped

    # Count decision outcomes
    bid_approved = sum(1 for r in bid_reviews if r.get('decision') == 'approve')
    bid_amended = sum(1 for r in bid_reviews if r.get('decision') == 'amend')
    bid_rejected = sum(1 for r in bid_reviews if r.get('decision') == 'reject')

    image_approved = sum(1 for r in image_approvals if r.get('decision') == 'approve')
    image_rejected = sum(1 for r in image_approvals if r.get('decision') == 'reject')

    # Collect rejection reasons (most recent from bid reviews)
    rejection_reasons = [r.get('reason', 'unknown') for r in bid_reviews if r.get('decision') == 'reject']
    rejection_reasons = rejection_reasons[:10]

    # Find blockers
    blockers = [m for m in mutations if 'blocker' in m.get('kind', '').lower()]
    degraded = [m for m in mutations if 'degraded' in m.get('kind', '').lower()]

    # Generate HTML
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    body_html = render_dashboard_body(
        now_utc, run_ids, mutations, total_skipped,
        bid_approved, bid_amended, bid_rejected,
        image_approved, image_rejected,
        rejection_reasons, blockers, degraded,
    )

    if artifact:
        html_content = f"""<title>{DASHBOARD_TITLE}</title>
<style>{DASHBOARD_CSS}</style>
{body_html}"""
    else:
        html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{DASHBOARD_TITLE}</title>
<style>{DASHBOARD_CSS}</style>
</head>
<body>
{body_html}
</body>
</html>
"""

    with open(out_path, 'w') as f:
        f.write(html_content)

    return str(out_path)


def selfcheck():
    """Run a self-check: create sample data, build, validate output."""
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir)
        os.environ['CLAUDE_PROJECT_DIR'] = str(project_root)

        # Create sample data structure
        audit_dir = project_root / "analytics" / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)

        decisions_dir = project_root / "analytics" / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)

        # Write sample audit mutations
        audit_file = audit_dir / "mutations.jsonl"
        with open(audit_file, 'w') as f:
            # Amazon record
            f.write(json.dumps({
                "ts": "2024-01-15T10:30:00Z",
                "run_id": "run-123abc",
                "kind": "price_update",
                "channel": "amazon",
                "entity_id": "ASIN-12345",
                "before": "100.00",
                "after": "95.00",
                "reason": "competitive adjustment",
                "actor": "bot",
                "dry_run": False
            }) + "\n")

            # Flipkart record
            f.write(json.dumps({
                "ts": "2024-01-15T10:31:00Z",
                "run_id": "run-123abc",
                "kind": "inventory_sync",
                "channel": "flipkart",
                "entity_id": "FK-67890",
                "before": "50",
                "after": "45",
                "reason": "stock adjustment",
                "actor": "bot",
                "dry_run": True
            }) + "\n")

            # Blocker record
            f.write(json.dumps({
                "ts": "2024-01-15T10:32:00Z",
                "run_id": "run-123def",
                "kind": "blocker_detected",
                "channel": "amazon",
                "entity_id": "ASIN-99999",
                "before": "active",
                "after": "blocked",
                "reason": "inventory zero",
                "actor": "bot",
                "dry_run": True
            }) + "\n")

            # Malformed JSON line (this should be skipped)
            f.write("{ malformed json line\n")

            # Record with XSS attempt in reason
            f.write(json.dumps({
                "ts": "2024-01-15T10:33:00Z",
                "run_id": "run-123ghi",
                "kind": "keyword_update",
                "channel": "amazon",
                "entity_id": "ASIN-11111",
                "before": "old keyword",
                "after": "new <script>alert('xss')</script> keyword",
                "reason": "keyword <script>alert('xss')</script>",
                "actor": "bot",
                "dry_run": False
            }) + "\n")

        # Write sample bid reviews
        bid_reviews_file = decisions_dir / "bid_reviews.jsonl"
        with open(bid_reviews_file, 'w') as f:
            f.write(json.dumps({
                "decision": "approve",
                "bid_id": "bid-1",
                "reason": "acceptable"
            }) + "\n")
            f.write(json.dumps({
                "decision": "reject",
                "bid_id": "bid-2",
                "reason": "too aggressive"
            }) + "\n")

        # Write sample image approvals
        image_approvals_file = decisions_dir / "image_approvals.jsonl"
        with open(image_approvals_file, 'w') as f:
            f.write(json.dumps({
                "decision": "approve",
                "image_id": "img-1"
            }) + "\n")

        # Build the dashboard
        out_file = build_dashboard()

        # Verify output file exists
        assert Path(out_file).exists(), f"Output file not created: {out_file}"

        # Read and validate content
        with open(out_file, 'r') as f:
            content = f.read()

        # Check for required sections
        assert "Marketplace Agent Telemetry Dashboard" in content, "Missing title"
        assert "Recent Mutations" in content, "Missing mutations section"
        assert "Decision Outcomes" in content, "Missing decision section"

        # Check provenance labels
        assert "Live API (SP-API / Ads API)" in content, "Amazon provenance label missing"
        assert "Autonomous Browser Session" in content, "Flipkart provenance label missing"

        # Check malformed line count
        assert "Skipped 1 malformed" in content, "Skipped count not reported"

        # Check that XSS attempt is escaped
        assert "<script>" not in content, "XSS vulnerability: unescaped <script> tag found"
        assert "&lt;script&gt;" in content, "Properly escaped content missing"

        # Check dry-run distinction
        assert "dry-run" in content.lower(), "Dry-run distinction missing"

        # Artifact fragment mode
        artifact_out = str(project_root / "dashboard" / "artifact_fragment.html")
        artifact_file = build_dashboard(artifact_out, artifact=True)
        with open(artifact_file, 'r') as f:
            artifact_content = f.read()

        assert "<title>" in artifact_content, "Artifact fragment missing <title>"
        lowered = artifact_content.lower()
        assert "<!doctype" not in lowered, "Artifact fragment must not contain a doctype"
        assert "<html" not in lowered, "Artifact fragment must not contain an <html> tag"
        assert "<body" not in lowered, "Artifact fragment must not contain a <body> tag"
        assert "Live API (SP-API / Ads API)" in artifact_content, "Amazon provenance label missing in artifact"
        assert "Autonomous Browser Session" in artifact_content, "Flipkart provenance label missing in artifact"
        assert "<script>" not in artifact_content, "XSS vulnerability in artifact fragment"
        assert "&lt;script&gt;" in artifact_content, "Properly escaped content missing in artifact fragment"

        print("selfcheck ok")
        return True

    finally:
        shutil.rmtree(tmpdir)
        if 'CLAUDE_PROJECT_DIR' in os.environ:
            del os.environ['CLAUDE_PROJECT_DIR']


def main():
    if len(sys.argv) < 2:
        print("Usage: dashboard.py build [--out PATH] [--artifact]")
        print("       dashboard.py selfcheck")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "build":
        out_path = None
        artifact = False
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--out" and i + 1 < len(args):
                out_path = args[i + 1]
                i += 2
            elif args[i] == "--artifact":
                artifact = True
                i += 1
            else:
                i += 1
        result = build_dashboard(out_path, artifact=artifact)
        print(f"Dashboard built: {result}")

    elif cmd == "selfcheck":
        selfcheck()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
