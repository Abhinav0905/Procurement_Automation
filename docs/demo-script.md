# Recording the demo

Three minutes, four files, one browser tab. Nothing here is mocked: the host runs
Temporal and a worker, and the database is CockroachDB Cloud.

**Live demo:** <http://ec2-44-201-136-91.compute-1.amazonaws.com>

## Before you press record

Two files exist for each path so you can rehearse without spending the take.
Requisition intake is **idempotent by content hash** — re-uploading the same file
returns the same case and does not start a second workflow — so the rehearsal file
and the recording file must be different files. They are:

| Path | Rehearse with | Record with |
| --- | --- | --- |
| Happy | `samples/happy_path_A_rehearsal_PR-2026-0854.csv` | `samples/happy_path_B_recording_PR-2026-0852.csv` |
| Guardrail | `samples/guardrail_path_A_rehearsal_PR-2026-0861.csv` | `samples/guardrail_path_B_recording_PR-2026-0863.csv` |

Every line in all four was validated against this cluster's material master
before the file was committed, so none of them can fail for an accidental reason.

### Who can do what

No single identity can take a requisition to a purchase order, by design. Two
switches during the take, and they are worth naming out loud rather than hiding:

| Shot | Act as | Because |
| --- | --- | --- |
| 1–4 · upload, memory, RFQ release, guardrails | **Sam — Senior buyer** | the only identity with both `CASE_CREATE` and `RFQ_RELEASE` / `EMAIL_SEND` |
| technical approval, if you show it | **Priya — Engineer** | `TECHNICAL_APPROVE` sits with engineering and quality only |
| 6 · award the order | **Jordan — Procurement head** | `AWARD_APPROVE`, which no buyer holds |

Jordan **cannot upload a requisition** — `PROCUREMENT_HEAD` has no `CASE_CREATE`.
Starting the take as Jordan gets you `403 jordan.head lacks CASE_CREATE` on the
first drag. There is an **Admin (all permissions)** identity if you fumble a
switch mid-take, but the two switches are the better story: separation of duties
is the product, not an obstacle.

Then:

1. Open the demo URL and set **Acting as → Sam — Senior buyer**. This matters —
   see the identity table below. Dana, the default, is a plain `BUYER` and cannot
   release e-mail; if a button looks broken, that is the reason.
2. Check the header badges read `ok`, `native vectors` and `temporal: ok`. If
   Temporal says anything else, stop — the whole point is that it is live.
3. Run the rehearsal upload once, all the way through, then leave that case alone.
4. Close every other tab. Zoom the browser to ~110%: the compliance colour coding
   is what carries the meaning and it has to survive compression.

## The take

Timings are the target, not a script to read out. Cut between shots — do not
speed-ramp. A sped-up agent demo reads as though something was hidden, and
nothing here is slow enough to need it.

### 1 · What this is — 0:00–0:20

Land on **How it works**. You do not need to read it out; let the two columns be
visible while you say the one line:

> Procurement teams re-derive the same decision every year because the memory of
> what they paid, and why, is scattered across purchase orders and shared drives.
> ProcureGuard keeps that memory in CockroachDB and queries it — and it never
> approves anything itself.

Point at the amber stage in the fifteen-stage strip. Four human gates.

### 2 · A requisition arrives — 0:20–0:50

**Cases** → drag `happy_path_B_recording_PR-2026-0852.csv` onto the drop zone.

What to point at, in order:

- 8 lines, parsed as `csv` at confidence `0.9`, **no parser warnings** — the
  sheet uses `Sl.No.`, `EOM`, `Material Group` and a 200-character `Long Text`,
  which is what a real SAP export and a real hand-built sheet look like.
- The green pill: **`Workflow procurement-PG-PR-2026-0852 started`**. Say the
  workflow id out loud. That is the live moment.

Open the case. It has already moved off `RECEIVED` on its own.

### 3 · The memory — 0:50–1:30

This is the most important shot in the video. The hackathon is about agentic
memory; everything else is plumbing around it.

- **Requisition** card: every line resolved `VALID` at confidence `1` against the
  material master — plant extension, blocked status, in-house parts, unit
  convertibility and lot size all checked.
- **Price history / benchmark**: what the company actually paid for this material
  before. Say the number that matters:

  > Twelve rows out of twenty-five thousand purchase-order lines, through a
  > covering index. The model never sees a table — memory that has to fit in a
  > context window is not memory.

- **Shortlist**: suppliers surfaced by capability using native `VECTOR` search
  over the vendor master with a C-SPANN index, not just whoever was used last
  time. Open **Master data → search** for one line if you want the vector hit
  visible on its own.
- **RFQ**: drafted, and **held**. `ALLOW_AUTOMATED_EMAIL_SEND=false`. Show the
  outbox count and release exactly one message as Sam.
- Say the sealed-bid line while the `bids sealed` pill is on screen:

  > Each supplier's commercial payload is encrypted under a per-bid key until a
  > human approves the technical evaluation. The agent physically cannot read a
  > price while judging technical compliance.

### 4 · It refuses to guess — 1:30–2:05

Back to **Cases** → drop `guardrail_path_B_recording_PR-2026-0863.csv`.

Open it and walk the eight lines. Each fails for a different real reason:

| Line | Material | Why it stops |
| --- | --- | --- |
| 10 | `HYD-00016` | on engineering hold |
| 20 | `ELC-00017` | blocked for procurement |
| 30 | `VAL-00017` | made in-house, must not be bought out |
| 40 | `VAL-00019` | obsolete — successor `VAL-00020` should be ordered |
| 50 | `BRG-00003` | not extended to plant 1000; it exists at 1100 |
| 60 | *(free text)* | no master match — five candidates proposed, none applied |
| 70 | `TOL-00052` | resolves, but the requisition's material group disagrees with the master |
| 80 | `SEL-00040` | clean — so it is clearly not failing everything |

The line worth saying:

> It does not guess, and it does not fail the whole requisition either. It tells
> the buyer which line is wrong and what to do about it — including the one line
> where the material resolves but the requester's material group suggests they
> had a different part in mind.

### 5 · Really orchestrated — 2:05–2:30

On either case, open **Durable orchestration** → **Load Temporal history**.

Real run id, task queue `procureguard-procurement`, and the activity events with
their names as Temporal recorded them. Then **Open in Temporal UI ↗** on port
8088 for the same execution in Temporal's own console.

> One durable workflow per case. A case that waits three weeks for a supplier
> reply survives a restart and a redeploy without losing its place — the
> follow-up at 72 hours is a durable timer, not a cron job hoping the process is
> still alive.

### 6 · A human places the order — 2:30–2:50

Open case **`PG-PR-2026-0851`**, already at `WAITING_FOR_AWARD_APPROVAL`. It has
been through RFQ, technical evaluation, unsealing, ranking and one negotiation
round that took **9.76%** off the baseline.

Switch to **Jordan — Procurement head** and approve the award. Say why you are
switching: the senior buyer who ran the RFQ is not allowed to authorise the order.
Give a real reason — it is recorded in the audit trail — then watch the savings
populate and the PO draft appear.

Close on:

> The purchase order is drafted, not placed. ProcureGuard never writes to SAP: it
> emits a draft and an SAP-shaped payload for a human to release.
> `ALLOW_AUTOMATED_PO_CREATION` ships false.

## Two things that will bite you

- **Do not click *Agent decisions* twice on the same case.**
  `POST /cases/{id}/requirements/extract` is not idempotent and returns 500 on a
  second run — a known gap, documented in [hackathon.md](hackathon.md).
- **Do not re-upload a file you already uploaded** expecting a second case. You
  will get the first case back. That is the content-hash idempotency working, but
  it looks like a bug on camera.

## If Temporal is not `ok`

The header badge is the check. If it is not green, the API cannot reach the
orchestrator, uploads will still open cases — the case is authoritative in
CockroachDB either way — but no workflow starts and shot 5 has nothing to show.
Redeploy rather than record around it:

```bash
./infra/ec2/deploy.sh --terminate && ./infra/ec2/deploy.sh
```

Allow about ten minutes: the host builds the image from source on first boot.
