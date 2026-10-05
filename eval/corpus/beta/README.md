# beta

A reporting portal built on alpha, used as the second fixture for links
between projects.

It runs alpha's worker image for its nightly jobs, depends on the `alpha-api`
package for the shop client and on the `alpha-worker-lib` Python package for
the report formats.

`deploy/` is the workspace that deploys the portal, laid out by the
`hierarchy:` in `data/common.yaml`: the node `web-01.example.com`, which
alpha's `infra/` circuit creates, takes the `portal` role, and the role runs
the `base` and `nginx` modules. Its hosts install packages from the `repo`
bucket alpha's storage circuit creates.
