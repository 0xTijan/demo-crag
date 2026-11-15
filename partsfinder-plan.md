# Plan — White-label local chat + Parts Alternative Finder for electronics distributors

## Context

Small electronics distributors (~10 users each) get customer requests for parts they don't carry. Today
salesmen browse datasheet PDFs by hand to find an equivalent from the lines they distribute. The product is a
company-branded, on-prem chat app (local Ollama only, consumer GPU) that:

- finds the closest alternative in *our* catalogue for any part number (dedicated **Parts Finder** tab + in chat),
- answers questions over all datasheets, does general assistant work, and builds presentations for our parts,
- has signup → **pending** → admin approval, **mandatory 2FA**, admin management,
- ships as Docker, installs in minutes, and is re-deployed per client with that client's branding.

It is a **new, separate repo**. From `demo-crag` we only reuse the crawler, PDF→Markdown and LLM→SQL pieces.
v1 has no stock/price data (catalogue = parts of the manufacturer lines the client distributes). Outbound
internet is allowed; only the LLMs and company data stay local.

## Decision: Open WebUI as the face — configured, not forked; all domain logic in our own service

| | Open WebUI (pinned image) | LibreChat fork | Build own |
|---|---|---|---|
| Pending role + admin approval | native (`DEFAULT_USER_ROLE=pending`) | missing, must build | build |
| Mandatory TOTP 2FA | native ("require authenticator for all users", recovery codes) | native | build |
| Admin panel (users, groups, per-model/tool access) | full | "Preview", incomplete | build |
| Local code interpreter | Jupyter engine, local | separate code-interpreter service | build |
| Tools | Python tools with rich HTML embeds + native MCP (Streamable HTTP) | MCP/agents | build |
| Stack | Python + Postgres (= yours) | Node + MongoDB + Meilisearch | yours |
| White-label | branding clause; **removal allowed for ≤50 users per deployment** (your clients ≈10) | MIT | free |
| Maintenance for a solo dev | upgrade = bump a tag | rebase a large fork forever | own every chat feature forever |

**Why:** Open WebUI covers every non-domain requirement out of the box (pending approval, 2FA, admin, RAG,
code interpreter, Ollama, Postgres). Forking anything means merging a fast-moving upstream for years, and
building your own means months of chat polish that adds nothing to the value you sell. The sustainable
split is: **upstream image untouched + your `parts` service plugged in through standard interfaces.**

**Risks and how the plan handles them**
- *Licence:* ≤50 users per deployment allows removing branding. Get a one-off legal confirmation, and track user counts per client; a client above 50 needs an OWUI enterprise licence or keeps OWUI branding.
- *Upstream churn:* pin an exact version. Upgrades go through a scripted smoke test (below) before rollout to all clients.
- *No custom tab without a fork:* Parts Finder is our own page at `/parts` on the same domain, linked from an OWUI admin **banner** and reachable in chat via a tool with a rich-UI result. A sidebar link is the only thing a fork would add, and it's not worth one.
- *Exit strategy:* all domain logic is behind REST + MCP, so the face can be swapped (LibreChat or your own UI) without touching the parts engine.

**MCP or fork for the parts search? Both entry points, one engine.** Logic lives in `parts-api`. Thin
adapters: (1) an OWUI Python tool (gets `__user__` and renders comparison tables as rich HTML in chat),
(2) an MCP endpoint (FastMCP, Streamable HTTP) for portability to any MCP-capable client, (3) the `/parts` page.

## Architecture (docker compose, one host)

```
caddy (TLS, routing)  ──/──────► open-webui (pinned)  ──► ollama (GPU)
                      └─/parts──► parts-api (FastAPI: REST + MCP + static Parts Finder UI)
open-webui ──tools──► parts-api          open-webui ──code interpreter──► jupyter (sandboxed)
parts-worker (crawl → download → Docling → extract → embed; Postgres job queue, no Redis)
postgres + pgvector  (DBs: openwebui, parts)      searxng (datasheet web search, no API keys)
backup (nightly pg_dump + datasheet volume + secrets reminder)
```

- **Auth for `/parts`:** `parts-api` forwards the OWUI session token to OWUI `GET /api/v1/auths/` and allows only role `user`/`admin`, so a pending user is rejected. The result is one login and one place for approval and 2FA. *(Phase 0 spike verifies the cookie/token path on the pinned version; fallback is Parts Finder only via the in-chat rich UI.)*
- **Models (12–24 GB VRAM):** a tool-calling chat model (e.g. qwen3 8–14B), `qwen3-vl:8b` for OCR fallback, a multilingual embedding model. The worker runs heavy extraction off-hours or when the GPU is idle (`OLLAMA_MAX_LOADED_MODELS`), like the "never Playwright + LLM at once" rule in `demo-crag/plan.md`.

## Parts engine (the core)

**Data model (`parts` DB):** `manufacturers`, `crawl_sources` (per client: manufacturer sites to crawl),
`datasheet_files` + `datasheet_errors` (port `report_files`/`report_file_errors` pattern from
`demo-crag/schema.sql`), `parts` (mpn, mpn_normalised, manufacturer, category, description, lifecycle,
`in_catalogue` = we carry it vs. looked-up external part), `part_attributes` (EAV: key, value_num in SI base
unit, value_min/max, unit, raw_text, page, confidence, method), `categories` + `category_attributes`
(**per-client registry**: key, unit, type numeric/enum/text, weight, match rule, hard/soft),
`datasheet_chunks` (pgvector, page-tagged), `cross_references` (confirmed alternatives from salesmen),
`search_log` + `feedback`.

**Category registry ("differs per client"):** ship a library of templates (resistors, MLCC/electrolytic caps,
inductors, diodes, MOSFETs, BJTs, LDO/DC-DC, op-amps, crystals, connectors…). The admin enables or edits them in a
Parts admin page. For unknown categories: generic key/value extraction, then the LLM proposes a schema, then an admin approves it.

**Ingestion (our catalogue, crawled from manufacturer sites):** port `dataPipeline/download/*` (crawler,
fetcher, downloader, settings) with a datasheet target instead of financial reports. Port
`dataPipeline/convert/*` (docling.py, common.py, ruled_tables.py, vision_ocr.py) unchanged in spirit:
Markdown with `<!-- page: N -->` markers, ruled-table fix and the vision OCR fallback. Datasheets are short, so full-document
Docling is fine. Status machine per file (`discovered → downloaded → converted → extracted → validated | needs_review`),
incremental by hash.

**Extraction:** classify category, then the LLM maps rows to the category's attributes (JSON schema, temp 0) and returns
**raw strings + page**. **Python parses units** (`4k7`, `4.7 kΩ`, `±1 %`, `-40…+125 °C`, `0402/1005M`),
verifies each raw string occurs in the Markdown, and rejects it otherwise (the principle from `demo-crag/plan.md` §1.4).
Validation rules per category put anything doubtful into `needs_review`, never into silent loading.

**Query part (not in our DB):** normalise MPN → exact or fuzzy hit in our DB? → else SearXNG `"<MPN> datasheet"`,
preferring manufacturer domains → download → same convert + extract path → stored as `in_catalogue=false`
(cached for the next salesman).

**Matching (parametric first, vectors second):**
1. Hard filters in SQL: same category, plus the hard attributes (e.g. package/footprint, mounting, pin count/pitch).
2. Rule-based score per attribute: `equal`, `≥` (Vds, voltage rating, power), `≤` (Rds_on, leakage),
   `range-contains` (temp range), `within ±x %` (value, tolerance), weighted from the registry.
3. Embedding similarity of description/features as a tie-breaker and for attributes with no rule.
4. Boost from `cross_references` and past accepted feedback; a known cross-reference always ranks first.
5. Output: ranked list labelled **drop-in / functional (different package) / partial**, per-attribute
   ✓ / ⚠ / ✗ diff table, datasheet page links for each value.

## UI surfaces

- **Parts Finder `/parts`** (small SPA served by parts-api, client branding): MPN box, then the extracted specs of the
  query part (editable, so a salesman can fix an attribute or relax a constraint), then a ranked comparison table, then
  👍/👎 "offered to customer" (feeds `cross_references`), then "add to presentation". Plus admin pages: crawl sources,
  category registry, review queue (`needs_review` with the PDF page beside the values, same idea as `demo-crag/plan.md` Phase 7).
- **Chat tools** (OWUI Python tool file → parts-api, also exposed via MCP):
  `find_alternatives(mpn, constraints?)` (rich HTML table), `search_datasheets(query, mpn?, category?)`
  (pgvector, page citations), `search_catalogue(category, filters)` (structured parametric search, safer than free SQL),
  `ask_catalogue_sql(question)` (fallback: port the generate → execute → fix/re-examine loop from `rag/numeric.py`
  and `rag/sql.py`, but against curated views with a **SELECT-only DB role** instead of the regex guard),
  `create_presentation(mpns[], title?)` (python-pptx with the client's `.pptx` template → download link;
  deterministic, because small local models write unreliable pptx code). The OWUI Jupyter code interpreter stays on for ad-hoc charts and analysis.
- **Branding:** `WEBUI_NAME`, logo/favicon via mounted static files, banner, login text, pptx template, all from one
  `branding/` folder per client. Asset paths are on the upgrade checklist.

## Repo layout (new repo, e.g. `partsfinder`)

```
deploy/   docker-compose.yml, Caddyfile, .env.example, install.sh (generates secrets, pulls models),
          upgrade.sh, backup/restore scripts, branding/ (example)
parts/    api/ (FastAPI routes, auth.py, mcp.py), engine/ (match.py, rules.py, units.py), ingest/ (crawl/,
          convert/ ← ported from demo-crag, extract/), worker.py, db/ (migrations: alembic), web/ (Parts Finder SPA),
          categories/ (template YAMLs), prompts/
owui/     tools/parts_tools.py (OWUI tool), config notes per pinned version
tests/    unit (units, rules, extraction parsing), gold/ (query MPN → accepted alternatives), e2e/ (Playwright)
.github/workflows/  build + push images to GHCR, run tests, smoke-test compose
```

`config.json` + `helpers/config.py:model_settings` pattern carries over as per-step model config, in env/YAML.

## Phases

0. **Spike (2–3 days):** pinned OWUI + Postgres + Ollama in compose; verify pending flow, mandatory MFA,
   branding asset swap, banner link, `/parts` token validation, Python tool rich embed, MCP registration.
   Write the licence-confirmation question for a lawyer.
1. **Deployable skeleton (≈1 wk):** repo, compose, Caddy, `install.sh`, CI images, backups (incl.
   `MFA_ENCRYPTION_KEY` and `WEBUI_SECRET_KEY`, since losing the MFA key breaks startup), DB migrations.
2. **Datasheet ingestion (1–2 wk):** port crawler + Docling pipeline, worker + job queue, crawl-source admin.
3. **Extraction + category registry (≈2 wk):** unit parser with exhaustive tests, templates for 5–6 common
   categories, review queue.
4. **Matching + Parts Finder page (≈2 wk):** rules, scoring, labels, feedback → cross_references; seed a small
   gold set by hand and grow it from feedback.
5. **Chat integration (≈1 wk):** OWUI tool, MCP endpoint, datasheet RAG, catalogue search/SQL, presentations.
6. **Hardening + first client (≈1 wk):** upgrade checklist/smoke test, logs, health checks, admin & install docs.

## Verification

- **Unit:** `pytest` for unit parsing (SI prefixes, ranges, tolerances, locale decimals), match rules, extraction
  raw-string verification.
- **Accuracy:** `tests/gold/eval.py`: for each query MPN, is an accepted alternative in top-1/top-5, and are there
  zero hard-constraint violations among drop-ins. Run on every prompt/model/rule change; the set grows from in-app 👍.
- **E2E (Playwright):** signup → user is pending and blocked from chat and `/parts` → admin approves → forced
  TOTP setup → chat calls `find_alternatives` and renders the table → `/parts` search → presentation download.
- **Ops:** fresh install on a clean VM with only `install.sh`; backup → wipe → restore; upgrade script on a
  copy of a client DB before every OWUI version bump.
