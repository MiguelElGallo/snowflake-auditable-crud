# Test and approve a change to a Snowflake configuration table

A fictional store is testing a new checkout page. One setting in its Snowflake configuration table controls what percentage of testers see the page. You'll change that value from 25% to 50% on a copy of the table, called a *clone*. A reviewer will check which row changed, approve the update, and apply it to the original table.

Use a disposable development installation with the sample tables populated. You need the Developer app. Your reviewer needs the Admin app under a different Snowflake user. [Sample data](../../bootstrap/10_seed_demo.sql)

## 1. Open a request

In the Developer app, open **Choose table**. On the **FEATURE_FLAGS** card, select **Propose a change**. The **Change workbench** opens. Copy the request ID so your reviewer can find the same request later.

Expand **Show schema and sample of AUDITABLE_CRUD.DEMO.FEATURE_FLAGS**. Find the row where `FLAG_KEY` is `new_checkout` (the new page) and `ENVIRONMENT` is `staging` (the test environment). The `ROLLOUT_PCT` column holds the percentage of testers who see the page. It starts at `25` in the sample data. If your row already has a different value, use a fresh copy of the demo for this lesson.

## 2. Preview one row

Select **Write SQL** and enter this update. The `WHERE` clause selects the setting in the test environment and checks that its current value is still `25`.

```sql
UPDATE FEATURE_FLAGS
SET ROLLOUT_PCT = 50
WHERE FLAG_KEY = 'new_checkout'
  AND ENVIRONMENT = 'staging'
  AND ROLLOUT_PCT = 25;
```

Select **Dry run on the clone** to test the update on the copy. Under **Dry-run result**, open **Updated (1)** and compare the old and new values.

| Flag | Environment | Changed column | Before | After |
| --- | --- | --- | ---: | ---: |
| `new_checkout` | `staging` | `ROLLOUT_PCT` | 25 | 50 |

The counters should show one update, zero inserts and zero deletes. The source table still has the original value. If the preview differs, select **Discard request** and confirm **Yes, discard**.

## 3. Hand the change to your reviewer

Enter `Show the new checkout page to 50% of testers` in **Note for the reviewer (optional)** and select **Submit for approval**. You can track the request under **My requests**.

Have your reviewer open **Review queue** in the Admin app and select your request ID. On **Review request**, they can read your note and the SQL. Under **Change set**, check that only the percentage for `new_checkout` in the test environment changes. Select **Drift since dry run** to see whether the data has changed since your preview. If it has, select **Re-validate on current data** and inspect **Change set → Admin re-validation (current data)**. Continue only if the result still matches the row above.

Have the reviewer enter `Confirmed that only the test setting changes, from 25% to 50%` in **Comment (required)**, leave the allowed deviation at `0`, and select **Approve**. The request now shows **Approved**. At this point, the value in the original table is still `25`.

## 4. Execute and read the record

In the Admin app, select **Execute on AUDITABLE_CRUD.DEMO.FEATURE_FLAGS**, check the table name and the expected count of one update, and confirm **Yes, execute now**. This is the step that writes `50` to the test setting in the original table.

Open **History and audit**, select the same request and inspect **Audit trail**. Look for the requester on `SUBMITTED`, the reviewer on `APPROVED` and the executor on `EXECUTED`. The **Execution** row in the stage table shows one update and its Snowflake query ID.

In this example from the local demo, the stage table shows one updated row at execution. The audit trail records the requester and reviewer separately. Your request and query IDs will differ.

![Admin History and audit showing one executed update and separate requester and reviewer events](../media/tutorial-audit.png)

To repeat the lesson, open a new request for the same test setting. Use `SET ROLLOUT_PCT = 25` and change the last condition to `AND ROLLOUT_PCT = 50`. Preview, review, and execute the reset. The audit trail records both requests.
