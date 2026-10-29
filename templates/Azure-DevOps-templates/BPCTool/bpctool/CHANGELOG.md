# Changelog

All notable changes to BPCTool (React UI + local runner) are documented here.

## 2026-10 — Run results a standard user can act on

### Catalog Import

- **Faster.** Parent-link backfill reads 100 work items per request (13,912 single reads in the October import → about 140) and records each confirmed link in `ado-id-map.csv`, so later runs skip it; a recorded parent only counts for the work item it was confirmed on. States set after create go 200 per `$batch` request on a background thread, tracked in the new `pending-state-updates.csv` until confirmed; updates a stopped run never sent, or that failed, are sent by the next run (one per item if Azure DevOps refuses the batch)
- **Test Case State `New`.** The Process step adds `New` (Proposed) to the process's Test Case, as Microsoft's setup does (the October catalog sets `New` on about 8,000 Test cases), placed first (States API `order`) so Test cases are created as `New` directly. Where `New` isn't first (an existing process), Azure DevOps accepts only Design on create, so — as Microsoft's importer does — such items are created without State and then moved to it. This fixes about 7,300 failed rows; items that can't be moved are a warning, not failures
- **No duplicates from renumbered rows.** Rows whose key isn't in `ado-id-map.csv` are looked up by Microsoft ID (one WIQL query per 200 IDs) and reuse an item no other row claims; a key given a new Microsoft ID gets a new item; a row matching several items isn't created. Listed in `matched-by-microsoft-id.csv`. July → October: 66 items reused instead of 63 duplicates. If the lookup fails, those rows wait for a re-run
- **No duplicates from lost responses.** A create that times out, drops, or gets 500 / 502 / 504 is looked up four times over ~30 s (ignoring items mapped to other keys) and never sent again; if not found, the row fails with *re-run Catalog Import*. Parent links aren't re-sent after an unclear error either
- **`ado-id-map.csv` is safe.** A map saved by Excel (UTF-8 with BOM, no final line break) is read and appended correctly. Each results folder records its org + project in `bpctool-target.json`: projects whose names give the same folder name get separate folders, a map listing another project's items is refused (also when mixed with this project's), and a run with Catalog Import stops when none of the mapped items exist any more (project deleted and recreated) instead of skipping every row
- **Rows no longer lost.** Rows repeating another row's Process sequence ID (31 in July) are imported under their own key (`<id>#<Microsoft ID>`). Deprecated / Deleted rows and the rows below them are skipped by default, as Microsoft's importer does (`skipped-deprecated-deleted.csv`; **Import Deprecated / Deleted rows** checkbox / `include_deprecated_deleted`)
- **Data checks before import** (Preview plan, rehearsal, live), each listed in `import-prevalidate.csv`: Area / Iteration Path project prefix rewritten; `Your Project Name Here` replaced with the project name (also in template area / iteration paths and team backlog iterations); paths moved under a **Root area path** below the project; area paths and States the project doesn't have removed (live runs read the project's); text over 255 characters shortened; several values in a single-select field reported with the fix; unknown iteration paths flagged for review
- **Rate limits honoured** before Azure DevOps delays requests: `Retry-After` and `X-RateLimit-*` (also on HTTP 200) pause or space all workers together; a 429 / 503 pauses every worker. `Retry-After` is honoured up to 5 minutes (it was cut to 60 s), and the run log notes a longer request. Cancel stops a request waiting out a pause or retry delay within half a second, without sending it
- **Safer under failures.** A create answered 503 without rate-limit headers is treated like 500 / 502 / 504 (looked up, not re-sent); a failure to connect is retried (nothing was sent); a reply that isn't JSON is an error, not a crash. A new item's ID is written to `ado-id-map.csv` first; if that file can't be written, no more items start and the created IDs are listed as failures. A State update error leaves its rows pending for the next run. Appends follow the file's own column order (a map reordered in Excel); two identical rows can't claim the same item; Cancel stops the ~30 s lookup at once. A create reply without a work item ID is looked up like a lost response instead of failing the row. Parent links that backfill confirms are still reported when `ado-id-map.csv` can't be written (a warning; the next run checks them again). A failed append to `ado-id-map.csv` or `pending-state-updates.csv` is rolled back, so the file can't end in a cut-off row whose shortened ID would point at a different work item
- **Data fixes:** an iteration path naming another project and not in the template goes to the project's root iteration (reported); a level-1 area named like the project is kept; date and number cells are sent as ISO text / numbers; text such as `<Customer> reviews…` stays text instead of being taken for HTML; Markdown lists keep their numbering across blank lines and nest by indentation; whitespace-only rows and cells are ignored; `import-preview.csv` shows values after the data checks; a corrupt, encrypted or locked workbook gives a clear error

### Process, Layout, Project, Teams, Backlog

- **Steps report what really happened.** Process, Layout, Teams and Backlog fail (after doing everything else) when a type, picklist, field, form change, area, team or iteration can't be created, instead of warning and leaving Catalog Import to fail row by row. Update / disable failures, single-value fallbacks and project description failures are warnings
- **Re-runs converge.** Re-enables types the workbook enables; syncs field Required / Default value; uses the process's own copy of a type; changes a backlog level's default type in place; recognises either multi-select control id (`<field>.multivalue`); sends the project description only when changed; Teams keeps the sheet's *Include sub areas* and groups teams ignoring case
- Fix ([#372](https://github.com/microsoft/dynamics365patternspractices/issues/372)): new picklists (e.g. **Process type**) failed with *its picklist '…' wasn't found* on a first run; the Process step now uses the ID returned on create and retries lookups. Existing organization fields no longer get an update call (no more 404 per `MSBPC.*` field)
- **Long descriptions** over 255 characters are shortened at a word boundary, retried shorter or empty if refused, and listed in `descriptions-shortened.csv`
- **Projects.** Creation follows Azure DevOps' operation (up to 5 minutes), is never sent twice, and reports the failure reason; every run waits for a project still being set up; an existing project on another process stops the run; projects are looked up by name (not the first 100)
- **Layout** treats only a field already on the type as "already there" (boolean fallback payloads now run); a page, group or control Azure DevOps didn't confirm fails the step instead of being sent again
- **Backlog** counts as changing the process for the update policy, leaves an existing process alone under *skip existing*, and stops promptly on Cancel
- **More fixes:** Cancel stops Process, Layout, Teams and Backlog at their next request (Layout used to finish first). Integer picklists are created as Integer lists; picklist names match ignoring case (no duplicate list). Backlog also configures teams named only on the Area paths sheet; a team's backlog iteration is found with or without the project prefix, ignoring case, and an ambiguous name is a warning. *Rename from* a level that doesn't exist creates it. A 403 / 500 while checking an area or iteration fails that path at the end instead of stopping the step
- **Faster.** Layout reads each form once (about 10 minutes saved in the August run); Teams reads the area tree once; Backlog reads iterations, types and levels once and configures teams in parallel (about 3 minutes saved); Preview plan is 2–3× faster (workbooks cached until changed)

### Rehearsals and reports

- **Rehearsals read Azure DevOps** (read-only client; GET, `wiql` and `workitemsbatch` only) in the catalog step: data checks use the project's area paths and states, and *Changes since the last import* / `import-changes.csv` compares the workbook with the existing items (field, parent and type differences, missing items, retired rows, new rows), formatting differences ignored. Without `ado-id-map.csv`, rows are matched by Microsoft ID. Rows a live run would reuse or not create are listed, and *would import N* counts only workbook rows
- **Report after every run** (success, failure, cancel); rehearsals write `rehearsal-summary.*`. The HTML report and the new UI **results card** lead with a headline and *What needs attention* (largest first, with example rows and fixes), then steps, automatic fixes, files, and links to the project, Boards, Work items and Process settings. **Set up Catalog Import re-run** resumes in one click. Failures from an earlier run are labelled as such
- **Excel-ready CSVs:** `import-failures.csv` (*Fix this row* vs *Blocked by parent*, plain-language fix), `import-prevalidate.csv`. Text that could run as a formula is prefixed with `'` (not in files BPCTool reads back). JSON files share the run ID
- **Catalog columns in every row CSV.** Each CSV that lists catalog rows (failures, data checks, preview, skipped rows, changes, ID map, pending States, Microsoft ID matches, backfill report) ends with the row's *Catalog status*, *Microsoft ID*, *Mavim ID*, *Mavim reference* and *Partner ID* `MS BPC` values from the workbook; a file that already had one of these fields keeps it in place under the `MS BPC` name instead of repeating it

### UI, launch and runner

- New run mode **Run from selected step onward**. **Run output folder** setting (`output_root`). Existing projects and processes load automatically, and live runs check Azure DevOps before applying the update policy (Create only / Require confirmation were not enforced without a fetch)
- **Launch:** setup runs before the runner starts (fresh clone) and checks Node 20.19+ / 22.12+ and Python 3.11+ (`py -3` on Windows); packages reinstall only when lock files change; the runner starts with the venv's Python, so moving the folder doesn't break it; the browser opens once the runner is ready, and requests while it starts get a quiet 502 instead of proxy errors. `npm start` no longer uses `--reload` (a code change cut off runs). The launcher reuses a runner with a run in progress and leaves other programs on 8787 alone. Windows: paths with spaces, child process stopped with BPCTool, CRLF `.bat` files. Setup rebuilds a broken `.venv` (its Python removed or upgraded); the launcher waits for the runner it started, not an older one still on 8787, and finds the port's owner on non-English Windows; Excel lock files (`~$…`) are hidden in the picker; the drive list doesn't stall on a disconnected network drive
- **UI fixes:** Simulate / API runs don't mark steps complete for Runner; no double start; Cancel at the last moment no longer marks a finished run cancelled; extension and list checks wait for typing to stop and re-check after an org URL / PAT change; screen readers announce the run message; blocked browser storage no longer gives a blank page; durations show as `1h 55m`; the path picker keeps keyboard focus inside it (page behind is inert) and returns it to **Browse…** on close. **Run** is disabled when the plan has nothing to run (it used to start every step live); steps the runner skipped aren't cached as completed; another run no longer forgets a still-running job a reload can reattach to; API mode doesn't ask for the Multivalue extension; Preview plan results clear when inputs change and runner errors are readable; the run-mode options are one keyboard group; an org URL with an invalid organization (`dev.azure.com/_apis`) is refused and `org.visualstudio.com/DefaultCollection` accepted. Plain-language header (**BPCTool**)
- **Runner:** job `steps` must name known steps (HTTP 422); a failed first check still writes a report; only one rehearsal / live run per org + project (HTTP 409); jobs keep only the last 20 and forget their PAT when done; `npm run runner*` scripts work on Windows; new `runner:install-dev` / `runner:test`
- **Security:** no wildcard CORS and only `127.0.0.1` / `localhost` Host headers (blocks other sites and DNS rebinding); changes requested by another website refused (`Origin` / `Sec-Fetch-Site` check, JSON bodies only; FastAPI 0.141.1+); network paths (`\\server\share`) refused, including for the template, catalog folder and output folder; validation errors don't echo the request (PAT); dev dependencies updated for `npm audit`

### Docs

- One root [`README.md`](../README.md) (app and docs READMEs merged); new [Quick reference](./docs/QUICK_REFERENCE.md) and overview infographic; screenshots retaken
- PAT scopes by run type; sessionStorage wording; what reports contain; Node requirement corrected
- [Workflow & reference-script comparison](./docs/workflow-and-script-comparison.md) re-checked against Microsoft's archived July 2026 scripts and the October 2026 workbooks (new *October 2026 content* section); `docs/reference/MSBPC-process-analysis.md` removed

## 2026-10 — Clearer run modes

- **Preview plan** (Excel only), **Rehearsal only** (runs the steps without changing Azure DevOps; amber note), **Run selected flow (live)** (red, names the target). API names unchanged
- Fix: an invalid or expired PAT (HTTP 203 sign-in page) no longer shows the Multivalue extension as installed

## 2026-10 — Folder layout: release folders, output root, `bpctool.env`

- No hard-coded user paths: catalog root defaults to `~/BPC Files`; `/defaults` picks the newest release subfolder (`YYYY-Qn`, `_` / `.` ignored) and pre-fills **Workbook version**
- `BPCTOOL_OUTPUT_DIR` overrides `runner-out`; launchers read `BPCTOOL_*` from `bpctool/bpctool.env`
- Preview plan warns about mixed releases or template and catalog from different release folders

## 2026-10 — Release-agnostic template and catalog

- Template found by `ADO template guideline*.xlsx` in the catalog folder; no code, UI or docs tied to one quarterly release; new-release checklist in the User Guide; `IR_VERSION` → `2026.10-1`

## 2026-10 — Work item types matrix: `H` = hidden field

- `H` fields are added to the type without a visible form control (inherited controls are hidden; `H` → `X` shows them again); a required `H` field without a default is a warning

## 2026-10 — Catalog performance, safe retries, cancel / reattach

- Keep-alive connections per worker (proxy settings honoured); jittered backoff and `Retry-After`; HTTP 203 is an auth error
- Creates are retried only when Azure DevOps rejected them (429 / 503); a lost create is looked up instead (see above for the current rules)
- Heap ready queue (children start as soon as their parent exists); backfill skips links made at create
- **Cancel run** (`POST /jobs/{id}/cancel`): stops between steps and after in-flight creates; re-run resumes. Reloading the page reattaches to a running job; catalog progress bar with ETA; incremental `GET /jobs/{id}?since=N`. **Threads** renamed **Catalog workers**

## 2026-08 — Catalog, multi-value, teams and launch

- **Catalog Import:** all matching workbooks in the folder; parents from **Title 1..N** (Process sequence ID is only a key); parent-aware parallel workers (1–16, default 4); resume from `ado-id-map.csv`; parent-link backfill (`backfill-links-report.csv`); empty Description omitted; required fields filled from Fields-sheet defaults; picklist values matched case-insensitively
- **Multi-select fields** use suggested picklists and the DevLabs **Multivalue** control; the extension is required (checked, and installable, from the Connection panel and the runner)
- **Process / backlog:** Excel-disabled types kept and disabled; backlog levels renamed / created from the **Backlogs** sheet (healing earlier duplicate levels); project description field
- **Teams / iterations:** all of a team's area paths in one update; team backlog iteration set from the iteration root before selecting iterations
- **Launch and UI:** Vite starts and stops the runner; double-click launchers (`Start BPCTool.command` / `.bat`); org URL normalised; Browse… path picker; run modes Selected step only / Dependency-aware / Full; Run timing per step; profiles removed (org URL + PAT session only); wider layout
- Template is user-supplied (not bundled); repo scaffolding and duplicate READMEs / changelogs removed; docs and screenshots kept in sync

## 2026-08 — Standalone runner replaces the reference scripts

- The React UI orchestrates configuration, policy and progress; a local FastAPI runner (`127.0.0.1:8787`, proxied as `/api`) parses Excel, plans and applies through the Azure DevOps REST API. No subprocess calls to Microsoft's scripts
- Pipeline: Excel → IR → plan → apply → report, in step order `process → layout → project → teams → backlog → catalog → report` (process and layout before any project)
- Update policies (require confirmation / always update / skip existing / create-only), workbook fingerprints, Simulate / API / Runner execution modes, Preview plan
- Rich text: Markdown kept by default (`multilineFieldsFormat=Markdown`) or converted to HTML; HTML cells passed through
- Known limits at the time: catalog fields create-only; jobs in memory only; not every reference-script edge case ported

## Earlier baseline (pre-runner)

- React workflow cards, Simulate executors and Azure DevOps read adapters; connection, policy and fingerprint foundations later reused by the runner
