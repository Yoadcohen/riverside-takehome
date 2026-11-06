# Quote-to-order handoff assessment

Checks every opportunity before it is handed from Sales to Deal Operations and writes a
`handoff_status` (`Ready`, `Needs Review`, `Blocked`, `Not Yet Eligible`) with the reasons,
the next action, and who owns it.

## Deliverables

- `docs/diagnosis.pdf`: the one-page root-cause diagnosis
- `handoff.py` and `extract_notes.py`: the working prototype
- `output/queue.html`: the review queue for Deal Operations (the user-facing artifact)
- `output/handoff_assessment.csv`: one row per opportunity with every output field
- `docs/checks.md`: every check in plain English

## Run it

Needs Python 3.9 or later. Nothing to install.

```
python3 handoff.py                      # writes output/handoff_assessment.csv and output/queue.html
python3 -m unittest discover tests      # checks the output against tests/expected_outcomes.csv
```

To refresh the AI step (only needed if a note changes):

```
export ANTHROPIC_API_KEY=...
python3 extract_notes.py                # writes ai_cache.json
```

## How it works

1. `handoff.py` loads the five CSV files into SQLite and runs the rule checks in SQL.
2. The AI step reads only `note_text`. It returns facts such as an approved discount or a product
   name, each with the exact sentence it came from. A fact is thrown away unless that sentence is really
   in the note and contains the fact itself (the number, product or billing name).
3. Rules compare those facts to the other files, then set `handoff_status`.

Every check is listed in `docs/checks.md`.

## Where AI is and is not used

| Task | Done by |
|---|---|
| Stage, approval, quote date, billing entity, start date, order count | Rules |
| District name matching (for example, "Westfield SD") | Rules |
| Contact age, job title under a school, contact named like a rep | Rules |
| Reading free-text handoff notes | AI (Claude), every fact quoted |
| Setting `handoff_status` | Rules only |

An AI finding can only send an opportunity to `Needs Review`. It never sets `Blocked` or `Ready`.
If the AI result is missing or the note changed since the AI read it, the opportunity goes to
`Needs Review` (check R4).

## Assumptions

- Today is treated as 2026-02-10, the latest date an event happened in the data (signal SIG-004).
- "Soon" means within 14 days. To validate with Deal Operations.
- An expired quote blocks the order even if the customer signed before it expired. To validate with Deal Operations.
- Opportunities live in a CRM and orders in a separate system.
- A contact whose information is old is a warning, not a reason to stop an order.

## AI tools used

- Claude (Anthropic API) reads the handoff notes in `extract_notes.py`.
- The code was written with Claude Code and reviewed by me.

## Limitations

- `Ready` means ready to hand to Deal Operations, not ready to fulfill. A Pending PO order (OPP-001) is still `Ready`.
- The data has no quantities, so "three assessment licenses" in a note can't be checked against the order.
- The tool flags problems but doesn't track whether someone fixed them. In production, each exception would be assigned, its outcome recorded, and the check rerun before handoff.
- Six opportunities is too few to measure accuracy; the tests prove the rules, not the AI at scale.
- Name matching only handles "SD" for "School District". Names like "Pine Ridge USD" would not match yet.
- `tests/fixtures/ai_cache_sample.json` is a hand-written sample used to test the rules. It is not AI output.
- Synthetic data only. No connection to any real system.
