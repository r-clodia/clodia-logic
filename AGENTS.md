# Repository instructions

## Issue tracking

All Clodia issues are centralized in
[`r-clodia/clodia-platform`](https://github.com/r-clodia/clodia-platform/issues).
Do not create issues in this component repository. Implement changes here when
this component owns the affected code, while keeping discussion and status in
the centralized tracker.

## Development workflow

Never implement features or issues directly on `main`. Create one dedicated
branch for each feature or issue, commit the scoped changes, push the branch,
and open a pull request. Keep unrelated work in separate branches and pull
requests.
