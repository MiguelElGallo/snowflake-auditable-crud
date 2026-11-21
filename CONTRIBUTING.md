# Contribute to Snow Auditable CRUD

You can help by reporting a bug, improving a guide or changing how the Snowflake workflow works.

## Report a bug

In the repository’s **Issues** tab, look for an existing report. If you open a new issue, include the app page or command, the smallest steps that reproduce the problem, and what you expected and observed. The configuration table tutorial shows how to describe a change to one row. [Configuration table tutorial](docs/tutorials/first-change.md)

For a suspected vulnerability, use the project’s private reporting channel. [Private reporting instructions](SECURITY.md)

Use synthetic rows, SQL and usernames. In public issues, pull requests and screenshots, omit credentials, account identifiers, query IDs and customer data. Keep your deployment’s `manifest.yml`, connection settings and `.streamlit/secrets.toml` local.

## Propose a change

1. Fork the repository on GitHub and clone your fork. Start a branch from the current upstream `main`.
2. Make one focused change. For a workflow fix, include a regression test that fails on the old behavior and checks the result users care about, such as which row changed or which action was blocked.
3. For documentation, put a first-time lesson in `docs/tutorials`, a task in `docs/how-to`, a lookup in `docs/reference` or a design discussion in `docs/explanation`. Try each example with synthetic data and check that the page labels and expected results match the app.
4. From the repository root, run the same checks as CI. You need Python 3.11, `uv` and `make`. The tests run without a Snowflake account.

   ```sh
   make check
   ```

   Changes to bundled procedure code or dependencies may also require regenerating checked-in files. The [verification guide](docs/how-to/verify.md) gives the commands and explains how to run the optional isolated Snowflake tests.

5. Before committing, check the author name and email that will appear in Git history. To keep your personal email private, configure your GitHub-provided `noreply` address for this repository. Your GitHub username still appears on the pull request. [Set your commit email address](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address)
6. Commit the change, push the branch to your fork and open a pull request targeting the upstream `main`. Describe what changed, why, and which checks you ran. If GitHub shows **Review required**, a maintainer can arrange a review by someone other than the author who has write access. For changes to approvals or source writes, ask the reviewer to check the preview, the decision and the final effect on the source.

The project uses the [MIT license](LICENSE). Keep the copyright and permission notices in copies or substantial portions of the software.
