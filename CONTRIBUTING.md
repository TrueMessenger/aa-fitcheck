# Contributing to aa-fitcheck

Thanks for your interest in improving aa-fitcheck! Bug reports, feature ideas, and
pull requests are all welcome.

## Development setup

aa-fitcheck targets **Python 3.10–3.12** and Alliance Auth 4.x–5.x. The repo ships a
small dev/test Auth site (`testauth/`, SQLite + fakeredis) so you can run the suite
without a full Auth deployment.

```bash
# create a virtualenv (any tool; uv shown here)
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate

# install the package plus the test extras (coverage + securegroups) and fakeredis
pip install -e ".[tests]" fakeredis

# run the suite
python manage.py test fitcheck
```

To exercise the optional integrations locally, also install their packages:

```bash
pip install -e ".[securegroups]"   # Secure Groups smart filter
pip install allianceauth-corptools # corptools asset read-through
```

The full game static data (SDE) is only needed for live runs, not the test suite:

```bash
python manage.py fitcheck_load_sde
```

## Before you open a PR

- **Branch per change** — never commit directly to `main`.
- **Tests pass**: `python manage.py test fitcheck`.
- **Migrations are complete**: `python manage.py makemigrations --check --dry-run`
  must report no changes. Commit any new migration with your change.
- **Add tests** for new behaviour; the engine and services are well covered and new
  code should be too.
- Keep the change focused; describe the *why* in the PR.

## Versioning & changelog

This project follows [Semantic Versioning](https://semver.org/) with a release-driven
flow: add a line under the **[Unreleased]** section of [`CHANGELOG.md`](CHANGELOG.md)
for any user-facing change, and **do not** bump `__version__` in your PR — the version
is bumped once at release time.

## Code style

Match the surrounding code. The project uses Ruff (`pyproject.toml`); keep imports tidy
and prefer the existing patterns (services hold the logic, views stay thin).

## License

By contributing, you agree your contributions are licensed under the project's
**GPL-3.0-or-later** license.
