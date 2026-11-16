# Helix Stage 2 implementation report

Date: 2026-11-15
Branch: `feat/helix-stage-2-building-intelligence`
Base: validated Stage 1 `origin/feat/maximus-stage-1-discovery` at `06d95d5`

## Delivered

Stage 2 adds a selected-property investigation vertical slice to Helix. The Stage 1 **Investigate this property** action now calls the additive `/api/investigate` handler, validates and preserves the Stage 1 listing/suite/intake context, collects available CoStar, Matterport and Vexcel evidence, runs reconciliation and a building-only regulatory preflight, and persists a structured dossier for later retrieval at `/api/investigation?id=...`. The UI shows property identity, source status, physical evidence, contradictions, preflight findings, unknowns and the Stage 3 handoff. No properties or evidence are fabricated. Stage 1 discovery behavior and ranking were not changed.

The deterministic evidence engine checks identity, fact, scope, units, measurement semantics and time before comparing claims. Same-provider duplicate rows are not treated as independent corroboration. It labels gross floor area versus footprint as not comparable, and preserves Asmit's scan/tenancy timeline and attachment-date semantics from `playground/reconcile.py`. The three Grant, York Centre and Sam Houston regression patterns are represented in sanitized fixtures/tests; dated occupancy is not inferred from Matterport model creation/modification timestamps.

## End-to-end live check: CoStar property 236138

The live, read-only Stage 1 fixture selected CoStar property `236138`, listing `22141592`, suite/space `20324034`, at 15603 W Hardy Rd, Houston, TX. The investigation completed with CoStar, Matterport and Vexcel available.

- **CoStar:** property facts and the exact selected listing/suite were read from the existing local CoStar kit. Source identifiers, scope and known missing fields are retained.
- **Matterport:** explicit CoStar-linked model `NDMWnz1H9uV` matched the selected listing's exact normalized address. The live metadata query found 1 floor, 4 rooms and 36 available panorama locations; the dossier includes only four panorama references. Model timestamps are not represented as image capture dates. One authorized panorama face was retrieved and sent to `qwen/qwen3.8-flash` for one bounded visual call: 4,851 ms, 1,441 total tokens, reported cost `$0.00029967`. It returned three visual-only observations (exposed ceiling structure, linear light fixtures, ductwork), each tied to an image/frame identifier and normalized bounding box. The captured date remains unknown. Perceptron Mk1 was not run: its use is optional and the researched benchmark did not establish benefit for this basic scene-description call. No hidden-system, safety, engineering or compliance claims are made.
- **Vexcel:** all five read-only metadata operations returned HTTP 200: property extract (7 features), oriented coverage (25), oriented query (181), ortho coverage (15), and ortho dates (date metadata). The selected point yielded a structure candidate and imagery-derived roof/footprint signals; no automatic CoStar building identity is inferred from parcel geometry. Ortho metadata reports collection `us-tx-cypress-2026` with capture window on 2026-01-02. Image extraction remains disabled because permission/cost terms for extracts were not confirmed.
- **Reconciliation:** no definitive conflicts, no remaining possible conflicts, and one explicit not-comparable verdict for floor area versus footprint. The prior same-source pair explosion is fixed.
- **Compliance:** Jayant's `playground/compliance_engine.py` `preflight` interface is adapted through `stage2.property_preflight`; jurisdiction is resolved from property coordinates using his jurisdiction module. The live result is preliminary and not legal approval. `check_business` is not imported or called; the adapter explicitly reports that business-specific checks were not invoked.

The fixture's original Stage 1 intake and financial/listing context are preserved in the Stage 3 handoff; business recommendations are empty and financial feasibility is null.

## Architecture and files

- `project helix/stage2/`: validated handoff/evidence contracts, bounded CoStar/Matterport/Vexcel adapters, deterministic reconciliation, Jayant's isolated jurisdiction/preflight adaptation, optional one-image OpenRouter vision adapter, orchestration, and sanitized fixtures/tests.
- `project helix/app.py`, `project helix/costar_service.py`: additive Stage 2 API wiring, persisted investigation retrieval, and raw CoStar record access without changing the Stage 1-facing CoStar result contract.
- `project helix/index.html`, `project helix/maximus.js`, `project helix/maximus.css`: responsive investigation view and safe DOM rendering.

The `research/` directory was already untracked and is intentionally not included in the commit.

## Validation

- Stage 2: `python3 -m unittest discover -s 'project helix/stage2' -p 'test_*.py' -q` — 22 passed.
- Stage 0/1 regression: `python3 -m unittest discover -s 'project helix' -p 'test_*.py' -q` — 39 passed.
- `node --check 'project helix/maximus.js'` — passed.
- `python3 -m py_compile 'project helix/stage2/'*.py 'project helix/app.py' 'project helix/costar_service.py'` — passed.
- `git diff --check` — passed.
- Local app served with `--no-browser` on port 8123; `GET /` and `GET /maximus.js` both returned HTTP 200. The server was stopped after verification.

## Limitations and follow-up

1. No Vexcel image extraction or broad inventory sweep was performed. Metadata is live; image extraction terms remain unverified.
2. Matterport imagery has no verified camera-capture date in the current API response. The single VLM output is explicitly a visual description, not a building inspection.
3. Perceptron localization is optional and was not used in the live end-to-end call.
4. Compliance output is a preliminary property-level preflight. Zoning/permit applicability and legal approval remain unverified; no business-specific rules are run.
5. Source-specific provider calls are bounded, but providers currently run serially; bounded concurrency/caching can be added after review.
6. A pre-existing hard-coded CoStar ZIP-password fallback remains in `project helix/costar_service.py` (verified during review). It was not changed, and its value is intentionally omitted here. Replace it with an environment-only configuration in a separately reviewed security change.

No PR was opened, no changes were pushed or merged, no secrets were modified, and no legacy Maximo code was imported.

## Customer-acceptance repair follow-up — 2026-11-15

This follow-up supersedes the earlier “complete” wording above for customer acceptance. The original Stage 2 commit is `9560469`; the repair pass is on the same Stage 2 branch and remains local.

- The Stage 2 screen is a separate investigation page with a return-to-properties action, live provider progress, a prominent Matterport frame, an actual Vexcel image gallery, grouped physical findings, and expandable technical handoff data. Per-image observations are grouped for the customer view; source-backed observations remain in the Stage 3 payload.
- A real headless-browser journey submitted a valid LA intake, received 120 live Stage 1 CoStar results, selected the actual 1250 S Broadway listing, and used the normal `/api/investigate` and polling flow. The finished page showed all 5 extracted Vexcel images (aerial plus east, north, south, and west obliques) with capture dates and enlarged-image evidence actions. All 5 exterior image analyses completed.
- The exact-address Matterport model was matched and its Showcase iframe was embedded. The parent page and Matterport endpoints returned HTTP 200, but the browser screenshot still showed “Connecting to the Matterport space”; the navigable 3D tour is therefore **not verified as working** and remains the primary acceptance blocker.
- Default visual analysis ran without an enable flag. In that browser run, 27 of 28 Matterport panoramas produced findings and one remained unparseable after the then-current retry; 297 grounded visual observations were passed into the dossier, with 301 physical observations total. The Stage 3 handoff includes physical capabilities, visual evidence, physical unknowns, and source coverage. A third strict-JSON retry was added afterward and is covered by unit tests, but has not been revalidated against that panorama live.
- Completion is reported as partial when a required component is incomplete. This run correctly ended `Partially complete` because one interior panorama failed. No provider evidence was replaced with fabricated results.
- Browser screenshots from this real run: `/tmp/helix-stage2-real-browser-desktop.png` and `/tmp/helix-stage2-real-browser-mobile.png`. The mobile layout had no horizontal overflow at 390 px. The evidence view showed the Matterport loading state described above.
- Final automated checks after the repair edits: Stage 2 **26 passed**, Stage 0/1 **43 passed**, JavaScript syntax, Python compilation, and `git diff --check` passed. No further live API/browser run was made after the final bounded retry and finding-filter edits.

**Acceptance status: partial.** Vexcel imagery, automatic visual analysis, the grouped findings view, the handoff, and the browser journey work. The real Matterport Showcase never moved past its loading screen in the captured browser run, and the final retry refinement still needs one live verification. Do not present Stage 2 as fully accepted until those two items are resolved.
