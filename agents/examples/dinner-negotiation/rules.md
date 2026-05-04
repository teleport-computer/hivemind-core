# Dinner-time room — rules

Alice and Bob are negotiating one specific dinner time and a venue.
Neither party wants to disclose any information beyond what is
strictly required to confirm the agreement.

## Allowed in the released output

- Exactly one date and time, within the requested window.
- Exactly one venue suggestion (name + neighborhood is fine).
- A short justification (one sentence) of why this slot was chosen.

## Not allowed in the released output

- Any times that are NOT the agreed slot, even framed as "available
  but not chosen" or "free on Wednesday".
- Any indication of what other commitments either party has
  (meetings, appointments, blocked times, busy/free flags beyond the
  one chosen slot).
- Any individual identifiers — names of attendees, phone numbers,
  email addresses, location-history entries.
- Any verbatim rows from the calendar table or verbatim bytes from
  any bundled file the query agent reads.
- Any SQL query the agent ran, any tool-call traces, any reasoning
  about ruled-out alternatives.

## Scope agent guidance

When constructing `scope_fn`:

- Only rows in the `calendar` table where `is_busy = false` AND
  `start_time` falls in the requested window are visible.
- Do not pass `notes`, `attendees`, or `location` columns through —
  these can leak schedule context. Only `start_time` and `end_time`.
- Reject SQL that tries to enumerate the full calendar, count
  appointments, or aggregate beyond what is needed to confirm
  availability.

## Mediator agent guidance

When deciding what to release:

- If the raw output names more than one time, redact all but the
  agreed one.
- If the raw output mentions any commitment, location, attendee, or
  preference that is not the chosen venue + chosen time, strip it.
- If the raw output appears to leak verbatim data from Alice's
  calendar or from Bob's bundled files, refuse to release and emit
  a short error: `"refused: candidate output appears to leak source
  data"`.
