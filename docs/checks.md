# Handoff checks

This file lists every check the script runs. The script is built from this file, not the other way around.

## Settings

| Setting | Value | Why |
|---|---|---|
| `as_of_date` | 2026-02-10 | Latest date an event happened in the data (`account_signals.csv`, `signal_date`, SIG-004). |
| `soon_days` | 14 | A deadline within this many days of `as_of_date` gets a warning. Assumption to validate with Deal Operations. |
| `stale_contact_days` | 365 | A contact whose `last_verified_date` is more than this many days before `as_of_date` gets a warning. |

## Order of `handoff_status` (first match wins)

1. `Not Yet Eligible` if check E1 fails.
2. `Blocked` if any check B1 to B4 fails.
3. `Needs Review` if any check R1 to R3 fails.
4. `Ready` if nothing above failed.

Warnings (W1 to W9) never change `handoff_status`.

## Eligibility check (rule)

| ID | Field | Passes when | If it fails |
|---|---|---|---|
| E1 | `opportunities.stage` | equals `Closed Won` | `Not Yet Eligible` |

## Blocking checks (rules)

| ID | Field | Passes when | If it fails |
|---|---|---|---|
| B1 | `opportunities.approval_status` | equals `Approved` | `Blocked` |
| B2 | `opportunities.quote_expiration` | is on or after `as_of_date` | `Blocked` |
| B3 | `opportunities.billing_entity` | is not blank | `Blocked` |
| B4 | `opportunities.implementation_start_date` | is not blank and is on or after `as_of_date` | `Blocked` |

## Review checks

| ID | Type | How it works | If it fails |
|---|---|---|---|
| R1 | Rule | Count active rows in `orders.csv` with this `opportunity_id` (Cancelled or Void orders don't count). Passes when the count is 0 or 1. | `Needs Review` |
| R2 | AI, then rule | AI reads `note_text` and returns any discount the note says was approved, with the exact sentence. A rule compares that number to `discount_pct`. Passes when there is no number or they are equal. | `Needs Review` |
| R3 | AI, then rule | AI reads `note_text` and returns every product the note mentions, with the exact sentence. A rule checks each product against `orders.product_summary`. Passes when every product appears, or when no order exists yet. | `Needs Review` |

AI rules:
- The AI only reads `note_text`. It never sets `handoff_status`.
- Every fact the AI returns must include the exact sentence it came from, and the fact itself (the number, product or billing name) must appear in that sentence. Otherwise it is thrown away.
- An AI finding can only cause `Needs Review`, never `Blocked` or `Ready`. A person confirms it.
- AI answers are saved to `ai_cache.json`, so the script runs the same way every time and works without an API key.

## Warnings (never change `handoff_status`)

| ID | Type | Field(s) | Warning when |
|---|---|---|---|
| W1 | Rule | `quote_expiration` | Quote has not expired yet and expires within `soon_days` of `as_of_date` (an expired quote is B2, not W1) |
| W2 | Rule | `implementation_start_date`, `orders.csv` | Implementation starts within `soon_days` and no order exists |
| W3 | Rule | `stage`, `close_date` | Stage is not `Closed Won` and `close_date` is before `as_of_date` |
| W4 | Rule | `account_name`, `account_directory.district_name` | No matching district in the directory |
| W5 | Rule | `account_directory.last_verified_date` | Older than `stale_contact_days` |
| W6 | Rule | `account_directory.title`, `school_name` | A district-level title (Superintendent, Chief Academic Officer, any Director) is filed under a school |
| W7 | Rule | `account_directory.contact_name`, `opportunities.owner` | Contact name is the same as a sales rep's name |
| W8 | Rule | `account_signals.*` | A signal matches this district (see matching rule below). Shows `signal_type`, `description`, `source`, `source_quality`, `signal_date`, and whether it came after `close_date`. |
| W9 | AI | `note_text` | Note asks for work the data does not support (for example, "start fulfillment" when stage is not `Closed Won`), or the note gives a billing name when `billing_entity` is blank (suggestion only; a person confirms) |
| W10 | Rule | `orders.order_status` | An order for this deal is Cancelled or Void, so it does not count as an order |

## What `Ready` means

`Ready` means the deal can be handed to Deal Operations. It does not mean the order can be fulfilled: OPP-001 is `Ready` while its order still waits on the school's Purchase Order. Checking fulfillment (PO received, quantities, delivery) is a later step.

## District name matching rule (rule, no AI)

1. Clean both names: lowercase, remove punctuation, change a final `sd` to `school district`.
2. Exact match before cleaning: match confidence `High`.
3. Match only after cleaning: match confidence `Medium`, with the reason written out (for example, "Westfield SD" was read as "Westfield School District").
4. No match: no signal is attached, and W4 applies.

Match confidence is separate from `source_quality`, which says how much to trust where the signal came from.

## Output fields (from the assignment)

| Field | What the script writes |
|---|---|
| `opportunity_id` | From `opportunities.csv` |
| `handoff_status` | `Ready`, `Needs Review`, `Blocked` or `Not Yet Eligible` |
| `exception_types` | IDs of every failed check and warning (for example, `B1; R2; W1`) |
| `validated_facts` | Facts confirmed by rules, each with the file and field it came from |
| `confidence` | `High` if only rules decided `handoff_status`; `Medium` if an AI finding affected it |
| `recommended_next_action` | One plain sentence, written by a rule from the failed checks |
| `owner` | The person or team who must do `recommended_next_action` |
| `review_status` | `Pending review` for `Blocked` and `Needs Review`; `Not needed` otherwise |
