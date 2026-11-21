# Deploy the two apps to Snowflake

Use this guide to install the Developer and Admin apps in a development account or update an existing installation. The commands use the supplied object names, a POSIX shell and a named Snowflake CLI connection. Run them from the project root. The deployment uses Snowflake Database Change Management (DCM) to compare the definitions in this repository with the account.

## Prepare your account settings

Install a Snowflake CLI release with `snow dcm` support. The documented workflow was checked with 3.34.0. Set `AC_SNOWFLAKE_CONNECTION` to a connection you have configured locally. The example uses `my_dev_connection`.

```sh
export AC_SNOWFLAKE_CONNECTION=my_dev_connection
snow connection test -c "$AC_SNOWFLAKE_CONNECTION"
```

**Upgrading a checkout that tracked `manifest.yml`?** Back up that file outside the checkout **before** pulling the new version, then restore it to the project root. It contains the full settings for your deployment, including role assignments. If you already pulled the update, recover the file from your last working revision or private configuration backup. The following commands recover the last committed manifest when the old history is available. They cannot recover edits that existed only in your working copy. Use your private backup for those. `cp -n` preserves an existing local file.

```sh
previous=$(git log --no-renames -1 --diff-filter=AM --format=%H -- manifest.yml)
backup_dir=$(mktemp -d)
test -n "$previous" &&
    git show "${previous}:manifest.yml" > "$backup_dir/manifest.yml" &&
    cp -n "$backup_dir/manifest.yml" ./manifest.yml
```

For a **new installation**, create the local manifest from the template.

```sh
cp -n manifest.example.yml manifest.yml
```

Set `targets.DEV.account_identifier` to your organization and account identifier. In `templating.configurations.DEV`, replace both sample role names with the existing account roles for your authors and reviewers. Verify the warehouse and compute pool names. The local `manifest.yml` is ignored by Git. Keep your private copy with your deployment configuration. On later upgrades, compare the new template with that copy. Carry over updated asset paths or defaults while preserving your managed tables and role assignments. [Configuration reference](../reference/configuration.md)

## Bootstrap the owner and project

An account administrator runs the bootstrap once. Read the script first. It creates the owner role and grants it to the user running the script, creates the database and DCM project, and grants warehouse usage and the task and Cortex privileges. If a deployment service will run later releases, the administrator must grant `AC_OWNER` to that service through the account’s normal role hierarchy. The script stops if an existing `AUDITABLE_CRUD` database belongs to another role. [Bootstrap SQL](../../bootstrap/00_admin_bootstrap.sql)

The supplied script expects an existing `COMPUTE_WH` warehouse. To use another warehouse, set its name in a private copy of the script and in `templating.defaults.warehouse`. Use the private path in the command below.

```sh
snow sql -c "$AC_SNOWFLAKE_CONNECTION" -f bootstrap/00_admin_bootstrap.sql
```

Ensure `AC_OWNER` has `USAGE` on the selected compute pool and access to `SNOWFLAKE.SNOWPARK.PYPI_SHARED_REPOSITORY` through the `SNOWFLAKE.PYPI_REPOSITORY_USER` database role. Those privileges can be inherited. [Snowflake package access](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/dependency-management)

## Review the plan and apply it

```sh
project=AUDITABLE_CRUD.DCM.AUDITABLE_CRUD_PROJECT
snow dcm raw-analyze "$project" -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER --target DEV
snow dcm plan "$project" -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER --target DEV --save-output
```

Inspect `out/plan_result.json`. On a new deployment, expect the managed schemas, tables, views, procedures, task, apps and grants. On an upgrade, check every change to existing tables and grants. An omitted role assignment can remove access. Resolve unexpected changes in the local manifest and rerun the plan. Apply the reviewed plan from the same files. Choose a new alias for each deployment.

```sh
snow dcm deploy "$project" -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER --target DEV --alias initial-dev
```

After first creation or a migration to the container runtime, attach the shared package repository to both apps. To use the sample tables, also run the seed script. It inserts rows only into empty demo tables.

```sh
snow sql -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER --database AUDITABLE_CRUD -f bootstrap/30_attach_streamlit_repository.sql
snow sql -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER -f bootstrap/10_seed_demo.sql
```

## Open the apps

```sh
snow streamlit get-url AUDITABLE_CRUD.APPS.DEVELOPER_APP -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER
snow streamlit get-url AUDITABLE_CRUD.APPS.ADMIN_APP -c "$AC_SNOWFLAKE_CONNECTION" --role AC_OWNER
```

Open the first link as a developer and the second as a reviewer. **Choose table** lists the managed tables. **Review queue** shows requests ready for review. Each page shows the signed-in viewer. Open **Runtime details** to see the installed Streamlit and Pillow versions. If packages fail to load, check the `artifact_repositories` column from the attachment script and the owner’s repository access. [Test and approve a configuration change](../tutorials/first-change.md)
