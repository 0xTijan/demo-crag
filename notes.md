What temperature means

At each step the model gives every possible next token a probability, then picks one. Temperature changes how it picks:

- 0: it always takes the most likely token. The same input gives the same output every time.
- 1 (your current setting): it samples from the model's raw probabilities. A token with a 5% chance gets picked about 1 time in 20. If that token is > instead of >=, or NARRATIVE instead of NUMERIC, the answer is wrong.
- Above 1: the choice gets flatter and more random, which is more "creative" but less reliable.

top_k 64 and top_p 0.95 limit which tokens can be sampled at all, but inside that set it's still a draw.

Classification, entity extraction and SQL each have one correct answer, so randomness only adds mistakes. With temperature 0, a wrong SQL query is wrong the same way every time, so you can reproduce it and fix the prompt. With temperature 1, the same question can pass on one run and fail on the next. Recommended settings:

┌──────────────────────────────────────────┬─────────────┐
│                   Step                   │ temperature │
├──────────────────────────────────────────┼─────────────┤
│ reformulator / classifier / SQL / fixing │ 0           │
├──────────────────────────────────────────┼─────────────┤
│ conclusion (wording only)                │ 0–0.3       │
└──────────────────────────────────────────┴─────────────┘

client.chat(model, messages=messages, format=SCHEMA, options={"temperature": 0, "num_ctx": 8192})
│ reformulator / classifier / SQL / fixing │ 0           │
├──────────────────────────────────────────┼─────────────┤
│ conclusion (wording only)                │ 0–0.3       │

❯ What does the temperature mean; how would you merge the reformulator and classifier

  Ran 1 shell command

What temperature means

At each step the model gives every possible next token a probability, then picks one. Temperature changes how it picks:

- 0: it always takes the most likely token. The same input gives the same output every time.
- 1 (your current setting): it samples from the model's raw probabilities. A token with a 5% chance gets picked about 1 time in 20. If that token is > instead of >=, or NARRATIVE instead of NUMERIC, the answer is wrong.
- Above 1: the choice gets flatter and more random, which is more "creative" but less reliable.

top_k 64 and top_p 0.95 limit which tokens can be sampled at all, but inside that set it's still a draw.

Classification, entity extraction and SQL each have one correct answer, so randomness only adds mistakes. With temperature 0, a wrong SQL query is wrong the same way every time, so yoe prompt. With temperature 1, the same question can pass on one run and fail on the next. Recommended settings:

┌──────────────────────────────────────────┬─────────────┐
│                   Step                   │ temperature │
├──────────────────────────────────────────┼─────────────┤
│ reformulator / classifier / SQL / fixing │ 0           │
├──────────────────────────────────────────┼─────────────┤
│ conclusion (wording only)                │ 0–0.3       │
└──────────────────────────────────────────┴─────────────┘

client.chat(model, messages=messages, format=SCHEMA, options={"temperature": 0, "num_ctx": 8192})

Merging the reformulator and classifier

Today you run two calls that do overlapping work:

┌─────────────────────────────────────────────────┬─────────────────────────────────────────────────┐
│              Reformulator (~20 KB)              │               Classifier (~22 KB)               │
├─────────────────────────────────────────────────┼─────────────────────────────────────────────────┤
│ scope (in scope / chitchat / out of scope)      │ —                                               │
├─────────────────────────────────────────────────┼─────────────────────────────────────────────────┤
│ language                                        │ keep the user's language                        │
├─────────────────────────────────────────────────┼─────────────────────────────────────────────────┤
│ resolve history and pronouns → refined_question │ rewrite → numeric_question / narrative_question │
├─────────────────────────────────────────────────┼─────────────────────────────────────────────────┤
│ blocking_ambiguity + clarification_question     │ needs_clarification + clarification_question    │
├─────────────────────────────────────────────────┼─────────────────────────────────────────────────┤
│ —                                               │ NUMERIC / NARRATIVE / MIXED, entities           │
└─────────────────────────────────────────────────┴─────────────────────────────────────────────────┘

Clarification is handled twice, rewriting happens twice, and the classifier never sees the history. That's a problem, because the reformulator can only pass that context on through refined_question.

One router call with one schema

Field order matters. The model writes JSON top to bottom, so the rewrite should come before the routing fields. That way the model rewrites first and then classifies its own rewrite ier the two-call version follows today.

ROUTER_SCHEMA = {
  "type": "object",
  "properties": {
    "scope":    {"enum": ["IN_SCOPE", "CHITCHAT", "OUT_OF_SCOPE"]},
    "language": {"enum": ["en", "sl", "other"]},
    "direct_reply": {"type": ["string", "null"]},           # chitchat / out-of-scope
    "needs_clarification": {"type": "boolean"},
    "clarification_question": {"type": ["string", "null"]},
    "suggestions": {"type": "array", "items": {"type": "string"}},
    "refined_question": {"type": ["string", "null"]},       # history resolved, self-contained
    "assumptions": {"type": "array", "items": {"type": "string"}},
    "classification": {"enum": ["NUMERIC", "NARRATIVE", "MIXED", None]},
    "entities": {
      "type": "object",
      "properties": {
        "companies_mentioned": {"type": "array", "items": {"type": "string"}},
        "portfolio_wide": {"type": "boolean"},
        "fiscal_years": {"type": "array", "items": {"type": "integer"}},
        "latest_period_requested": {"type": "boolean"},
        "metrics": {"type": "array", "items": {"enum": [...]}}  # the real DB metric list
      },
      "required": ["companies_mentioned", "portfolio_wide", "fiscal_years",
                   "latest_period_requested", "metrics"]
    },
    "numeric_question": {"type": ["string", "null"]},
    "narrative_question": {"type": ["string", "null"]},
    "narrative_depends_on_numeric": {"type": "boolean"}
  },
  "required": [...all of the above...]
}

Two things come for free here:
- Putting the metric names in an enum means the model can't invent cogs or profit. That fixes the metric-mismatch problem from my earlier list in the same change.
- I dropped the classifier's reasoning field. It costs tokens on every call, and the order of the rewrite fields already does that job.

Control flow

The two recursive functions become one loop in main.py:

history = []
q = input("What is your question? ")
while True:
    r = route(history, q)                     # the single LLM call
    history.append({"role": "user", "content": q})
    if r["scope"] != "IN_SCOPE":
        reply = r["direct_reply"]
    elif r["needs_clarification"]:
        reply = r["clarification_question"]   # + print suggestions
    else:
        dispatch(r)                           # numeric / narrative / mixed
        reply = None
    if reply:
        print(reply); history.append({"role": "assistant", "content": reply})
    q = input("> ")

This also fixes a bug where the clarification branch in promptClassifier called promptReformulator and then recursed.

The merged prompt

Write one prompt instead of joining the two files together. Structure it like this:
1. Role, plus "never answer the question."
2. Scope: what the system can and can't answer (from the reformulator).
3. Data sources and the real metric list, with synonyms (from the classifier, corrected).
4. Resolving history (reformulator §4).
5. Rewriting and splitting into numeric and narrative questions (classifier §3).
6. Entity extraction (classifier §4).
7. Clarification: one set of rules. Ask only when it truly blocks the answer; otherwise make an assumption.
8. About 8 full examples covering every branch.

Remove the two "Output schema" sections and the "Consistency checks". The structured-output schema now enforces the field shapes, so most of those checks are redundant. Keep only the rules about meaning, such as "never invent companies." I'd expect the result to be about 10–12 KB, roughly a quarter of what the two prompts send now.

What you gain and the risk

- Faster: one call instead of two. The system prompt is also always identical, so Ollama can reuse its cached prefix on later turns, and only the history and question need processing.
- More accurate: routing sees the full history, the two clarification rules no longer conflict, and metric names are limited to valid values.
- Risk: a 4B model has to do more in one call. Before switching, run 20–30 real questions through both versions and compare classification, entities and refined_question. If routing gets worse, a fallback is to merge only the prompts and keep the schema split. But with temperature 0 and a schema-enforced output, I'd expect the merged version to hold up.

I can write the merged prompt and route() if you want.