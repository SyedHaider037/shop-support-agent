# Security policy

## Supported version

Only the `main` branch is supported. There are no releases; fixes land on `main`.

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in a public issue:

1. Open the repository on GitHub and go to the **Security** tab.
2. Choose **Report a vulnerability** (GitHub private vulnerability reporting).

Please include:

- what the problem is and which file or endpoint it affects,
- the steps or the request that reproduces it,
- what an attacker could do with it,
- the commit you tested against.

Never include real API keys, tokens or personal data in a report.

## What to expect

This is a portfolio project with a single maintainer. Reports are handled on a best-effort
basis: I aim to reply within a week, and I will say whether I can reproduce the issue and
when a fix is likely. There is no bug bounty.

## Scope notes

The demo uses fake sample data only (sample products and orders with `@example.com`
addresses); it holds no real customer data. The admin endpoints that approve or reject
refunds (`/admin/approvals`) are disabled unless an `ADMIN_TOKEN` is set, and then require
that token in the `X-Admin-Token` header. Leaked or weak admin tokens, ways around the
approval gate, and ways to get another customer's order data are all in scope.
