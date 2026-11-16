# MAXIMUS Stage 1 validation and legacy capability audit

Date: 2026-11-15 (UTC)

## Verdict: PASS WITH ISSUES

The Helix Stage 1 path is implemented and works against the supplied live CoStar kit. Focused regression tests pass. The validation exposed three defects in confidence, malformed-rent handling, and selected-property handoff; all three are fixed on `feat/maximus-stage-1-discovery` and covered below. The bounded live inventory is not exhaustive by design and is explicitly flagged in the UI/response. Explicit Tavily checking can return search results without proving the listing identity; this remains an honest `unverified` state.

No Stage 2 implementation, legacy imports, branch merge, or PR merge was performed.

## Helix changes

- `discovery.py`: reject nonpositive/reversed rate or area ranges; make confidence use four equal 25% factors and a deterministic property-search-fit score for the hard-filtered city/transaction; include the chosen lease horizon in budget confidence; bound Nemotron input to eight records, use the supported OpenRouter free-route request (no unsupported `response_format`), allow sufficient token room for reasoning plus JSON, and return a clear deterministic fallback for empty model content.
- `app.py`: a selected-property handoff now includes CoStar building facts and an explicit available/unavailable status.
- `test_discovery.py`, `test_intake.py`: cover malformed rent ranges, four-factor confidence and horizon, deterministic ranking, business-preference invariance, Nemotron fallback, and Houston's sale rule.
- `reports/maximus-stage1-selected-property.json`: sanitized, real CoStar-selected listing/handoff payload. It retains CoStar provenance, normalized finances, selected lease horizon, property coordinates/building facts, and verification status; third-party search-result payload is omitted.

## Verification evidence

| Check | Result |
| --- | --- |
| Unit suite (`python3 -m unittest discover -v`) | 17 passed |
| Python compile, JS syntax, `git diff --check` | Pass |
| Served app `/`, `/maximus.css`, `/maximus.js` | HTTP 200 each |
| Houston $500,000 live CoStar discovery | 1,000 returned / 2,482 available, 463 qualifying, 184 over-budget; 51 sale records; both sale and lease queried; marked truncated; zero Tavily discovery calls |
| Live Nemotron call | One call, `status=ok` |
| Reclassification ($400,000 occupancy / 10-year lease) | Succeeded from cached inventory, `reclassified_without_provider_calls=true` |
| Explicit verification on chosen listing | One cached Tavily result set; 5 claims remain `unverified`; no exact identity match established |
| Selected-listing handoff | Real CoStar building data available, same property ID, coordinates included; Stage 2 remains placeholder |
| Branch / PR | `feat/maximus-stage-1-discovery` @ `c87a759`; PR #18 open against Stage 0 branch; no merge |

The inventory trace is per current supplied kit and bounded request. A truncated result set can omit candidates, so counts/ranking must not be represented as exhaustive market coverage.

## Stage 1 handoff shape

The Stage 0 contract remains: `city`, `total_budget_usd`, `financing`, `business_preferences`, `open_to_ideas`, `transaction_types`, `acquisition_allocation`, and `lease_horizon_years`. Initial optional fields remain `null`; business fields are retained but do not influence Stage 1. The selected handoff adds a normalized selected listing, CoStar property facts, provenance, verification state, and a `stage2_placeholder` status. See the JSON artifact for a real example.

For the artifact's selected listing, `stable_id=lease:22141592:20324034`, `property_id=236138`, `listing_id=22141592`, `space_id=20324034`; asking rent normalizes from $6.50/SF/year and 5,941 SF to $3,218.04/month base rent. Property coordinates and building facts came from the CoStar property dataset. Tavily did not establish an exact listing match, so the record remains unverified.

## Read-only branch audit

The modules named in coordination notes are **not all on `feature/pilot`**. Inspection used Git tree/blob reads without switching branches or changing legacy files.

| Capability | Found on | Contract / evidence | Stage 1 relationship |
| --- | --- | --- | --- |
| `market_rank.py` / `load_market` / `rank_market` | Not present on `feature/pilot` or the checked location-demand/rent-estimate/data-pilot branches | No matching path found in the inspected branch trees or `git log --all -- '*market_rank.py'` | No module to import or compare; the present Helix ranking is separate and business-agnostic. |
| Rent normalization and listing financial evidence (`playground/deal.py`) | `origin/feat/rent-estimate` (`ce0ecd4`) and `origin/feat/physical-fit` (`5511c6d`); absent from `feature/pilot` | `monthly_rent()` converts explicit CoStar basis 1 or 2 to monthly total and $/SF/year. `lease_records()` retains asking-rate provenance and NNN/expense basis; `sale_records()` distinguishes asking from realized sale prices; missing/portfolio prices stay unknown. `test_deal.py` tests rate conversion, unknown rates, portfolio price, and contract fields. | Useful future source/conformance reference, but narrower than Helix's low/high range normalization and deterministic budget classification. Do not wire it into Stage 1 without approval. |
| Peer rent estimate (`playground/rent_model.py`) | `origin/feat/rent-estimate` (`ce0ecd4`); absent from `feature/pilot` | Quantile model trained/evaluated by held-out buildings with peer median baseline, coverage calibration, support thresholds, and fallback to similar-deal ranges; output file is generated/ignored. Unit tests cover basis conversion, peer support, method selection, and ranges. | Optional future enrichment only; it is an estimate and not a listing asking rent. Keep separate from Stage 1 affordability unless explicitly approved and represented as estimate/unknown. |
| CoStar ingestion (`playground/deal.py`, `space_fit.py`) | Same rent-estimate/physical-fit line | Filters active listing records and joins spaces/comps to a requested property ID; carries listing IDs and suite selection. Tests check active-only and property matching. | Helix already has a distinct bounded city inventory reader, stable listing/space IDs, raw evidence, and building lookup. There is useful parity inspiration, but no transplant warranted for this audit. |
| Original `feature/pilot` | `feature/pilot` (`676605f` local; `origin/feature/pilot` currently `6e6d300`) | A Houston/Tanner demo web/pilot flow (`pilot.py`, `web.py`, scenarios, tests). It is a building/business cash-flow pilot, not the general CoStar discovery/ranking engine cited in Slack. | Not suitable to merge into Stage 1; business-specific simulation is downstream. |

Coordination Slack claimed `market_rank.py` on `feature/pilot`, but the checked tree does not contain it. Slack says Helix should remain the final product and leaves integration approval for later; no integration decision is inferred from that message.

## Remaining limits and review

- Live inventory is capped and marked truncated; no claim of exhaustive discovery.
- Tavily verification is intentionally explicit and can remain unverified if the source does not tie back to exact listing identity.
- CoStar property facts are available for the tested selection; future consumers must treat unavailable as explicit and must not infer missing fields.
- Stage 2 remains a placeholder. No business recommendations, zoning determinations, renovation estimates, demand claims, or feasibility analysis were added.

Review PR #18 before merge. This audit does not authorize Stage 2 or reuse of the separate rent-model branch.
