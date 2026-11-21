# Why a one-row change can need another review

A reviewer sees a proposed checkout rollout from 25% to 50% in staging. Before they approve it, someone else sets the rollout to 10%. The proposed SQL still says `SET ROLLOUT_PCT = 50`, but the reviewer has never seen a change from 10% to 50%. If the statement also checks that the starting value is 25%, it now touches zero rows. The time of the preview matters in both cases.

## What a preview captures

The Developer app records a snapshot of the managed table, makes a zero-copy clone and runs the proposed statement against that clone. If the statement reads an allowed lookup, Snowflake reads that lookup at the same instant. For example, a price update that selects active regions uses the region membership that existed when the prices were cloned.

A stream records the clone's changes. The app saves those rows so a reviewer can compare values side by side, even after leaving the page. A count of one update could describe a change to the staging flag or a change to the production flag. The saved business keys and before/after values tell the reviewer which one occurred.

## When the evidence changes

The workflow calls the net row changes since the last preview **drift**. It tracks changes to the target and to each lookup the statement actually reads. **Drift since dry run** also reports how many of the changed target keys overlap the request. Any detected drift blocks approval, including changes to other keys. A lookup change can affect which rows a statement selects even when the target has no changes of its own.

Table definitions matter too. A different default can change what an `INSERT` writes. Approval and execution check the target's columns against the reviewed clone and check the last definition change on the target and referenced lookups. Even a table comment can advance that timestamp. Use tables in a local primary database with change tracking and enough Time Travel retention to cover the review period. [Snowflake table metadata](https://docs.snowflake.com/en/sql-reference/info-schema/tables#usage-notes)

**Re-validate on current data** repeats the submitted SQL on a fresh clone. It compares both the operation counts and a fingerprint of the changed row values with the developer's run. The reviewer can inspect **Developer dry run** and **Admin re-validation (current data)**. If the effect differs, the Admin app requires the reviewer to display the new change set before approving it. In the checkout example, a zero-row result tells the reviewer the expected starting value has disappeared. They can reject the request and ask the developer to propose the change they now intend.

An approved request can return to review with a reason. The workflow also returns it to `SUBMITTED` if execution detects drift or can no longer verify the reviewed snapshot. When a deployment changes how statements interpret dates or other session settings, an older pending request needs a fresh revalidation. If its original SQL can no longer be verified against the submitted statement, the developer opens a new request.

## What the approval authorizes

The apps identify the signed-in viewer and open a separate owner-rights Snowflake connection per viewer. The procedures trust the identity the app supplies. End users receive the app roles. Direct procedure access and the `AC_OWNER` role belong to trusted operators. Changing account roles within one user's session still leaves the same requester on the audit record.

Approval records the exact SQL hash, the reviewer's comment and the allowed deviation in row counts. Execution checks the hash and the reviewed data, runs the statement on the source, checks the resulting counts and checks again for drift before committing. The source change, final request status and success event commit together. A count outside tolerance rolls back the source transaction. The recorded execution attempt can still have a Snowflake query ID.

The staging configuration value becomes visible to new reads when that transaction commits. You can trace the decision in **History and audit** after the temporary sandbox is cleaned up. [Review a request after drift](../how-to/review-after-drift.md) · [Change request reference](../reference/workflow.md)
