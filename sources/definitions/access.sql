-- Access model:
--   AC_DEVELOPER -> may open the developer app.
--   AC_ADMIN     -> may open the admin app and read the reporting views.
-- Only the owner can call the workflow procedures or write the CORE tables.
-- The procedures enforce the requester/reviewer separation.

DEFINE DATABASE ROLE {{ app_db }}.AC_DEVELOPER
    COMMENT = 'Propose DML change requests through the developer app';

DEFINE DATABASE ROLE {{ app_db }}.AC_ADMIN
    COMMENT = 'Review, approve and execute DML change requests through the admin app';

{% for role in ['AC_DEVELOPER', 'AC_ADMIN'] %}
GRANT USAGE ON DATABASE {{ app_db }} TO DATABASE ROLE {{ app_db }}.{{ role }};
GRANT USAGE ON SCHEMA {{ app_db }}.APPS TO DATABASE ROLE {{ app_db }}.{{ role }};
{% endfor %}

GRANT USAGE ON STREAMLIT {{ app_db }}.APPS.DEVELOPER_APP TO DATABASE ROLE {{ app_db }}.AC_DEVELOPER;
GRANT USAGE ON STREAMLIT {{ app_db }}.APPS.ADMIN_APP TO DATABASE ROLE {{ app_db }}.AC_ADMIN;

GRANT USAGE ON SCHEMA {{ app_db }}.CORE TO DATABASE ROLE {{ app_db }}.AC_ADMIN;
GRANT SELECT ON VIEW {{ app_db }}.CORE.V_REQUESTS TO DATABASE ROLE {{ app_db }}.AC_ADMIN;
GRANT SELECT ON VIEW {{ app_db }}.CORE.V_AUDIT TO DATABASE ROLE {{ app_db }}.AC_ADMIN;

{% for role in developer_account_roles %}
GRANT DATABASE ROLE {{ app_db }}.AC_DEVELOPER TO ROLE {{ role }};
{% endfor %}
{% for role in admin_account_roles %}
GRANT DATABASE ROLE {{ app_db }}.AC_ADMIN TO ROLE {{ role }};
{% endfor %}
