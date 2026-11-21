# Report a security vulnerability

If the repository offers **Report a vulnerability** in the **Security and quality** tab, use that form to reach the maintainers privately. If the button is unavailable, open an issue titled **Request for a private security contact**. Share the details once a private channel is available. [GitHub’s private reporting guide](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)

In the private report, identify the affected revision, app or procedure, the role involved, what you expected, and what you observed. Use synthetic rows and role names. Leave credentials, real account and user identifiers, live query IDs and customer records out of attachments.

## What the workflow protects

The owner-rights procedures trust the signed-in viewer identity supplied by the Developer and Admin apps. Direct procedure access and the `AC_OWNER` role belong to trusted operators. A developer can preview a change on a clone. A different user reviews it, and approval and writing to the source are separate actions. The source change and its successful audit event must commit together. [How approval handles drift](docs/explanation/approval-and-drift.md)

For example, report a preview that writes to the source, a requester who can approve their own change, or a completed source change with no corresponding audit event. For an ordinary usability problem, use the [contribution guide](CONTRIBUTING.md).
