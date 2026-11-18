# Part update plan: how indexed data is kept up to date

This file describes how the indexed supplier data is updated after the first full indexing run.
It builds on the v1 plan ([`parts_alternatives_plan.md`](parts_alternatives_plan.md)): spec lists in
`spec_lists/*.yaml`, results in the index cache (`index_cache/…`), loaded into `catalog_parts`.

**The principle:** only what changed is re-run. Every cached result records the datasheet's file hash, the
extractor version and the spec-list version it was made with. An update compares these with the current state
and sends to the LLM only the datasheets whose result is out of date.

---

## 1. What triggers an update

| Trigger | Example | What is re-run | Usually |
|---|---|---|---|
| **New datasheet** | a supplier adds a new MOSFET datasheet | classify + index that datasheet | two-weekly run |
| **Changed datasheet** | a supplier publishes REV.B of the MLCC datasheet | classify + index that datasheet | two-weekly run |
| **Removed datasheet** | a supplier drops a product line | nothing; its parts are marked removed | two-weekly run |
| **Spec added to a type** | add `vgs_max` to MOSFETs | that type's datasheets, **only the new spec** | on request |
| **Spec rule changed** | `<=` → `≈` | nothing; only the scoring changes | on request |
| **New type supported** | a spec list for relays is written | that type's datasheets | on request |
| **Extractor changed** | a better prompt or a new model | the affected types (or everything) | rarely |

Updates never touch the `matches` table directly. They only mark matches for re-check (section 5).

---

## 2. The two-weekly run

Runs automatically, e.g. every second Saturday night. Steps in order:

```
1. SCAN        list all markdowns in markdowns/suppliers/ and compute their file hashes
2. COMPARE     against the index cache → new / changed / removed / unchanged
3. CLASSIFY    new and changed datasheets: first pages → component type
4. INDEX       new and changed datasheets whose type has a spec list → key specs (LLM)
5. CHECK       raw-text check + min ≤ max check on every new value
6. WRITE       cache files (index_cache/…), then load into catalog_parts
7. RE-CHECK    matches whose compared specs changed → needs re-check
8. REPORT      summary of what happened (section 6)
```

Details:

- **Scan and compare (minutes):** a datasheet is *unchanged* when its cache file has the same file hash,
  extractor version and spec-list version. Unchanged datasheets are skipped. This is normally > 95% of them.
- **Changed datasheet:** treated like a new one. The old cache file is kept until the new result has passed
  the checks, so a failed run never leaves a part without data.
- **Removed datasheet:** its parts are marked `removed` in `catalog_parts`, not deleted. They no longer appear
  as candidates. Confirmed matches pointing to them stay, shown as "part no longer in the catalogue".
- **Failures:** if the LLM call fails or the output can't be read, the datasheet keeps its previous result
  (if any), and the failure is listed in the report. The next run tries it again.
- **Search keeps working during the run.** The database is updated per datasheet at the end of step 6, so
  users see either the old or the new result, never a half-written one.

**Time:** with a few dozen new or changed datasheets, usually minutes to an hour (estimates; see the
indexing-time section of the plan).

---

## 3. Spec-list changes (on request)

Example: add the maximum gate-source voltage to MOSFETs.

1. **Edit the YAML and raise its version** (2 min):
   ```yaml
   type: mosfet_n
   version: 4                     # was 3
   specs:
     ...
     vgs_max: {label: "V(GS) max", unit: V, rule: ">=", aliases: ["VGS", "Gate-Source Voltage"]}   # new
   ```
2. **Check it on ~10 sample datasheets** (~10 min): is `vgs_max` found in most of them? An engineer
   confirms the direction (`>=`) and whether it's critical.
3. **Start the update for that type.** The indexer finds cache files with `mosfet_n@3` while the list is
   now `@4`, and re-runs exactly those datasheets:
   - **New spec only (default):** the LLM is asked only for `vgs_max`; the answer is merged into the existing
     cache files. Small output, so several times faster.
   - **Full re-extraction:** all MOSFET specs again. Only needed when the type's other specs also changed.
4. **Load into the database** (minutes), then the re-check step and the report, as in the two-weekly run.

| Time (assuming ~600 MOSFET datasheets) | One at a time | Parallel |
|---|---|---|
| New spec only | ~1–2 h | ~10–20 min |
| Full re-extraction | ~4–6 h | < 1 h |

Plus ~15 minutes of human time for steps 1–2.

### Effect of adding a spec

| | Effect |
|---|---|
| Other types | untouched |
| Search during the update | keeps working; parts not yet updated show `?` (not stated) for the new spec |
| Confirmed matches | stay valid: the new spec wasn't compared (decision 1 in the plan) |
| Cached external parts of that type | get the new spec in the same run, or the next time they're requested |
| Code | no change |

### Other kinds of spec-list change

| Change | What runs | Note |
|---|---|---|
| **Rule changed** (`<=` → `≈`) | nothing is re-indexed | Saved matches compared under the old rule may deserve a quick engineer look; the report lists them. |
| **Spec removed** | nothing | Stored values are simply ignored. |
| **Spec renamed** | avoid | Stored values and saved matches use the old name and would need migrating. Add the new spec and stop using the old one instead. |
| **New kind of rule** (beyond `=`, `>=`, `<=`, `≈`, `contains`) | nothing is re-indexed | A few lines in the comparison function. |

---

## 4. New type and extractor changes

- **New type supported:** create the spec list (~45–60 min, as described in the plan), then run the update
  for that type. Its datasheets were already classified, so only the indexing step runs.
- **Extractor changed** (prompt, model, compact output format…): raise the extractor version. Re-run one type
  first and compare old and new results on the test set; only then re-run the other types, ideally overnight.

---

## 5. What happens to saved matches

Each match stores a copy of the specs that were compared when it was proposed.

| After an update… | The match |
|---|---|
| a compared spec of either part changed | goes to **needs re-check**; an engineer looks again |
| only specs that weren't compared changed, or a spec was added | stays valid |
| the catalogue part's datasheet was removed | stays, shown as "part no longer in the catalogue" |
| a rule of the type changed | stays valid; listed in the report for a quick look |

Matches are never deleted or changed by an update; only their status can move to *needs re-check*.

---

## 6. The update report

Every run ends with a short report (a file in the repository and/or a message to whoever maintains the system):

```
Update 2026-11-28 (two-weekly)
  datasheets scanned        10,214
  unchanged (skipped)        10,151
  new                            41   → indexed 38, type not supported 3
  changed                        19   → indexed 19
  removed                         3   → 12 parts marked removed
  failed                          2   → see list below (kept previous result)
  unverified values              14   → see list below
  matches → needs re-check        4   → see list below
  time                       47 min
```

What to look at:
- **failed** datasheets: usually a conversion problem; fix and they're retried next run.
- **unverified** values: wrong or broken values in the source, or an extraction mistake.
- **needs re-check** matches: go to an engineer.
- **type not supported**: if one type keeps showing up, it's a candidate for a new spec list.

---

## 7. After every update

1. **Commit the changed cache files** to the company's git repository. Only the files of datasheets that
   changed are in the commit.
2. **Database backup** of the `matches` table, as usual (it's not in the cache).
3. A new installation needs no re-run: it loads the cache from git.

---

## 8. Who does what

| Task | Who | How often |
|---|---|---|
| Two-weekly run | automatic | every two weeks |
| Read the report, retry failures | whoever maintains the system | every two weeks, ~15 min |
| Re-check flagged matches | engineer | as they come up |
| Spec-list changes, new types | maintainer + engineer for review | on request, ~15–60 min each |
| Extractor changes | maintainer | rarely, tested on one type first |
