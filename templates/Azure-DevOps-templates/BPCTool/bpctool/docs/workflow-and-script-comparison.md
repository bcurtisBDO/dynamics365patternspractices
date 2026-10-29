# BPCTool workflow & reference-script comparison

Engine order from `STEP_ORDER` in `runner/app/planner.py`. ADO paths via runner writers + `ado_client.py` / `catalog_runtime.py`. Comparison vs the Microsoft Azure DevOps Python reference scripts (phases 1–6) and the process / template model they implement.

**Reference compared:** the **July 2026** package, now archived at [microsoft/dynamics365patternspractices](https://github.com/microsoft/dynamics365patternspractices/tree/main/templates/Azure-DevOps-templates/Archive/July) `templates/Azure-DevOps-templates/Archive/July`: `setup_wizard.py`, `ado_setup_config.py`, `ado_setup_validation.py`, phase scripts `1_…`–`6_…`, and the `bpc_ado_import` package behind Phase 5. Microsoft moved it there unchanged on 2026-10-23 ([PR #371](https://github.com/microsoft/dynamics365patternspractices/pull/371), identical file contents; scripts last changed 2026-07-07, `dd61b871`). The earlier March 2026 `(Preview)` scripts are in `Archive/pre-OCT-2026`.

**October 2026 release:** Microsoft publishes only the template workbook, [`ADO template guidelines - OCT 2026.xlsx`](https://github.com/microsoft/dynamics365patternspractices/blob/main/templates/Azure-DevOps-templates/ADO%20template%20guidelines%20-%20OCT%202026.xlsx) (SHA-256 `f7aaa138…c64c2`), plus the catalog workbooks at [aka.ms/businessprocesscatalog](https://aka.ms/businessprocesscatalog). Its README calls the archived scripts "historical reference material, not the current October release", so there are no October scripts to compare against. The July package is the newest Microsoft automation. BPCTool is published in the same folder, at [`templates/Azure-DevOps-templates/BPCTool`](https://github.com/microsoft/dynamics365patternspractices/tree/main/templates/Azure-DevOps-templates/BPCTool) ([PR #370](https://github.com/microsoft/dynamics365patternspractices/pull/370), merged 2026-10-22). [October 2026 content](#october-2026-content-vs-the-july-scripts) below lists what changed in the workbooks and how each tool handles it.

Counts come from running the July importer's loader and BPCTool against the catalog workbooks: July counts against the July workbooks, October counts against the October ones.

BPCTool does **not** spawn those scripts — it reimplements the intent with Excel → IR → plan → apply.

**Quarterly releases:** Microsoft refreshes the ADO template guideline and the catalog workbooks quarterly. The reference scripts were last refreshed for July. BPCTool is not tied to one release: keep each release in its own folder (`~/BPC Files/<YYYY-Qn>/` with the template and catalog workbooks together — see USER_GUIDE §2 Folder layout). Load runner defaults selects the newest one and uses its name as **Workbook version**; folders not named `YYYY-Qn` (e.g. `OCT26-BPC Release`) are ranked by their newest workbook. The template is found by its `ADO template guideline*` name, which also matches October's plural *guidelines*. Sheet names and the matrix / L1–L4 conventions below are what BPCTool expects; re-check them when a new release lands.

**Assessment date:** 2026-10-27, against the archived July 2026 package and the October 2026 template and catalog workbooks. Covers orchestration, pre-run checks, template matrix (`X` shown / `H` hidden), Catalog parallel/resume/backfill, row filtering, area paths, incremental re-runs, conflict handling, and the October content changes vs reference Phase 5 / Scripts 1–6. Re-check this page when the next quarterly release lands.

---

## Workflow diagram

```text
Multivalue preflight (extmgmt)
        │
        ▼
   process  →  layout  →  project  →  teams  →  backlog  →  catalog  →  report
   WITs/fields   pages     create     areas     behaviors   parallel+     local
   picklists     controls  / bind     assign    iterations  resume+       artifacts
                                                            backfill
```

| Step | Purpose |
|------|---------|
| **Preflight** | Require DevLabs Multivalue on the org (live process / layout / project only) |
| **process** | Inherited process, WITs, picklists, custom fields |
| **layout** | Pages, groups, controls (`HtmlFieldControl`, MultivalueContribution); `H` fields on the WIT with hidden controls |
| **project** | Create or bind project to process |
| **teams** | Teams + area paths (reference Script 3 semantics) |
| **backlog** | Backlog behaviors, WIT assignment, iterations, team settings (reference Script 4) |
| **catalog** | Import catalog workbooks: prevalidate → parent-aware parallel create → resume → parent-link backfill |
| **report** | Local HTML/JSON summary in the run output folder (also written at the end of every run) |

**Live apply only:** Multivalue preflight runs when process, layout, or project is selected and **Rehearsal only** is off (**Run selected flow (live)**). **Preview plan** (plan only) and **Rehearsal only** (`apply_dry_run`: writers run with no ADO client; Catalog Import gets a read-only client for the change report) skip ADO mutation (and the Multivalue UI gate). **Report** never calls ADO. Full, dependency-aware, and run-from-selected-step-onward runs enforce the sequence above; selected-step mode can run a single card alone.

Recommended hard sequence (planner): `process → layout → verify → project → teams → backlog → catalog → report`.

### Execution modes (UI)

| Mode | Role |
|------|------|
| **Runner** (default) | Live Excel → IR → plan → apply via local FastAPI (`/api/jobs`) |
| **API** | Live ADO **reads** only (`/ado-proxy`) — credential proof / inventory; not process/layout/catalog apply |
| **Simulate** | Local stubs for orchestration testing |

Catalog/report have no API-mode executor; setup apply is Runner-only.

---

## Template matrix: X vs H (current assessment)

On the **ADO template guideline** workbook, sheet **Work item types**, cells in the field-label grid (roughly `K2:FU44` in recent releases) mark which fields apply to each WIT.

| Flag | Reference Script 2 | BPCTool (`excel_ir` / layout) |
|---|---|---|
| **X** | Applicable — add field to WIT and place layout control | Same — `applicable_field_labels`; a control hidden by an earlier `H` run is shown again |
| **H** | Ignored (not `"X"`) — no add / no layout bind | **Hidden** — `hidden_field_labels`: field added to the WIT (so values can be stored and imported) but no control is placed; an existing control (e.g. inherited system field) is set `visible: false` |
| empty / other | Skipped | Skipped |

The reference script only continues when `wit_field_flags.get(field_label) == "X"`; there is no path for `H`. In the guideline, `H` marks fields that belong on the type but should not show on the form. Typically these are **system effort/build fields** on inherited types (Completed Work, Activity, Original Estimate, Remaining Work, Integration Build, Stack rank, Estimate on Action item / Bug / Business task / Issue / Task / Test case), plus **Microsoft workshop type** on Workshop. The exact set varies by release; Preview plan reports `hidden_field_placements` for the workbook you load.

Hiding uses `PATCH …/layout/groups/{groupId}/controls/{fieldRef}` with `visible: false` (the Process API equivalent of "Hide from layout"). A hidden field that is **required** with no default value logs a warning, because users cannot fill it on the form. Catalog prevalidate treats `H` fields as on the WIT (required-field defaults apply).

**Verdict:** BPCTool extends the reference scripts — `H` is honoured as "on the type, hidden from the form" instead of being ignored.

**Process custom rules:** Neither the guideline sheets nor BPCTool apply WIT Process Rules (conditions/actions). Fields sheet **Required** / **Default value** drive layout + catalog defaults only, so a process created from the template has no custom rules. (Exporting source-org rules into a future Rules sheet is out of scope for current BPCTool.)

---

## Incremental re-runs & conflicts (current assessment)

Re-runs against an existing org/process/project are supported, but “incremental update” is **not** a full spreadsheet overwrite. Update policy gates **process** and **project** only; other steps are mostly create-if-missing / heal.

### Update policies (process / project)

| Policy | Behavior |
|---|---|
| **Require confirmation** | Fail plan/apply if process/project exists and confirm boxes are unchecked (process: Process, Layout or Backlog step; project: Project step) |
| **Always update** | Proceed; writers reuse/update existing targets (still create-if-missing, not full rewrite) |
| **Skip existing** | Skip process (and layout with it) and/or project (and teams/backlog with it) when targets exist. An existing process with a new project: Backlog runs only its project part (iterations, team settings) and leaves the process's backlog levels and WIT assignments alone |
| **Create only** | Fail if selected process/project already exists |

Catalog is **not** gated by update policy.

### Per-step re-run behavior

| Step | On subsequent run | Conflicts / collisions |
|---|---|---|
| **process** | Create missing WITs/picklists/fields (existing org fields are used as is); disable Excel-disabled WITs when possible | WIT / picklist / field create **409** or *already exists* → skip; any other create failure fails the step after every item was tried; update and disable failures are warnings. Field descriptions over 255 characters are shortened before create; a description Azure DevOps rejects (400) is retried shortened, then empty (warning + `descriptions-shortened.csv`) |
| **layout** | Add missing pages/groups/controls for `X` fields; add `H` fields to the WIT and hide their controls; re-show a control when `H` → `X` | Each type's fields and form are read once; fields already on the type are skipped. Field **409** → present; field **400** → try the boolean fallback payloads, then report it. Control **409** → present; does **not** remove controls for fields that became blank |
| **project** | Reuse existing; update description when UI description is non-empty | Does **not** re-bind process template on an existing project; stops with an error if the project was created from a different process |
| **teams** | Create missing teams/areas (area tree read once); PATCH team area assignments with *Include sub areas* from the Teams sheet, so a Teams-only re-run keeps it | Team/area create **409** → exists; team names matched ignoring case |
| **backlog** | Rename/heal/create behaviors; assign WITs; create missing iterations; team settings (teams in parallel, **Catalog workers**) | Duplicate custom levels healed (prefer system Agile); name already present → reuse |
| **catalog** | **Create-only fields** — keys in `ado-id-map.csv` **skipped** (no WIP field refresh); new keys created; then parent-link backfill. A rehearsal first reports field / parent / type differences against the existing items (`import-changes.csv`) | See backfill statuses below. Rows not in `ado-id-map.csv` (renumbered by a release, or all rows when the map is missing) are looked up by Microsoft ID first and reuse existing items; a row matching several items isn't created |
| **report** | Regenerates local HTML/JSON | N/A (no ADO) |

### Catalog parent-link conflict statuses

After create (and on re-run for mapped drafts), backfill writes `backfill-links-report.csv`:

| Status | Meaning |
|--------|---------|
| `linked` | Missing Hierarchy-Reverse added |
| `already_linked` | Expected parent already set |
| `conflict_existing_parent` | Different parent already exists — **not overwritten** |
| `failed` | API/other error |

If a parent never imports, children fail with `Parent not imported` (not created unlinked).

**Verdict vs reference Phase 5:** Same operational model (resume + Hierarchy-Reverse + conflict statuses). BPCTool runs backfill automatically after create; the reference importer exposes `backfill-links` as a separate CLI command that the wizard does not run. Neither refreshes field values for keys already in the id map.

---

## Catalog Import (current assessment)

Aligned with reference Phase 5 on the operational import model; still create-only for **field** updates (no WIP upsert).

| Capability | Behavior |
|---|---|
| **Hierarchy** | Title 1..N outline (ADO CSV / BPC guidance). Only `System.LinkTypes.Hierarchy-Reverse` is created — same as the reference scripts (no Related / Predecessor / etc.) |
| **Parallelism** | Heap ready queue + children-by-parent index + ThreadPool (UI **Catalog workers** → `catalog_parallel_workers`, default 4, clamp 1–16). A child is queued when its parent is created |
| **Connections** | Per-thread keep-alive HTTP(S) connections in `ado_client` (no new TLS handshake per request) |
| **Create retries** | Only 429/503 rejections are retried. A network error after send triggers a WIQL lookup (type + title + `MSBPC.microsoftid`): adopt one match, retry once if none, fail as possible duplicate if several |
| **Resume** | Load existing `ado-id-map.csv`; skip keys already imported; look the remaining rows up by Microsoft ID and reuse items no workbook row still claims (`matched-by-microsoft-id.csv`); append new rows |
| **Parent-link backfill** | Rows whose id-map `ParentId` matches (linked at create, or confirmed by an earlier backfill) → `already_linked` with no read. The rest: `workitemsbatch` reads with relations, 100 items per request, batches in parallel, + `PATCH` missing Hierarchy-Reverse; confirmed links are appended to `ado-id-map.csv`. Writes `backfill-links-report.csv` (`linked` / `already_linked` / `conflict_existing_parent` / `failed`) |
| **Cancel / progress** | `POST /jobs/{id}/cancel` stops after in-flight creates (recorded in the id map) and queued State updates (unconfirmed ones stay in `pending-state-updates.csv`), skips backfill; a request waiting out a rate-limit pause or retry delay stops without being sent; `catalog-progress` events drive a UI progress bar + ETA |
| **Failures** | If a parent never imports, children fail with `Parent not imported` (not created unlinked). `import-failures.csv` separates root causes from blocked children, explains each ADO error (TF401347, TF401324, invalid State, …) in plain language with a fix |
| **Prevalidate** | Local sanitize before create (`import-prevalidate.csv` / `.json`; also in Preview plan): `Your Project Name Here` → project name in Area / Iteration Path, Area Path and Iteration Path project-prefix rewrite, strip area paths not in the project (live: read from ADO), strip States the type doesn't allow (live: read from ADO after Process adds Test Case `New`; offline: Test Case `New` only when Process isn't in the run), shorten text over 255 chars, case-insensitive picklist normalize, required-field defaults, empty Description omitted (no synthetic placeholder) |
| **Not done** | WIT `$batch` creates (`$batch` is used only for States set after create) / native CSV bulk upload; WIP field upsert against existing work items; creating Area / Iteration nodes that only the catalog uses |
| **Deprecated / Deleted rows** | Skipped by default with every row below them (`skipped-deprecated-deleted.csv`); UI **Import Deprecated / Deleted rows** / `include_deprecated_deleted` imports them. A rehearsal labels items imported before the row was retired |

**Performance note:** Parallel workers cut wall time vs the former 1×N serial create loop (often several× when network-bound). Keep-alive connections and skipping backfill reads for links already recorded remove roughly one round-trip plus a TLS handshake per item; the rest of the backfill reads 100 items per request, and States set after create go 200 per `$batch` request. Start at 2–8 workers; higher counts risk ATCPU/429. Chunked `$batch` creates would reduce round-trips further but are not implemented.

Key modules: `writers/catalog.py`, `catalog_runtime.py`, `ado_client.create_work_item` / `get_work_items_batch` / `update_work_items_batch` / `add_parent_link`.

---

## Local runner APIs

Vite proxies `/api/*` → FastAPI on `127.0.0.1:8787`.

| Endpoint | Role |
|---|---|
| `GET /health` | Liveness + writer mtimes |
| `GET /defaults` | Newest release folder under `BPCTOOL_CATALOG` / `~/BPC Files`, its template, release label, output root |
| `POST /plan/dry-run` | Excel → IR → plan (**Preview plan**) |
| `POST /jobs` | Start live apply or rehearsal (`apply_dry_run`, UI **Rehearsal only**) |
| `GET /jobs/{id}?since=N` | Poll step results + events from index N (incremental) |
| `POST /jobs/{id}/cancel` | Cooperative stop → status `cancelled` |
| `GET /outputs/{folder}/{file}?root=` | Serve a known run output (report, failures / data-check CSV) for the results card |
| `GET /fs/roots`, `GET /fs/list` | Path picker Browse… |
| `POST /ir/parse` | Full IR dump (manual; UI does not call) |

Job body fields: `catalog_parallel_workers` (1–16; UI **Catalog workers**); `output_root` (UI **Run output folder**); `update_policy` + confirm flags for process/project gates. The UI stores the active job id in `sessionStorage` and reattaches after a page reload.

Browser inventory reads use `/ado-proxy/{org}` → `dev.azure.com`. Extension check/install uses `/extmgmt-proxy/{org}` → `extmgmt.dev.azure.com`.

---

## Azure DevOps APIs per step

Hosts:

- `https://dev.azure.com/{org}` — Process, WIT, Project, Work
- `https://extmgmt.dev.azure.com/{org}` — Multivalue extension management

| Step | Runner entry | Primary REST surface |
|---|---|---|
| **Preflight** | UI Connection / Run gate; `require_multivalue_extension` | `GET\|POST /_apis/extensionmanagement/installedextensionsbyname/ms-devlabs/vsts-extensions-multivalue-control` (`7.1-preview.1`) |
| **process** | `POST /api/jobs` → `apply_process` | `GET\|POST /_apis/work/processes` · WIT create/PATCH · `GET\|POST\|PUT /_apis/work/processes/lists` · `GET\|POST /_apis/wit/fields` |
| **layout** | `apply_layout` | GET `…/workItemTypes/{wit}/fields` and layout once per WIT (layout re-read after an add) · POST pages/groups/controls · POST `…/workItemTypes/{wit}/fields` for missing fields · `PATCH …/layout/groups/{group}/controls/{field}` (`visible` for `H`) · MultivalueContribution / HtmlFieldControl |
| **project** | `apply_project` | GET processes · `GET\|POST /_apis/projects` · `PATCH /_apis/projects/{id}` (description) |
| **teams** | `apply_teams` (reference Script 3) | `GET\|POST …/projects/{id}/teams` · `GET` classificationnodes/Areas tree once · `POST` missing area nodes · `PATCH` teamsettings/teamfieldvalues (`includeChildren` from *Include sub areas*) |
| **backlog** | `apply_backlog` (reference Script 4) | Process behaviors (GET/PUT/POST/DELETE) · WIT↔behavior · Iterations nodes · `GET\|PATCH` teamsettings · team iterations (teams configured in parallel, **Catalog workers**) |
| **catalog** | `apply_catalog` + prevalidate + `catalog_runtime` | Parallel `PATCH …/wit/workitems/$Type` (fields + optional Markdown format + optional Hierarchy-Reverse); resume via `ado-id-map.csv`; `POST …/wit/wiql` (Microsoft ID `IN` lists, 200 per query) + `POST …/wit/workitemsbatch` to find rows already in the project under another key, and to reconcile a create with unknown outcome; `POST …/_apis/wit/$batch` (200 `PATCH`es per request, api-version 4.1) for States refused on create; backfill `POST …/wit/workitemsbatch` with `$expand: relations` (100 items per request) + `PATCH …/workitems/{id}` for parent links not yet recorded in `ado-id-map.csv` |
| **report** | `apply_report` | None — writes `bpc-ado-setup-summary.json` / `.html` under `<Run output folder>/<org>_<project>/` (also written automatically at the end of every live run; Rehearsal writes `rehearsal-summary.*`) |

Key sources: `runner/app/planner.py`, `apply_engine.py`, `ado_client.py`, `catalog_runtime.py`, `writers/*.py`.

---

## Phase ↔ step map

| Reference phase script | BPCTool engine step |
|---|---|
| `1_ADO_Creation_Script.py` (process half) | **process** |
| `2_ADO_Page_Layout_Script_Threaded.py` | **layout** |
| `1_ADO_Creation_Script.py` (project half) | **project** (only after layout) |
| `3_ADO_Teams_Areas_Script.py` | **teams** |
| `4_ADO_Backlog_Config_Script.py` | **backlog** |
| `5_BPC_Catalog_Import.py` (+ `bpc_ado_import`) | **catalog** |
| `6_Generate_HTML_Report.py` | **report** |
| `setup_wizard.py` (`--start-at` / `--stop-after`) | UI workflow selection + run modes |
| `setup_wizard.py` sheet / PAT checks, `ado_setup_validation.py` | **Preview plan**, Connection checks, Multivalue preflight |
| `bpc_ado_import plan` / `import --dry-run` | **Preview plan** / **Rehearsal only** |
| `bpc_ado_import backfill-links` | Automatic backfill at the end of **catalog** |

---

## October 2026 content vs the July scripts

The October 2026 template has the same sheets as July's, including the unused `Fields-Old` sheet, which both tools ignore because they read `Fields` by exact name. Counts:

| Item | July | October |
|---|---|---|
| Work item types (3 disabled) | 40 | 40 |
| Fields (custom) | 161 (133) | 168 (140) |
| Picklists | 61 | 63 |
| Area paths | 121 | 123 |
| Teams | 47 | 47 |
| Iteration paths | 5 | 5 |
| Backlog levels | 6 | 6 |

The catalog workbooks hold 24,983 rows. These rows are what each tool would do with the October files, as found by running the July loader and BPCTool's Preview plan against them:

| October change | July scripts | BPCTool |
|---|---|---|
| **Template file name** `ADO template guidelines - OCT 2026.xlsx` (plural) | Any name: the wizard takes the path (`--excel-file` / `BPC_ADO_EXCEL_FILE`) | Found automatically in the release folder (`ADO template guideline*`), or picked with Browse… |
| **State `New` on every catalog row** (July filled none) — 8,242 imported rows are Test cases, and Test Case has no `New` state | Script 1 adds `New` (Proposed) to every Test Case reference it finds, and stops Phase 1 if none accepts it. If a create rejects the State, the importer creates the item without it and sets the State afterwards | Process step adds `New` (Proposed) to the process's Test Case, placed ahead of Design so it is the default state. If Azure DevOps refuses, it warns and continues; those rows start in Design. Data checks count `New` as valid when Process Template is in the run (Preview plan, rehearsal). Live runs check against the project's states after the Process step |
| **318-character field description** (`MSBPC.articlestatus`, Article status; July's was 173). Field help text is limited to 255 characters, and every one of the 24,260 imported rows fills this field | Script 1 sends the description as written. If Azure DevOps rejects the field, Phase 1 logs it and continues without it. Phase 5 then fails the first row with *Unknown target fields: MSBPC.articlestatus* and stops, because the wizard passes neither `--skip-unknown-fields` nor `--continue-on-error` | Preview plan warns. The Process step shortens it at a word boundary to 247 characters; the full text goes to `descriptions-shortened.csv`. A description Azure DevOps still rejects is retried shortened, then empty, so the field is always created |
| **Area / Iteration Path prefix** `Your Project Name Here` on every row (July: `MSBPCProj`) | Prefix replaced with the project name, so July's `MSBPCProj` problem doesn't arise. Every value is in the template's tree | Prefix replaced; no values removed (none outside the template's area tree) |
| **Deprecated / Deleted rows**: 723 (699 `70 - Deleted`, 24 `60 - Deprecated`) | Skipped with their child rows: 24,260 imported | Same: 24,260 imported, 723 listed in `skipped-deprecated-deleted.csv` |
| **Repeated Process sequence IDs** | None in October: 24,260 keys | Same: 24,260 keys (no `#` tracking keys needed) |

With Process Template in the run, BPCTool's data checks list only two other kinds of value for review in `import-prevalidate.csv`:

- 108 multi-select values not in the template's picklists
- 45 text values over 255 characters, shortened

---

## Re-running a July project with the October release

Both tools treat a re-run as an incremental update: the structure steps add what's new, and Catalog Import skips rows whose key (Process sequence ID) is in the project's `ado-id-map.csv`. Neither updates or removes existing work items. Between July and October Microsoft renumbered some rows (same Microsoft ID, new sequence ID) and gave some keys a new Microsoft ID, which only matching by Microsoft ID catches. Counts come from replaying each tool's July import into a simulated project, then its catalog logic on the October workbooks.

| Re-run behaviour | July scripts | BPCTool |
|---|---|---|
| **Existing process and project** | Reused by name, without checking which process the project uses | Reused after you confirm the update; stops if the project uses another process |
| **Existing organization fields** | Script 1 sends the workbook's name, type and description to the Fields update API and doesn't check the result. That API only locks or restores fields, so in practice the July definitions stay | Left as they are |
| **Where the ID map lives** | `out/<org>_<project>/` next to the scripts. Scripts copied into a new release folder start without it | `runner-out/<org>_<project>/` (or the chosen Run output folder), whichever release folder is used; a marker file stops another project's map being used |
| **Rows matched by key** | 14,705 skipped | 14,679 skipped |
| **Renumbered rows** (new key, Microsoft ID already in the project) | Created again: **32 duplicates** | Looked up by Microsoft ID before create: **66 rows reuse their items** (the ID map moves them to the new key); **no duplicates** |
| **Keys given a new Microsoft ID** (26) | Skipped: the key stays on the old item, and the row's new item is never created | The item follows its Microsoft ID to the renumbered row; 23 of these keys are created as new items, 3 reuse another existing item |
| **Rows created** | 9,555 (32 of them duplicates) | 9,515 |
| **Row matching several existing items** | Not checked (the importer looks items up only after a transient create failure) | Not created; reported in `import-failures.csv` with the item IDs, children blocked |
| **Missing ID map** | Every row is created again | Every row is looked up by Microsoft ID; matched items are reused |
| **Changed titles and types** (273 and 3) | Not updated, not reported | Not updated; a rehearsal lists them in `import-changes.csv` |
| **Changed parents** (16) | Nothing happens: the wizard doesn't run the parent-link check | Backfill reports `conflict_existing_parent`; not re-parented |
| **Items whose row is Deprecated / Deleted or gone** | Left in Azure DevOps, not reported | Left; a rehearsal labels them (23 now Deprecated / Deleted, 1 no longer in the workbook — the other gone keys were renumbered rows, now reused) |
| **Seeing it before the live run** | The importer has a plan mode, but the wizard doesn't expose it | Preview plan, and a rehearsal against the project: renumbered rows show as *Already in Azure DevOps under another key* |

The Microsoft ID check costs one WIQL query per 200 new rows plus one work-item read per 200 matches (54 requests for October), and is skipped when the project has no items with a Microsoft ID (a first import).

---

## Microsoft reference scripts vs BPCTool

| Area | Reference scripts | BPCTool | Verdict |
|---|---|---|---|
| **Orchestration** | `setup_wizard.py` runs the six phase scripts as subprocesses (`--non-interactive`), passing settings and the PAT in `BPC_ADO_*` environment variables; `--start-at` / `--stop-after` pick a phase range | In-process writers in local FastAPI runner; React UI posts `/api/jobs` and polls events; four run modes (selected step, dependency-aware, from step onward, full) | Improved / ported |
| **Pre-run checks** | Wizard checks the sheets each selected phase needs and the PAT (`GET /_apis/projects`); after phases 1–4 `ado_setup_validation.py` reads back WITs, fields, a layout, teams, areas, behaviors and iterations (missing items are warnings) | **Preview plan** parses the whole workbook, plans every step, runs the catalog data checks and the update policy; Connection validates the PAT and loads existing processes / projects. No separate read-back pass — each step reports what it created, skipped, or failed | Comparable (different shape) |
| **Sequence** | Phase 1 creates process + project together; layout is a later phase | Hard gate **process → layout → project** (layout before any project on full runs) | Improved / ported |
| **Excel handling** | Each script re-reads Excel during mutation | Parse once to IR → plan → apply; fingerprint + Preview plan + rehearsal runs | Improved / ported |
| **WIT×field matrix (`X`/`H`)** | Script 2: only `X` adds field/layout; `H` ignored (still true in July) | `X` adds field + control; `H` adds field to the WIT and hides its control (`visible: false`) | Improved |
| **Process custom rules** | Not authored in guideline Excel; not applied by phase scripts | Same — no Rules sheet / no `…/rules` apply | Parity (gap vs full process) |
| **Multivalue extension** | Phase 2 tries to install the DevLabs extension; if the PAT or org policy doesn't allow it, it logs a warning and **skips Multivalue controls** (Multi-select fields get no Multivalue control) | **Required** for process/layout/project live runs; Connection check + optional API install; Preview plan / rehearsal skip the gate | Stricter |
| **Process / fields / picklists** | Script 1 creates inherited process, WITs, fields, picklists | Ported via `apply_process`; multi-select → suggested picklists (`isSuggested`); re-run create-if-missing + limited PATCH | Improved / ported |
| **Disabled WITs** | Script 1 skips rows marked `Disabled` and tells you to disable the type by hand | Tries the Process API disable (`isDisabled`), falls back to the same manual instruction when the API refuses | Improved |
| **Test Case `New` state** | Script 1 adds a `New` state (Proposed category) to every Test Case type it finds (`7.1-preview.1`), counting a 409 as already there. If none accepts it, Phase 1 stops | Process step adds `New` (Proposed, color `b2b2b2`) to the process's Test Case if missing, with `order` set to Design's so `New` becomes the first state (checked afterwards; a refused `order` adds it without one); a 409 counts as already there. Any other failure is a warning, and those rows start in Design. Needed from October 2026, whose catalog sets `State = New` on every row (8,242 Test cases). Where `New` isn't first (an existing process), Azure DevOps accepts only Design when a Test Case is created, so Catalog Import creates it in Design and then sets `New`, as Microsoft's importer does | BPCTool also makes `New` the default state; continues where Phase 1 stops |
| **Long field descriptions** | Script 1 sends each field's description as written; a rejected create is logged and the field is skipped. October's 318-character Article status description is at risk: without that field, Phase 5 stops at the first row | Descriptions over 255 characters are shortened at a word boundary before create and flagged in Preview plan. Any field or work item type description Azure DevOps rejects is retried shortened, then empty. Full text in `descriptions-shortened.csv` | Improved |
| **Page layout** | Script 2 threaded layout + DevLabs MultivalueContribution | `apply_layout` mirrors Script 2, reading each type's fields and form once; boolean fields retry the fallback payloads after a 400; re-run adds missing controls (no stale remove) | Improved / ported |
| **Teams & areas** | Script 3 teams, L1–L4 areas, `set_team_area` | `apply_teams` — reference semantics; re-run upserts areas/assignments | Improved / ported |
| **Backlog & iterations** | Script 4 Parts 1–4: behaviors, WIT assign, defaults, team iterations | `apply_backlog` — rename/heal levels, WIT↔behavior, iterations, team settings (teams in parallel) | Improved / ported |
| **Incremental process/project** | Script re-runs with exist checks / warnings | Explicit update policies + confirm flags; writers still not full overwrite | Improved / ported |
| **Catalog hierarchy** | Parent-child Hierarchy-Reverse only | Same — Title 1..N; no other WIT relation types | Parity |
| **Catalog parallelism** | `--catalog-parallel-workers` parent-aware pool | UI **Catalog workers** / `catalog_parallel_workers` parent-aware ready queue; keep-alive connections | Improved / ported |
| **Create retry safety** | Transient failures (network errors, 408 / 429 / 5xx) are retried — the wizard sets 8 retries, 30 s base delay. Before each retry a WIQL lookup by Microsoft ID adopts the item if exactly **one** matches; with several matches it retries anyway | Only 429 / 503 rejections are retried. A network error after send is looked up by type + title + Microsoft ID: one match is adopted, none → one retry, several → reported as a possible duplicate instead of creating another | Improved |
| **Rate limits** | Each worker retries its own 429 (honouring `Retry-After` on that response) or 5xx with backoff; Azure DevOps' early warnings on successful responses (`Retry-After`, `X-RateLimit-*`) are ignored, so a run keeps its full pace until requests are blocked | One gate shared by every worker: a `Retry-After` on any response, including 200s, pauses all workers. When `X-RateLimit-Remaining` drops low or Azure DevOps reports a delay, requests are spaced out until the budget recovers. The run log notes it at most once a minute | Improved |
| **Deprecated / deleted rows** | Skipped by default: rows whose **Catalog status** contains *Deprecated* or *Deleted*, plus all their child rows, listed in `skipped-deprecated-deleted.csv` (`--include-deprecated-deleted` imports them). July workbooks: **700** rows (`65 - Deprecated (final)`). October workbooks: **723** (699 `70 - Deleted`, 24 `60 - Deprecated`) | Same default and same rule (700 July / 723 October rows skipped), listed in `skipped-deprecated-deleted.csv` and in Preview plan; **Import Deprecated / Deleted rows** (`include_deprecated_deleted`) imports them. A rehearsal reports items imported before their row was retired as *Deprecated or deleted in the workbook* | Parity; BPCTool adds the rehearsal label |
| **Repeated Process sequence IDs** | Rows repeating another row's sequence ID collapse into one key — July workbooks load **15,438** rows. October workbooks have no repeats (24,260 rows) | Repeats get their own tracking key (`<id>#<Microsoft ID>`) — **15,469** July rows, 31 more; October gives the same 24,260 | Improved for July; same for October |
| **Catalog Area / Iteration Path** | Replaces only the `Your Project Name Here` / `Project name` prefix, then **creates every missing Area and Iteration node** as written. The July workbooks prefix every path with `MSBPCProj`, so for any other project name it creates a `<project>\MSBPCProj\…` subtree and Azure DevOps rejects the work items' paths (TF401347). The October workbooks use the `Your Project Name Here` placeholder, which it replaces correctly | Rewrites the `MSBPCProj` (or placeholder) prefix to the target project. Never creates catalog-only nodes: area values not in the project's tree (built by Teams & areas from the template) are removed and listed in `import-prevalidate.csv`; Iteration Path is only re-prefixed. Every October area value is in the template's tree, so none are removed | Improved for July; same result for October; different policy (no node creation) |
| **Failure handling** | Phase 5 stops at the **first** failed row (the wizard doesn't pass `--continue-on-error`); orphans listed up to 20; `import-failures.json` | Keeps going: every failure recorded, children of a failed parent marked *Blocked by parent*; `import-failures.csv` explains each error with a fix | Improved |
| **Cancel / long runs** | Ctrl-C the script; rerun to resume | Runner cancel endpoint (cooperative, id map kept); progress bar + ETA; UI reattaches after reload | Improved |
| **Catalog resume** | Skip keys in `ado-id-map.csv` on rerun | Same append/skip resume model; rows not in the map are first looked up by Microsoft ID, so renumbered rows (or a missing map) reuse existing items instead of creating duplicates | Improved |
| **Catalog parent backfill** | `backfill-links` CLI (`linked` / `already_linked` / `conflict_existing_parent` / `failed`) | Automatic post-create backfill, batched reads (100 per request) → same statuses; links recorded at create or confirmed by an earlier run skip the read; conflicts **not** force-reparented | Improved / ported |
| **Catalog prevalidate** | Maps columns to fields, resolves WIT display names to the project's reference names, fails rows with fields the project lacks (or drops them with `--skip-unknown-fields`); warnings in `import-issues.csv` (`plan` command only) | Data checks on every Preview plan / rehearsal / live run: picklist case fix and removal, required-field defaults, 255-character limit, area / State checks against the project, single-select vs several values; each change in `import-prevalidate.csv` with a fix | Improved |
| **Catalog dry run** | `bpc_ado_import plan` and `import --dry-run` write `import-plan.json`, `import-preview.csv`, payloads (not exposed by the wizard) | **Preview plan** (no ADO calls) and **Rehearsal only** (`import-preview.csv`, `import-prevalidate.csv`, `import-changes.csv` against an existing project) | Improved |
| **Catalog field updates** | Create-only; the only update is setting a Test Case's State after a create that rejected it | Create-only fields (no WIP upsert); spreadsheet edits to mapped keys do not refresh ADO. Same State fallback: a State Azure DevOps refuses on create (Test Case `New`: only the first state, Design, is accepted then) is set after the create, 200 items per `$batch` request, and later rows of that type and State skip the refused attempt. Each update is tracked in `pending-state-updates.csv` until confirmed, so a stopped or failed update is retried by the next run. A rehearsal lists them: `import-changes.csv` compares the workbook with the project's items (from `ado-id-map.csv`, or matched by Microsoft ID) and lists rows a live run would reuse by Microsoft ID or not create (several matches) | Parity; BPCTool adds the change report |
| **Report** | Script 6 HTML from the import files and phase logs: counts, retry settings, failures reconciled with `ado-id-map.csv`, API status counts per phase log, **quick links** to the ADO project, Boards, Work Items and Process settings | Plain-language HTML/JSON (headline, what needs attention with fixes, steps, files) written after every run, plus a UI results card (no ADO calls); failures from an earlier run are labelled as such; links to the ADO project, Boards, Work Items and Process settings. No API status counts | Improved |
| **UI execution modes** | N/A (CLI scripts) | Runner = apply; API = reads only; Simulate = stubs | Improved / ported |
| **Edge-case parity** | Full script surface + many ADO quirk workarounds | Template-shaped, not line-for-line; residual: system WIT disable fragility, existing org fields not updated, Deliverables picklist mismatches | Gap / residual |
| **Observability / UX** | Per-script console/files; limited structured UI | Job events, Run timing, plan preview with data checks, results card, Excel-ready failures / data-check CSVs with fixes, id-map / backfill artifacts | Improved |

### Known residual vs the reference scripts

- Catalog-only Area Path nodes are not created: area values outside the project's tree are removed (the July importer creates every missing node). This changes nothing for the October workbooks, whose area values are all in the template's tree.
- The HTML report has no per-phase API status counts; failures are grouped by cause with a fix instead.
- No read-back validation pass after each step (the wizard's `ado_setup_validation.py`); steps report their own results.
- Not every reference-script option is ported (`--skip-unknown-fields`, `--recovery-field`, `--print-created-items`, `--disable-project-output-scope`).
- Catalog fields remain **create-only** (no bulk WIP upsert). Resume skips existing keys and reuses items matched by Microsoft ID; it does not refresh field values, titles or types. A rehearsal lists what differs (`import-changes.csv`) so you can update those items by hand.
- Parent-link `conflict_existing_parent` is reported only — no automatic re-parent.
- Existing projects are not re-bound to a different process template on re-run; a project on another process stops the Project step.
- Layout does not remove controls that are no longer marked `X` in Excel.
- No WIT Process Rules import/apply (the guideline also does not encode them).
- No WIT `$batch` creates or native Boards CSV bulk path yet (`$batch` is used only to set States after create).
- Backfill trusts `ParentId` recorded in `ado-id-map.csv` (at create, or confirmed by an earlier backfill); a parent changed by hand in ADO after that is not re-checked for those rows (a rehearsal reports it as *Parent differs*).
- Cancel is cooperative; one long-running ADO request finishes before the job stops.
- Inherited system WIT disable can still fail in the Process API (manual ADO UI fallback).
- Process doesn't change organization fields that already exist (the Fields update API only locks or restores them); a changed type, picklist or description in the workbook isn't applied to them.

See also: [USER_GUIDE.md](../USER_GUIDE.md) (§9 catalog, §10 re-runs, §17 limitations), [CHANGELOG.md](../CHANGELOG.md) (standalone replacement + residual risk).
