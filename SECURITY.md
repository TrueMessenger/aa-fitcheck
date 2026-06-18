# Security Policy

## Supported versions

Security fixes are applied to the latest released `1.x` version. Please make sure you
are on the most recent release before reporting an issue.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report vulnerabilities privately via GitHub's
[private security advisories](https://github.com/TrueMessenger/aa-fitcheck/security/advisories/new),
or by email to **truemessenger07@gmail.com**.

Include enough detail to reproduce (affected version, configuration, steps). You can
expect an acknowledgement within a few days. Once a fix is available it will be released
and credited in the changelog (unless you prefer to remain anonymous).

## Scope notes

aa-fitcheck reads pilot data from ESI and Alliance Auth and never stores EVE SSO
credentials itself (Auth's `django-esi` owns tokens). Reports about token handling,
permission/visibility bypasses (doctrine/fit visibility, Secure Groups membership), or
data exposure across alliances/corporations are especially welcome.
