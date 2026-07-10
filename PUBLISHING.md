# Publishing sqldoc

How to cut a release: push the CI workflow, publish to PyPI, and (optionally)
create the GitHub Release. Steps that need **your** credentials are called out.

## Status of prep

- ✅ Package builds: `python -m build` produces `dist/sqldoc-<ver>.tar.gz` and
  `dist/sqldoc-<ver>-py3-none-any.whl`.
- ✅ `twine check dist/*` passes.
- ✅ The name **`sqldoc`** is available on PyPI (as of this writing).
- ⚠️ **Two decisions below** (license + monetization) before publishing publicly.

---

## Decision 1 — License

`pyproject.toml` has **no `license` field**, so PyPI will show "License:
UNKNOWN" and, legally, no license means *all rights reserved* (proprietary). Pick
one and add it before publishing:

- **Proprietary / commercial** (matches the paid-tier model in
  `pricing-strategy.md`): add a `LICENSE` with your commercial terms and set
  `license = { file = "LICENSE" }` in `pyproject.toml`.
- **Open-source** (MIT/Apache-2.0): add the standard `LICENSE` and
  `license = { text = "MIT" }`. Simpler adoption, but see Decision 2.

## Decision 2 — Public PyPI vs. the paid tiers

Publishing to **public PyPI makes the entire tool `pip install`-able and its
source readable by anyone** — including the `sqldoc scan` compliance features you
priced at $149/mo. Before you `twine upload`, decide the model:

- **Open-core** — publish the free/Professional features to PyPI; gate Compliance
  (`scan`) and Enterprise behind a license-key check or a separate private
  package. (Requires a small licensing/entitlement layer — not yet built.)
- **Free tool, paid support/hosting** — everything is free to run; you sell
  support, SLAs, air-gapped/SSO builds, and managed scanning.
- **Fully proprietary** — do **not** publish to public PyPI; distribute via a
  **private index** (e.g. a self-hosted or cloud private registry) and issue
  install credentials per customer.

`pip install sqldoc` from public PyPI only makes sense under the first two
models. If you want the paid tiers enforced, stop here and let's build the
entitlement layer / private-index flow instead.

---

## One-time setup (your credentials)

1. Create a PyPI account at <https://pypi.org/account/register/> and enable 2FA.
2. Create a **project-scoped API token** at
   <https://pypi.org/manage/account/token/> (scope it to the `sqldoc` project
   after the first upload; use an account-wide token for the very first upload).
   Store it as the password with username `__token__`.

   Recommended alternative: **Trusted Publishing (OIDC)** — no long-lived token.
   Configure it at <https://pypi.org/manage/account/publishing/> for this repo +
   a release workflow, then GitHub Actions can publish with no stored secret.

## Dry run on TestPyPI (recommended first)

```bash
python -m build
python -m twine upload --repository testpypi dist/*
# verify in a clean venv:
pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ sqldoc
sqldoc --version
```

## Publish to PyPI

```bash
python -m build                 # fresh sdist + wheel from a clean tree
python -m twine check dist/*
python -m twine upload dist/*   # username: __token__   password: <your token>
```

Then verify:

```bash
pip install sqldoc
sqldoc --version        # -> sqldoc, version 1.1.0
```

## Automated publish on tag (optional)

With Trusted Publishing configured, add a workflow that builds and uploads on a
`v*` tag push, so `git tag vX.Y.Z && git push --tags` cuts a release. (This also
needs the `workflow` scope to push — see below.)

---

## Note: pushing workflow files needs the `workflow` token scope

Files under `.github/workflows/` (the CI workflow, and any release workflow)
cannot be pushed with a Personal Access Token that lacks the **`workflow`**
scope. To push `.github/workflows/ci.yml`:

1. GitHub → Settings → Developer settings → Personal access tokens → your token →
   enable the **`workflow`** scope (or create a new token with it), and update
   your git credential.
2. Then:
   ```bash
   git add .github/workflows/ci.yml
   git commit -m "ci: add GitHub Actions workflow"
   git push
   ```

Alternatively, add the workflow through the GitHub web UI (Actions → New workflow
→ paste `ci.yml`), which doesn't require the scope locally.
