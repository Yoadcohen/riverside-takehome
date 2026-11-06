"""AI step: read each handoff note and pull out facts, with the exact sentence each fact came from.

This is the only place AI is used. The AI never decides handoff_status. It returns
facts; handoff.py keeps only facts quoted from the note, then rules compare them
to the other files.

Run once (needs an Anthropic API key):
    export ANTHROPIC_API_KEY=...
    python3 extract_notes.py

Writes ai_cache.json. handoff.py reads that file, so the assessment runs the
same way every time and works without a key. Uses only the Python standard library.
"""

import csv
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from handoff import AI_CACHE, DATA_DIR, validated_ai_facts

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
PROMPT_VERSION = "2"

SYSTEM_PROMPT = """You read one sales handoff note for a K-12 school district deal.
Report only what the note says. Do not guess or add anything.
For every fact, copy the exact words from the note into the matching evidence field.
If the note does not state something, use null, false, or an empty list.

Reply with only this JSON object and nothing else:
{
  "approved_discount_pct": number or null,      // a discount percent the note says was approved
  "approved_discount_evidence": string or null,
  "products": [{"name": string, "evidence": string}],  // products or SKUs bought; name only, no quantities; not services events such as kickoff dates
  "billing_hint": string or null,               // who the note says the invoice should go to
  "billing_hint_evidence": string or null,
  "asks_to_start_fulfillment": true or false,   // the note asks to start fulfillment, order creation, or fulfillment planning
  "asks_to_start_fulfillment_evidence": string or null
}"""


def read_note(note_text, api_key):
    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"<note>{note_text}</note>"}],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Anthropic API error {error.code}: {error.read().decode(errors='replace')}")
    text = "".join(block.get("text", "") for block in reply["content"] if block["type"] == "text")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY first.")
    with open(DATA_DIR / "handoff_notes.csv", newline="", encoding="utf-8") as f:
        notes = list(csv.DictReader(f))

    results = {}
    for row in notes:
        opp_id, note_text = row["opportunity_id"], row["note_text"].strip()
        facts = read_note(note_text, api_key)
        if facts is None:
            print(f"{opp_id}: AI reply was not valid JSON; skipped (handoff.py will mark it R4).")
            continue
        _, dropped = validated_ai_facts(facts, note_text)
        results[opp_id] = {"note_text": note_text, "facts": facts}
        print(f"{opp_id}: {json.dumps(facts)}" + (f"  DROPPED (not quoted from note): {dropped}" if dropped else ""))

    Path(AI_CACHE).write_text(json.dumps(
        {"model": MODEL, "prompt_version": PROMPT_VERSION, "results": results}, indent=2), encoding="utf-8")
    print(f"\nWrote {AI_CACHE.name}. Now run: python3 handoff.py")


if __name__ == "__main__":
    main()
