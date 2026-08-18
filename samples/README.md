# Demo requisitions

Five requisitions, all in the column format a plant engineer actually types or an
SAP export actually produces: `Sl.No.`, `Material Code`, a 40-character
`Item Description`, `Plant Code`, `Material Group`, `Storage Location`,
`Quantity`, `EOM`, `Delivery Date` and a 200-character `Long Text`, preceded by a
`Key: value` header block.

Every line in every file was validated against this cluster's material master
before the file was committed, so none of them fails for an accidental reason.
They parse with **no warnings** at confidence `0.9`.

| File | PR | What it shows |
| --- | --- | --- |
| `happy_path_A_rehearsal_PR-2026-0854.csv` | PR-2026-0854 | Seven lines, all resolving. Rehearse with this one. |
| `happy_path_B_recording_PR-2026-0852.csv` | PR-2026-0852 | Eight lines, all resolving, most with several suppliers in the purchase history. Record with this one. |
| `guardrail_path_A_rehearsal_PR-2026-0861.csv` | PR-2026-0861 | Six lines refused for six different reasons. Rehearse with this one. |
| `guardrail_path_B_recording_PR-2026-0863.csv` | PR-2026-0863 | The same six reasons, different materials. Record with this one. |
| `award_gate_seed_PR-2026-0851.csv` | PR-2026-0851 | Used to pre-drive a case to the award gate. Already consumed. |

**There are two of each on purpose.** Requisition intake is idempotent by content
hash: re-uploading a file you have already uploaded returns the existing case and
starts no new workflow. Rehearsing with the file you intend to record would spend
it. See [../docs/demo-script.md](../docs/demo-script.md).

## What the guardrail files exercise

Each line fails for a different real master-data reason, which is the point — the
agent stops the line it cannot source rather than guessing, and does not fail the
rest of the requisition either:

| Reason | 0861 | 0863 |
| --- | --- | --- |
| On engineering hold | `RAW-00051` | `HYD-00016` |
| Blocked for procurement | `TOL-00044` | `ELC-00017` |
| Made in-house, not bought | `INS-00041` | `VAL-00017` |
| Obsolete, successor recorded | `HYD-00049` → `HYD-00050` | `VAL-00019` → `VAL-00020` |
| Not extended to plant 1000 | `BRG-00001` (at 1100, 2100) | `BRG-00003` (at 1100) |
| No material master match | free-text pipe | free-text elbow |
| Material group disagrees with the master *(warning, not blocking)* | `TOL-00049` | `TOL-00052` |
| Resolves cleanly, for contrast | `SEL-00066` | `SEL-00040` |

## Older samples

`purchase_requisition_PR-2026-084{2,3,4}.csv` predate the set above and use a
different column layout. They still parse — the point of the alias table — but the
files above are the ones the walkthrough uses.

## Re-checking them yourself

Against a configured `DATABASE_URL`, this prints each line's resolution without
writing anything:

```bash
procureguard pipeline PG-PR-2026-0852        # after uploading
```

Or upload and read the case back:

```bash
curl -X POST http://localhost:8000/api/v1/cases/upload \
  -H 'X-Actor-Id: sam.senior' -H 'X-Actor-Roles: SENIOR_BUYER' \
  -F 'file=@samples/happy_path_B_recording_PR-2026-0852.csv' \
  -F 'plant_code=1000' -F 'start_workflow=true'
```

Note the identity: `PROCUREMENT_HEAD` has no `CASE_CREATE`, so Jordan cannot open
a case. `SENIOR_BUYER` is the role that can both open a case and release e-mail.
