# Run the project checks

## Check a change offline

Use Python 3.11 and `uv`. Run from the project root.

```sh
make check
```

The command checks the exported container dependencies against `uv.lock`, syncs the environment, runs Ruff lint and formatting checks, runs strict mypy, and exercises both apps and the workflow in pytest. This is also the pull request CI entry point. [CI workflow](../../.github/workflows/check.yml)

After changing code bundled into the Snowflake procedures, regenerate their checked-in definitions and rerun `make check`.

```sh
uv run --frozen python scripts/build_procedure.py
```

After changing dependencies, run `uv lock` and `make container-requirements`. Review the lockfile and `streamlit/common/requirements.txt` together. Both app packages include that requirements file.

## Verify the handlers against Snowflake

First bootstrap the development database and `AC_OWNER`. Set `AC_SNOWFLAKE_CONNECTION` to a named connection in `connections.toml` that the Snowflake Python connector can read. Configure a usable warehouse and grant the connection’s user `AC_OWNER`. A profile stored only in the CLI’s `config.toml` is insufficient for these Python tests. [Python connector named connections](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect#connecting-using-the-connectionstoml-file)

Isolated mode creates a fresh transient schema, installs the candidate handlers and drops the schema at the end.

```sh
AC_ISOLATED_TESTS=1 AC_SNOWFLAKE_CONNECTION=my_dev_connection uv run --frozen pytest -m snowflake
```

The live assertions cover actual target rows, review after a lookup or column definition changes, successful commits, failures, cleanup and mixed `MERGE` results. The service connection supplies two test actor labels. To check sign-in, open the hosted Developer and Admin apps as two distinct Snowflake users and complete the [first change](../tutorials/first-change.md).

To test **already-deployed procedures**, omit `AC_ISOLATED_TESTS`. That mode writes uniquely named pricing and lookup rows in the deployed demo tables, then removes the fixtures. It retains test request IDs in the audit log. Open requests reading those tables can see the activity as drift.

## Run the apps locally

Deploy the schema and procedures first, then create an ignored `.streamlit/secrets.toml` in the repository root. Use a named Python connector connection from `connections.toml` that can use the owner role.

```toml
[connections.snowflake]
connection_name = "my_dev_connection"
role = "AC_OWNER"
database = "AUDITABLE_CRUD"
warehouse = "COMPUTE_WH"
```

Start the apps in separate terminals.

```sh
AC_LOCAL_DEV=1 uv run --frozen streamlit run streamlit/developer_app/streamlit_app.py --server.port 8501
```

```sh
AC_LOCAL_DEV=1 uv run --frozen streamlit run streamlit/admin_app/streamlit_app.py --server.port 8502
```

For local development, `AC_LOCAL_DEV=1` lets the app identify the connected Snowflake user. Both apps show the same actor if they use the same connection. In Snowsight, each viewer signs in as their own user and has a separate connection. [Workflow reference](../reference/workflow.md)
