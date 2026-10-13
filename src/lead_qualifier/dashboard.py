"""A dependency-free HTML dashboard of recent decisions."""

from __future__ import annotations

from html import escape
from typing import Any

TIERS = ["hot", "warm", "cold", "spam"]

STYLE = """
:root { --bg:#f7f7f8; --card:#fff; --text:#1d1d1f; --muted:#6b6b76; --line:#e6e6ea;
        --hot:#e5484d; --warm:#f5a524; --cold:#3e8ef7; --spam:#8b8d98; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111113; --card:#1a1a1d; --text:#ececf1; --muted:#9a9aa6; --line:#2a2a30; }
}
* { box-sizing:border-box; }
body { margin:0; font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }
main { max-width:1200px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:20px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 20px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; text-decoration:none; color:inherit; }
.stat b { display:block; font-size:24px; } .stat .label { color:var(--muted); font-size:12px; }
.stat .badge { margin-right:4px; }
.stat.active { outline:2px solid var(--text); }
.table { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }
table { width:100%; border-collapse:collapse; min-width:820px; }
th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:12px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.03em; }
tr:last-child td { border-bottom:0; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; color:#fff; font-size:12px; font-weight:600; }
.hot{background:var(--hot)} .warm{background:var(--warm)} .cold{background:var(--cold)} .spam{background:var(--spam)}
.muted { color:var(--muted); } .nowrap { white-space:nowrap; } .review { color:var(--hot); font-weight:600; font-size:12px; }
details summary { cursor:pointer; color:var(--muted); }
pre { white-space:pre-wrap; word-break:break-word; margin:6px 0 0; font:12px/1.4 ui-monospace,Menlo,monospace; }
"""


def render_dashboard(stats: dict[str, Any], rows: list[dict[str, Any]], active_tier: str | None) -> str:
    by_tier = stats.get("by_tier", {})
    cards = [
        f'<a class="stat{" active" if not active_tier else ""}" href="?"><b>{stats.get("total", 0)}</b><span class="label">All leads</span></a>'
    ]
    for tier in TIERS:
        info = by_tier.get(tier, {})
        cards.append(
            f'<a class="stat{" active" if active_tier == tier else ""}" href="?tier={tier}">'
            f'<b>{info.get("count", 0)}</b><span class="badge {tier}">{tier}</span><span class="label">'
            f"avg {info.get('avg_score', 0)} · review {info.get('needs_review', 0)}</span></a>"
        )

    body_rows = []
    for row in rows:
        result = row["result"]
        lead = row["lead"]
        extracted = result.get("extracted", {})
        reasons = escape(" ".join(result.get("reasons", [])))
        routed = ", ".join(f"{r['output']}{'' if r['ok'] else ' ✗'}" for r in row.get("routing", [])) or "—"
        body_rows.append(
            "<tr>"
            f'<td class="muted">{escape(row["created_at"][:16].replace("T", " "))}</td>'
            f'<td><span class="badge {escape(row["tier"])}">{escape(row["tier"])}</span>'
            + ('<div class="review">needs review</div>' if row["needs_review"] else "")
            + "</td>"
            f'<td class="nowrap"><b>{row["score"]:.0f}</b> <span class="muted">rules {row["rule_score"]:.0f}'
            + (f" · model {row['llm_score']:.0f}" if row["llm_score"] is not None else "")
            + "</span></td>"
            f'<td>{escape(row.get("company") or "—")}<div class="muted">{escape(lead.get("email") or "")}</div></td>'
            f'<td>{escape(extracted.get("budget") or "—")}<div class="muted">{escape(extracted.get("timeline") or "")}</div></td>'
            f"<td>{escape(extracted.get('need') or '—')}</td>"
            f'<td class="muted">{escape(routed)}</td>'
            f"<td><details><summary>details</summary><pre>{reasons}</pre>"
            f"<pre>{escape(lead.get('message', '')[:1200])}</pre></details></td>"
            "</tr>"
        )
    table = (
        "<div class='table'><table><thead><tr><th>Received (UTC)</th><th>Tier</th><th>Score</th><th>Company</th>"
        "<th>Budget / timeline</th><th>Need</th><th>Routed to</th><th></th></tr></thead><tbody>"
        + ("".join(body_rows) or "<tr><td colspan='8' class='muted'>No leads yet.</td></tr>")
        + "</tbody></table></div>"
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Lead Qualifier</title><style>{STYLE}</style></head><body><main>"
        "<h1>Lead Qualifier</h1><p class='sub'>Most recent qualification decisions</p>"
        f"<div class='stats'>{''.join(cards)}</div>{table}</main></body></html>"
    )
