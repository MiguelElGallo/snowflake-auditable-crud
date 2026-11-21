# Review a request after the data changes

Use this when the Admin app reports drift, an expired snapshot, or a request returned to review. You need Admin app access under a different Snowflake user from the requester. Revalidation runs the submitted SQL against a new clone. You can inspect its result before deciding whether to approve.

1. Open **Review queue** and select the request. On **Review request**, choose **Drift since dry run**. Read the changes to the target and any referenced lookup tables. If the original snapshot expired, move on to a fresh preview.
2. Check the request status. For an **Approved** request, enter a reason in **Reason to return to review** and select **Return to review**. A **Submitted** request is already ready for revalidation.
3. Select **Re-validate on current data**. Open **Change set** and select **Admin re-validation (current data)**. Compare the keys, before/after values and per-operation counts with the requested change. Use **Developer dry run** to revisit the original preview.
4. If the refreshed rows match the business intent, display **Admin re-validation (current data)** again after comparing. When the results differ, the app requires that view before approval. Enter a comment explaining what you checked and select **Approve**. If they do not, enter the reason, select **Reject**, and confirm **Yes, reject**. The developer can open a new request with revised SQL.

After approval, the source still holds its current values. When the reviewed change is ready to go live, select **Execute on** the named table and confirm **Yes, execute now**. Check the final status, affected rows and query ID in **History and audit**. If execution returns the request to review again, repeat the check against the new data. [Trace an execution](operate.md#trace-a-request-to-its-execution) · [Why drift matters](../explanation/approval-and-drift.md)
