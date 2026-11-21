# Where the apps run

The compute pool runs the Streamlit servers. The query warehouse runs the SQL they issue. Opening the Developer app starts or connects to its server on `SYSTEM_COMPUTE_POOL_CPU`. Opening **Choose table** sends queries to `COMPUTE_WH` to list the managed tables and their row counts. Capacity and billing for the two resources are separate. [Snowflake runtime environments](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments) · [Streamlit billing](https://docs.snowflake.com/en/developer-guide/streamlit/object-management/billing)

## Why use a container?

The project needs control over the Python packages that run its two apps. The container runtime supports a `requirements.txt` built from the project lockfile. Both app packages include the same file at their root. The release tested Streamlit 1.67.0 and Pillow 13.0.0 on Python 3.11. The Snowflake procedures have their own package declarations. [Container requirements](../streamlit/common/requirements.txt) · [App definitions](../sources/definitions/apps.sql)

The project owner attaches Snowflake’s shared PyPI repository to each app after initial creation or migration. The attachment persisted through later DCM updates during the hosted pilot. **Runtime details** shows what the running server actually loaded. [Deploy and verify the apps](how-to/deploy.md)

## What happens when two people connect?

Several viewers can share one server process. Each viewer gets a separate Snowpark connection and keeps their request and editor state in their own Streamlit session. The app uses the viewer’s Snowsight identity for audit attribution. The owner role supplies the SQL privileges. [Approval and drift](explanation/approval-and-drift.md)

In the hosted pilot, both apps reported the pinned package versions in Edge. Separate tabs on the same Developer app reported separate Snowflake sessions. An independently authenticated requester submitted a one-row change and a reviewer approved and executed it through the Admin app. A controlled reconnect test retained the editor input and obtained a fresh Snowflake session. These checks cover different failure modes from the offline app tests. [Run the tests](how-to/verify.md)
