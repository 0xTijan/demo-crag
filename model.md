# Can the conclusion model be smaller than 4B?

It can be smaller, but only if the conclusion model no longer has to do any thinking with the numbers. Right now it does, and that's where a 1B model breaks.

## What the conclusion step asks of the model today

1. **Read raw rows.** The result JSON has 10+ columns per row, including `is_comparative`, `period_type` and mixed periods.
2. **Pick the right rows.** For example, FY rather than 1H, and own-period values rather than comparatives.
3. **Do arithmetic:** growth rates, differences, "did it increase?".
4. **Answer in the user's language,** often Slovenian.
5. **Follow the rules:** don't mention SQL, don't invent facts.

A 4B model already gets steps 2 and 3 wrong sometimes. A 1B model will get them wrong often, typically by quoting a 2023 figure as 2024, computing a percentage wrong, or treating "1.234.567" as about a thousand. In a financial answer those are the worst mistakes to make. Language is a second risk: small models are much weaker in Slovenian and Croatian. As far as I know, `gemma3:1b` is mainly English, unlike `gemma3:4b`. For a 1–2B model, the Qwen 2.5/3 small models are the multilingual candidates.

## How to make a 1B model viable

Move steps 1–3 into code, so the model only has to phrase the answer:

```
SQL rows ──► Python: pick relevant rows, format numbers (1.234,5 mio EUR),
             compute differences / growth % / which is higher
         ──► facts list:
             - Petrolina d.d., net income FY2024: 12,4 mio EUR (report 17)
             - Petrolina d.d., net income FY2023: 10,1 mio EUR (report 9)
             - change: +2,3 mio EUR (+22,8 %) → increased
         ──► small model: "answer the question in <language> using only these facts"
```

With that input, a 1B model mostly rewords facts that are already correct, which small models handle well. Two more changes:
- **Drop `format="json"`** in `rag/conclusion.py`. The prompt asks for prose but the call forces JSON, which makes the job harder for any model. Stream plain text instead.
- **Skip the model for single-value lookups.** "What was X's revenue in 2023?" can be answered from a template like `"{company} je v letu {year} ustvarila {value} prihodkov."`, with no LLM at all.

## Is it worth it?

Not much on its own. The conclusion is a short answer from a small input, so going from 4B to 1B saves maybe 1–3 seconds on a CPU. The changes above (doing the arithmetic in code, using templates for simple lookups, plain-text streaming) improve accuracy whichever model you use, and the template path saves the whole call.

**Recommendation:** make those changes first. Then put `qwen2.5:1.5b` (or another small multilingual model) into `conclusionGeneratorModel` in `config.json` and compare it with 4B on 20–30 real question/result pairs. A simple automatic check: every number in the answer must appear in the facts list. If the small model passes that and its Slovenian reads well, switch to it. If not, stay on 4B.
