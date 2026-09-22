# Contributing

## Running it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # 237 tests, ~19 s, no phone or network needed
ruff check .
```

The suite fakes both the phone and the network (`tests/fakes/`), so it runs anywhere
and a failure is a real defect rather than a missing device. CI runs exactly these two
commands, plus a `docker build`, on Python 3.12 and 3.13.

## Commit messages and versioning

Commits follow [Conventional Commits](https://www.conventionalcommits.org/), and the
release version is computed from them:

- `feat:` → minor bump
- `fix:` / `perf:` / `revert:` → patch bump
- a breaking change (`type!:` or a `BREAKING CHANGE:` footer) → major bump
- `chore` / `docs` / `style` / `refactor` / `build` / `ci` / `test` → no release

Use `type(scope): description`, e.g. `fix(upstream): release the hold when a probe fails`.

### The entity surface decides, not the commit type

The rule in [CHANGELOG.md](CHANGELOG.md) is authoritative: **the Home Assistant entity
and topic surface is the public interface.** Major means an entity id, topic or payload
shape changed or disappeared; minor means new ones appeared; patch means nothing visible
changed.

Conventional Commits agree with that most of the time, but not always, and the gaps are
the ones that bite:

| Situation | Commit type suggests | The surface rule says | What to do |
|---|---|---|---|
| Internal capability, no new entity | `feat:` → minor | patch | Add `+:patch` |
| Fixing something *by adding* an entity | `fix:` → patch | **minor** | Add `+:minor` |
| Renaming an entity id | `refactor:` → no release | **major** | Add `+:major` |
| Changing a payload's units or shape | `fix:` → patch | **major** | Add `+:major` |
| Improving an entity's *display name* | either | patch | Nothing; ids are pinned with `default_entity_id` precisely so names can change freely |

When the two disagree, the surface rule wins and you say so with an override.

Forgetting the override does not silently ship the wrong number: the release workflow
compares the version the commits imply against `pyproject.toml` and fails if they
disagree, after the merge has already landed on `main`. Recovering means tagging by hand,
which the tag-triggered release workflow accepts because it validates the tag against
`pyproject.toml` rather than against the commit log. Cheaper to add the token.

### Release-override directives

A `+:<spec>` token **alone on its own line** of a commit message overrides the
classification, where `<spec>` is `major` | `minor` | `patch` | `pre` | `prepre` | `rc`.

```
refactor(mqtt): rename the stream_state entity to camera_state

Existing automations that reference sensor.ios2ha_camera_stream_state will stop
resolving and must be edited.

+:major
```

### Attribution trailers

One trailer per commit, chosen for what actually happened: `Helped-By`, `Improved-By`,
`Suggested-By`, `Reviewed-By`, `Reported-By`, `Tested-By`, `Thanks-To`, or
`Commit-Written-By`.

## Branches

Branch as `type/short-description`, matching the commit type of the work
(`fix/cable-self-heal`, `build/ci-and-project-infrastructure`). Push the branch to the
remote before merging it, so the history of a change is recoverable from the remote
alone.

## Documentation that must keep up

Four files describe the running service, and a change that outdates one of them is not
finished:

- `README.md` — what it is and how to get it running.
- `ARCHITECTURE.md` — why it is built this way.
- `CONFIGURATION.md` — every environment variable and control, with defaults.
- `OPERATIONS.md` — running it, the alerts worth having, and what the phone will and
  will not do.

Write them for someone who has only this repository and no other context.
