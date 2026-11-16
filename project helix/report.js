"use strict";
(() => {
  const $ = s => document.querySelector(s);
  const id = new URLSearchParams(location.search).get("id") || "";
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const usd = v => v == null || Number.isNaN(Number(v)) ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(v);
  const short = v => {
    if (v == null) return "—"; const n = Number(v), a = Math.abs(n), sign = n < 0 ? "−" : "";
    return a >= 1e6 ? `${sign}$${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `${sign}$${Math.round(a / 1e3)}k` : `${sign}$${Math.round(a)}`;
  };
  const sqft = v => v == null ? "—" : `${Math.round(v).toLocaleString()} sq ft`;
  const date = v => { if (!v) return "—"; const d = new Date(v); return Number.isNaN(+d) ? String(v).slice(0, 10) : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); };
  const view = v => ({ aerial: "Aerial", "oblique-north": "North oblique", "oblique-south": "South oblique", "oblique-east": "East oblique", "oblique-west": "West oblique" }[v] || v);
  const sentence = s => { const t = String(s || "").trim(); return t ? t[0].toUpperCase() + t.slice(1) : ""; };
  const kindTag = k => k === "model_inference" ? '<span class="ai">AI reading</span>' : "";

  if (id) {
    $("#nav-investigation").href = `/?investigation=${encodeURIComponent(id)}`;
    $("#nav-finance").href = `/finance-lab?handoff=${encodeURIComponent(id)}`;
  }
  $("#print").addEventListener("click", () => window.print());

  function part(n, title, lede, body) {
    return `<section class="part" id="s${n}"><div class="part-head"><span class="num">${n}</span><div><h2>${esc(title)}</h2>${lede ? `<p class="lede">${esc(lede)}</p>` : ""}</div></div>${body}</section>`;
  }

  function cover(r) {
    const p = r.property, sel = r.selection, v = r.verdict, run = r.financial?.run;
    const title = sel ? `${sel.label} at ${p.address}` : `${p.address}: adaptive reuse options`;
    const kpis = run ? [
      ["Cash to open", short(run.capital.cash_to_open), `${run.scenario} case`],
      ["Reserve held", short(run.capital.reserve), ""],
      [run.capital.cash_gap > 0 ? "Funding gap" : "Headroom", short(run.capital.cash_gap > 0 ? -run.capital.cash_gap : run.capital.cash_headroom), ""],
      ["Cash at month 12", short(run.months.at(-1)?.cash), "no revenue"],
    ] : [];
    return `<div class="kicker">Investment memorandum · Adaptive reuse</div>
      <h1>${esc(title)}</h1>
      <p class="subtitle">${esc(p.city)}, ${esc(p.state)} · ${esc(p.property_type || "Commercial")} · ${sqft(p.space_sqft)} for lease at ${usd(p.rent_monthly)}/month</p>
      <div class="meta"><span>Prepared <b>${date(r.generated_at)}</b></span><span>Budget <b>${usd(r.intake.total_budget_usd)}</b></span><span>Funding <b>${esc(r.intake.financing || "—")}</b></span><span>CoStar property <b>${esc(p.property_id)}</b></span><span>Investigation <b>${esc(r.investigation_id.slice(0, 8))}</b></span></div>
      <div class="verdict ${esc(v.code)}"><div class="label">Verdict</div><h2>${esc(v.headline)}</h2>
        ${v.reasons?.length ? `<ul>${v.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        ${v.open_checks ? `<p class="condition">Conditional on ${v.open_checks} open diligence items (section 6). This is a screening and cash-runway view, not a profitability forecast.</p>` : ""}
      </div>
      ${kpis.length ? `<div class="kpis">${kpis.map(([k, val, n]) => `<div class="kpi ${String(val).startsWith("−") ? "neg" : ""}"><small>${esc(k)}${n ? ` · ${esc(n)}` : ""}</small><strong>${esc(val)}</strong></div>`).join("")}</div>` : ""}`;
  }

  function property(r) {
    const p = r.property;
    const rows = [
      ["Address", `${p.address}, ${p.city}, ${p.state}`], ["Property type", [p.property_type, p.building_class].filter(Boolean).join(" · ")],
      ["Building", `${sqft(p.building_sqft)} · ${p.stories ?? "—"} ${p.stories === 1 ? "story" : "stories"}`],
      ["Space offered", `${sqft(p.space_sqft)} (listing ${p.listing_id})`],
      ["Asking rent", `${usd(p.rent_monthly)}/month · $${p.rent_psf_year ?? "—"}/sq ft/yr`], ["Lease basis", p.lease_basis || "—"],
      ["Additional charges", p.occupancy_costs === "unknown" ? "Not disclosed (CAM, tax, insurance)" : p.occupancy_costs],
      ["Zoning (CoStar)", p.zoning || "—"], ["Ceiling (CoStar, building)", p.ceiling_ft ? `${p.ceiling_ft} ft` : "Not recorded"],
      ["Drive-in bays (CoStar)", p.drive_in_bays ?? "Not recorded"], ["Available", date(p.available_on)],
      ["Permitting authority", p.authority || p.jurisdiction || "—"],
    ];
    const [hero, ...rest] = p.images || [];
    const gallery = hero ? `<div class="gallery"><figure class="hero"><img src="${esc(hero.url)}" alt="${esc(view(hero.view))} of ${esc(p.address)}" loading="lazy"><figcaption>${esc(view(hero.view))} · Vexcel · ${date(hero.captured)}</figcaption></figure>${rest.map(i => `<figure><img src="${esc(i.url)}" alt="${esc(view(i.view))}" loading="lazy"><figcaption>${esc(view(i.view))}</figcaption></figure>`).join("")}</div>` : "";
    return part(1, "Property", "The building and the space on offer, from CoStar records and Vexcel imagery.",
      `<div class="grid2"><div><dl class="facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>${p.tour_url ? `<a class="tour" href="${esc(p.tour_url)}" target="_blank" rel="noopener">Walk through the Matterport tour ↗</a>` : ""}</div><div>${gallery}</div></div>`);
  }

  function physical(r) {
    const ph = r.physical, cov = ph.coverage || {}, label = r.selection?.label;
    const cards = ph.findings.map(f => `<article class="finding"><h3>${esc(f.title)}</h3>
        <div class="chips">${f.features.map(x => `<span class="chip">${esc(x)}</span>`).join("")}</div>
        ${f.detail ? `<p>${esc(sentence(f.detail))}</p>` : ""}
        <p><span class="so">So what:</span> ${esc(f.implication)}</p>
        ${f.for_selected_use && label ? `<div class="use"><b>For ${esc(label.toLowerCase())}:</b> ${esc(f.for_selected_use)}</div>` : ""}</article>`).join("");
    const unmeasured = ph.unmeasured?.length ? `<div class="callout"><b>Still to measure on site</b>${esc(ph.unmeasured.join(" · "))}. Photos show what is there; they do not give dimensions.</div>` : "";
    const comps = (ph.comparisons || []).filter(c => c.status === "not_comparable").map(c => `<li>${esc(c.reason)}</li>`).join("");
    return part(2, "Physical intelligence",
      `What the cameras saw: ${cov.panoramas_analyzed ?? "—"} of ${cov.panoramas_total ?? "—"} Matterport panoramas and ${cov.aerial_views ?? "—"} Vexcel views, ${cov.observations ?? "—"} observations in total. ${ph.scope_note}`,
      `<div class="findings">${cards}</div>${unmeasured}
       <details class="ev"><summary>All physical evidence (${ph.findings.reduce((n, f) => n + f.evidence.length, 0)} cited observations)</summary>
        ${ph.findings.map(f => `<h4>${esc(f.title)}</h4><ul>${f.evidence.map(e => `<li>${esc(e.description)} <span class="note">· ${esc(e.provider)} · ${esc(e.ref)}</span></li>`).join("")}</ul>`).join("")}
        ${comps ? `<h4>Measurements that are not comparable</h4><ul>${comps}</ul>` : ""}<p class="note">Vision model: ${esc(cov.model || "—")}. Observations are descriptions, not inspections.</p></details>`);
  }

  function rangeChart(o) {
    const c = o.candidates; if (!c.length) return "";
    const max = Math.max(o.budget || 0, ...c.map(x => x.cash_to_open.high || 0)) * 1.08;
    const pct = v => `${(100 * v / max).toFixed(2)}%`;
    const ticks = [0, 0.25, 0.5, 0.75, 1].map(t => Math.round(max * t / 100000) * 100000);
    return `<div class="range-chart">${c.map(x => { const k = x.cash_to_open; return `<div class="range-row ${x.selected ? "sel" : ""}"><div class="name">${esc(x.label)}</div>
      <div class="track"><div class="band" style="left:${pct(k.low)};width:${pct(k.high - k.low)}"></div><div class="base" style="left:${pct(k.base)}" title="Base case ${usd(k.base)}"></div>${o.budget ? `<div class="budget-line" style="left:${pct(o.budget)}"></div>` : ""}</div>
      <div class="val">${short(k.low)}–${short(k.high)}</div></div>`; }).join("")}
      <div class="axis"><i></i><div>${ticks.map(t => `<span style="left:${pct(t)}">${short(t)}</span>`).join("")}</div><i></i></div>
      <p class="note">Bars show cash to open, low to high; the dark tick is the base case. Dashed red line: the ${usd(o.budget)} budget.</p></div>`;
  }

  function opportunities(r) {
    const o = r.opportunities;
    const cards = o.candidates.map(x => `<article class="card ${x.selected ? "sel" : ""}">${x.selected ? '<span class="tag">Selected</span>' : ""}<div class="rank">Rank #${esc(x.rank)} · ${esc(x.group || "")}</div><h3>${esc(x.label)}</h3>
      <dl><dt>Building fit</dt><dd class="status ${x.building_status === "pass" ? "ok" : "check"}">${x.building_status === "pass" ? "Fits" : "Fits with a condition"}</dd>
      <dt>Base cash to open</dt><dd>${usd(x.cash_to_open.base)}</dd><dt>Largest reserve that still fits</dt><dd>${usd(x.max_reserve_base_case)}</dd></dl>
      ${x.building_checks.length ? `<h4>Building condition</h4><ul>${x.building_checks.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      ${x.checks.length ? `<h4>Decisive checks for this use</h4><ul>${x.checks.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}</article>`).join("");
    const stopped = Object.entries(o.stopped_at || {}).filter(([, n]) => n).map(([k, n]) => `${n} at ${k}`).join(", ");
    return part(3, "Opportunity comparison",
      `${o.evaluated} business types were screened against this space. ${o.passed} cleared every building, budget and compliance gate${stopped ? ` (stopped: ${stopped})` : ""}.`,
      `${rangeChart(o)}<div class="cards">${cards}</div>
       ${o.common_checks.length ? `<p class="common"><b>For every option:</b> ${esc(o.common_checks.join(" · "))}</p>` : ""}
       ${o.tie_breaks.length ? `<p class="note">Ranking: ${esc(o.tie_breaks.join(" "))}</p>` : ""}
       ${o.not_evaluated.length ? `<p class="note">Not part of this screening: local demand and licences (no Los Angeles model), and the scan findings in section 2.</p>` : ""}`);
  }

  function selected(r) {
    const sel = r.selection; if (!sel) return part(4, "Selected business", "", `<p class="note">No business has been chosen yet. Choose one from the business options to complete this memorandum.</p>`);
    const cand = r.opportunities.candidates.find(c => c.selected) || {};
    const notes = r.physical.findings.filter(f => f.for_selected_use).map(f => `<li><b>${esc(f.title)}:</b> ${esc(f.for_selected_use)}</li>`).join("");
    return part(4, `The case for ${sel.label}`, `Chosen ${date(sel.selected_at)} after reviewing the opportunity analysis. Ranked #${sel.rank} of ${r.opportunities.passed} by the deterministic screen.`,
      `<div class="case"><div><h3>What the evidence says</h3><ul class="claims">${(sel.report_claims || []).map(c => `<li>${esc(c.text)}${kindTag(c.kind)}</li>`).join("")}</ul><p class="note">From the GPT Sol opportunity analysis; each statement cites saved Stage 1–3 evidence (section 7).</p></div>
       <div><h3>What this building means for it</h3><ul class="claims">${notes || "<li>No use-specific observations.</li>"}${cand.building_checks?.map(c => `<li><b>Size:</b> ${esc(c)}</li>`).join("") || ""}</ul>
       ${cand.checks?.length ? `<h3 style="margin-top:14px">Make-or-break checks</h3><ul class="claims">${cand.checks.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}</div></div>`);
  }

  function cashChart(run) {
    const rows = run.months, late = run.delayed_months || [], reserve = run.capital.reserve;
    if (!rows.length) return "";
    const W = 900, H = 260, L = 64, R = 16, T = 16, B = 34;
    const vals = [...rows.map(x => x.cash), ...late.map(x => x.cash), reserve, 0];
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const x = m => L + (m - 1) * (W - L - R) / 11, y = v => T + (hi - v) * (H - T - B) / (hi - lo || 1);
    const path = arr => arr.map((p, i) => `${i ? "L" : "M"}${x(p.month).toFixed(1)},${y(p.cash).toFixed(1)}`).join("");
    const grid = [0, .25, .5, .75, 1].map(t => lo + (hi - lo) * t);
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Month-by-month cash balance with no revenue">
      ${grid.map(g => `<line x1="${L}" x2="${W - R}" y1="${y(g)}" y2="${y(g)}" stroke="#ece7dd"/><text x="${L - 8}" y="${y(g) + 4}" text-anchor="end">${short(g)}</text>`).join("")}
      ${rows.map(p => `<text x="${x(p.month)}" y="${H - 12}" text-anchor="middle">M${p.month}</text>`).join("")}
      <line x1="${L}" x2="${W - R}" y1="${y(reserve)}" y2="${y(reserve)}" stroke="#8a6212" stroke-dasharray="5 4"/><text x="${W - R}" y="${y(reserve) - 6}" text-anchor="end" style="fill:#8a6212">Reserve ${short(reserve)}</text>
      ${lo < 0 ? `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="#9a2f2f"/>` : ""}
      <line x1="${x(run.opening_month)}" x2="${x(run.opening_month)}" y1="${T}" y2="${H - B}" stroke="#c9c2b4" stroke-dasharray="2 3"/><text x="${x(run.opening_month) + 4}" y="${T + 10}">Opens M${run.opening_month}</text>
      <path d="${path(rows)}" fill="none" stroke="#1d3a5c" stroke-width="3"/>
      ${rows.map(p => `<circle cx="${x(p.month)}" cy="${y(p.cash)}" r="3.5" fill="#1d3a5c"><title>Month ${p.month}: ${usd(p.cash)}</title></circle>`).join("")}
    </svg>`;
  }

  function financial(r) {
    const f = r.financial, run = f.run, s3 = f.stage3_cash_to_open || {};
    if (!run) return part(5, "Financial analysis", "", `<p class="note">Run the Finance Lab for the selected business to add the capital and cash analysis.</p>`);
    const c = run.capital, total = Math.max(c.budget, c.cash_to_open + c.reserve);
    const w = v => `${(100 * Math.max(0, v) / total).toFixed(2)}%`;
    const gap = c.cash_gap > 0;
    const scen = ["low", "base", "high"].filter(k => run.scenarios?.[k]).map(k => { const s = run.scenarios[k];
      return `<tr class="${k === run.scenario ? "sel" : ""}"><td>${k[0].toUpperCase() + k.slice(1)}${k === run.scenario ? " (modeled)" : ""}</td><td class="n">${usd(s3[k])}</td><td class="n ${s.month12 < 0 ? "neg" : ""}">${usd(s.month12)}</td><td>${s.below_zero === 1 ? "Opening costs exceed the budget" : s.below_zero ? `Runs out in month ${s.below_zero}` : s.below_reserve ? `Below reserve from month ${s.below_reserve}` : "Stays above reserve"}</td></tr>`; }).join("");
    const dev = run.deviations.map(d => d.field === "reserve" && d.handoff == null ? `Reserve of ${usd(d.used)} entered in the Finance Lab (none was set at intake).` : `${d.field.replace("_", " ")}: ${d.used} used instead of ${d.handoff}.`);
    const matches = Object.values(run.consistency || {}).every(x => x.matches);
    return part(5, "Financial analysis",
      `Capital required to open, and how cash moves over the first 12 months if the business earns nothing. Modeled ${date(run.at)}, ${run.scenario} cost case, ${run.financing === "loan" ? "with the proposed loan" : "cash only"}.`,
      `<div class="stack"><div class="stack-bar"><div class="seg-open" style="width:${w(c.cash_to_open)}">Cash to open ${short(c.cash_to_open)}</div><div class="seg-res" style="width:${w(c.reserve)}" title="Reserve ${short(c.reserve)}">${c.reserve / total > .1 ? `Reserve ${short(c.reserve)}` : ""}</div>${gap ? `<div class="seg-gap" style="width:${w(c.cash_gap)}">Gap ${short(c.cash_gap)}</div>` : `<div class="seg-head" style="width:${w(c.cash_headroom)}">Headroom ${short(c.cash_headroom)}</div>`}</div>
        <div class="stack-legend"><span><i class="seg-open"></i>Opening costs (Stage 3 benchmark, scaled to ${sqft(r.property.space_sqft)})</span><span><i class="seg-res"></i>Reserve kept untouched</span><span><i class="${gap ? "seg-gap" : "seg-head"}"></i>${gap ? "Shortfall against" : "Left over from"} the ${short(c.budget)} budget</span></div></div>
       <h3>12-month cash balance, no revenue</h3>${cashChart(run)}
       <p class="note">Rent of ${usd(run.inputs.monthly_rent)}/month is paid from month 1, including the ${run.inputs.build_months}-month build-out. ${run.first_month_below_reserve ? `Cash falls below the reserve in month ${run.first_month_below_reserve}.` : `Cash stays above the reserve all year; the low point is ${usd(run.lowest_cash)}.`} A ${run.inputs.delay_months}-month opening delay moves opening to month ${run.delayed_opening_month} but does not change this no-revenue balance.</p>
       <table class="scen"><thead><tr><th>Cost case</th><th class="n">Cash to open</th><th class="n">Cash at month 12</th><th>Through the year</th></tr></thead><tbody>${scen}</tbody></table>
       ${dev.length || !matches ? `<p class="note">${esc(dev.join(" "))} ${matches ? "Opening costs match the Stage 3 screening exactly." : "Opening costs differ from Stage 3 because rent or build time was changed."}</p>` : `<p class="note">All inputs match the investigation; opening costs match the Stage 3 screening exactly.</p>`}
       <details class="ev"><summary>Opening cost ledger (${run.ledger.length} lines)</summary><table><thead><tr><th>Item</th><th>How</th><th class="n">Low</th><th class="n">Base</th><th class="n">High</th></tr></thead><tbody>${run.ledger.map(l => `<tr><td>${esc(l.label)}</td><td>${esc(l.how)}<br><span class="note">${esc(l.source)}</span></td><td class="n">${usd(l.low)}</td><td class="n">${usd(l.base)}</td><td class="n">${usd(l.high)}</td></tr>`).join("")}</tbody></table></details>`);
  }

  function nextSteps(r) {
    if (!r.next_steps.length) return "";
    return part(6, "Next steps", "Prioritized diligence before signing a lease. Each item names who or what can answer it.",
      `<ol class="steps">${r.next_steps.map(s => `<li><div class="q">${esc(s.question)}<span class="scope-tag">${esc(s.applies)}</span></div>${s.source ? `<div class="src">${esc(s.source)}</div>` : ""}</li>`).join("")}</ol>`);
  }

  function evidence(r) {
    const a = r.analysis, run = r.financial?.run;
    const claimList = list => `<ul>${(list || []).map(c => `<li>${esc(c.text)}${kindTag(c.kind)} <span class="note">· ${esc(c.evidence_ids.join(", "))}</span></li>`).join("")}</ul>`;
    return part(7, "Evidence and method", "Sources behind every section, for anyone who wants to check the work.",
      `${a ? `<details class="ev"><summary>Opportunity analysis · ${esc(a.model)} · ${date(a.created_at)}</summary>
          <h4>Thesis</h4>${claimList(a.sections.executive_thesis)}<h4>Physical intelligence</h4>${claimList(a.sections.physical_intelligence)}<h4>Evidence comparisons</h4>${claimList(a.sections.evidence_comparisons)}<h4>Risks and unknowns</h4>${claimList(a.sections.risks_and_unknowns)}
          <p class="note">Archived inputs: ${["stage1", "stage2", "stage3"].map(s => a.sources?.[s] ? `<a href="${esc(a.sources[s])}" target="_blank" rel="noopener">${s.replace("stage", "Stage ")}</a>` : "").filter(Boolean).join(" · ")}</p></details>` : ""}
       ${run ? `<details class="ev"><summary>Cost benchmark sources (${run.sources.length})</summary><ul>${run.sources.map(s => `<li>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a>` : esc(s.name)} · ${esc(s.document || "")}</li>`).join("")}</ul></details>` : ""}
       <details class="ev"><summary>Method and scope</summary><ul>${r.scope.map(s => `<li>${esc(s)}</li>`).join("")}<li>Verdict rule: ${esc(r.verdict.rule)}</li><li>Stage 3 screening output hash ${esc(r.stage3_hash)}; property ${esc(r.property.property_id)}, listing ${esc(r.property.listing_id)}, space ${esc(r.property.space_id)}.</li></ul></details>
       <p class="foot">Data: CoStar (property, listing, rent), Matterport (interior scan), Vexcel (aerial and oblique imagery), U.S. Census (jurisdiction), published franchise disclosure documents (opening costs), Capital One Nessie sandbox (cash simulation). Prepared by Helix.</p>`);
  }

  async function load() {
    const memo = $("#memo");
    if (!/^[0-9a-f]{32}$/.test(id)) { memo.innerHTML = '<p class="error">Open this page from an investigation to see its memorandum.</p>'; return; }
    try {
      const response = await fetch(`/api/final-report?id=${encodeURIComponent(id)}`, { cache: "no-store" });
      const r = await response.json(); if (!response.ok) throw new Error(r.error || "The memorandum could not be assembled.");
      document.title = `${r.selection ? r.selection.label + " · " : ""}${r.property.address} · Helix memorandum`;
      memo.innerHTML = `<article class="sheet">${cover(r)}${property(r)}${physical(r)}${opportunities(r)}${selected(r)}${financial(r)}${nextSteps(r)}${evidence(r)}</article>`;
    } catch (error) { memo.innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  }
  load();
})();
