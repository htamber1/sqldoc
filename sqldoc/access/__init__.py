"""The `sqldoc access` command suite: automate the SQL Server access-request
workflow DBAs handle daily — check current access (cross-referencing Active
Directory / Entra ID group membership with SQL Server logins + role
memberships), parse plain-English access requests, generate best-practice grant +
rollback scripts, process Jira tickets end-to-end, run access reviews, drive an
email approval workflow, and recommend least-privilege roles.

Active Directory is optional (`pip install sqldoc[activedirectory]`); the LDAP
(ldap3) and Microsoft Graph back-ends are auto-detected from the domain config.
"""
