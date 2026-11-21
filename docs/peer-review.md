# What the regression tests verify

A reviewer needs to know more than how many rows a statement affected. A preview can report one update and still reverse the old and new values, round an identifier or select a different row at execution. These are the checks that protect the change record.

## Preview and approval

| Case | Observable result |
| --- | --- |
| A mixed `MERGE` changes three rows | The preview and final table agree on the inserted key, updated key with its old and new values, and deleted key. [Live integration tests](../tests/test_integration_snowflake.py) |
| A nullable identifier exceeds the precision of a floating-point value | The row preview, source sample and CSV preserve the exact digits. The tests include a 38-digit number. [Developer app tests](../tests/test_developer_app.py) |
| A source default or lookup definition changes after the preview | An earlier approval returns to review before the source changes. A new preview uses the updated definition. [Live integration tests](../tests/test_integration_snowflake.py) |
| A reviewer leaves the page open while another action changes the request | The old revision cannot authorize execution. A failed audit write rolls back the source change and the completed status. [Workflow engine tests](../tests/test_engine.py) |
| Two sessions use different date or week settings | The guarded statement selects the same key in the clone and on the source. [Live integration tests](../tests/test_integration_snowflake.py) |
| A browser reconnects while a Cortex draft is open | The workbench reads the current request and checks that it is still editable before generating SQL. Each viewer gets a separate owner connection. [Workbench tests](../tests/test_developer_app.py) · [Viewer identity tests](../tests/test_ui_identity.py) |

An earlier review deliberately swapped the before and after records in a temporary checkout. The old tests passed even when a price appeared to move from 11 to 10 instead of 10 to 11. The replacement test asserts the actual values. A second deliberate change rounded `9007199254740993` down by one. The precision regression caught it.

## Operations

The live suite also exercises empty and populated managed-table views and a scheduled cleanup that reports a partial failure. The sandbox cleanup checks target a single request. [Managed-table tests](../tests/test_managed_table_view.py) · [Cleanup task tests](../tests/test_cleanup_task.py)

## Documentation example

The tutorial’s configuration change was tested with the project handlers and sample data in an isolated Snowflake schema. The preview recorded one row changing from 25 to 50 on the clone. After approval, the original table still read 25. Execution changed only the row for the test environment, and a separate reset request restored it to 25. The test also checked the actors, query ID, and values in the development and production rows. [Configuration table tutorial](tutorials/first-change.md) · [Run the project checks](how-to/verify.md)
