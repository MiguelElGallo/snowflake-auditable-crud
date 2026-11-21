# Snow Auditable CRUD

You keep your application settings in a Snowflake configuration table. Before updating a setting, you want to test the change on a copy of the table (a *clone*) and have someone else approve it.

In this demo, a fictional store is testing a new checkout page. One setting controls how many testers see it. In the **Developer app**, you change that setting on the clone from one in four testers (25%) to one in two (50%).

In the **Admin app**, a reviewer compares the old and new values and approves the change. When the reviewer applies it, the original table gets the new value. The audit log shows who requested the change, who approved it, and which SQL statement ran. [Follow the tutorial](docs/tutorials/first-change.md) · [Watch the demo](docs/media/snowflake-feature-flag.mp4) · [English captions](docs/media/snowflake-feature-flag.en_US.srt)

## Find what you need

| I want to… | Start here |
| --- | --- |
| Install the two apps in a development account | [Deploy to Snowflake](docs/how-to/deploy.md) |
| Make another table available or allow reads from a lookup | [Onboard a table](docs/how-to/onboard-a-table.md) |
| Decide what to do when data changed after the preview | [Review a request after drift](docs/how-to/review-after-drift.md) |
| Find the query behind a change or retire old sandboxes | [Inspect a run and manage retention](docs/how-to/operate.md) |
| Look up supported SQL, request states and reviewer actions | [Workflow reference](docs/reference/workflow.md) |
| Look up account roles, compute, defaults or managed-table settings | [Configuration reference](docs/reference/configuration.md) |
| Understand why a reviewed change can require another preview | [How approval handles drift](docs/explanation/approval-and-drift.md) |
| Understand where the apps run and how packages reach them | [Container runtime and dependencies](docs/container-runtime-assessment.md) |
| Report a bug or propose a change | [Contribute to the project](CONTRIBUTING.md) |
| Report a suspected vulnerability privately | [Security reporting](SECURITY.md) |

## Develop and verify

With Python 3.11, `uv` and `make` installed, run the offline checks from the repository root.

```sh
make check
```

This checks the dependency export, lints and type-checks the code, and runs the offline test suite. For local Streamlit development and isolated Snowflake tests, see [Run the project checks](docs/how-to/verify.md). For examples of bugs those tests catch, see [What the regression tests verify](docs/peer-review.md).

The project’s original code, documentation, demo video, captions and screenshot are available under the MIT license. Third-party dependencies retain their own licenses. [MIT license](LICENSE)
