# Releasing SmartVoice

## Versioning

The release version is defined once in `src/smartvoice/_version.py`. Package metadata, the Python API, `/health`, and OpenAPI read that value. Use Semantic Versioning: increment the patch for compatible fixes, minor for compatible features, and major for breaking changes. While the major version is `0`, minor releases may still contain breaking changes; call those out in the GitHub release notes.

## Release notes guidelines and templates

Release notes describe what users can do differently after upgrading. Generate them from the changes between the previous published tag and the release candidate, not by copying commit subjects. Commit messages can help locate work, but are not reliable evidence of product behavior.

Before drafting notes:

1. Identify the previous published tag and inspect the full diff to the candidate, including renamed files. Group changes by user-visible behavior, such as API contracts, CLI commands, the Console, model support, installation, and runtime behavior.
2. Verify each proposed claim against the implementation and its tests. Use API documentation, model catalogs, and validation reports to confirm defaults, limits, platform scope, and known constraints.
3. Compare old and new public behavior. Call out changes to defaults, accepted inputs, response formats, configuration, or platform requirements that may require user action.
4. Keep the notes focused on outcomes. Omit internal refactors, file moves, routine test additions, and implementation detail unless they change user behavior or provide a relevant operational fix. Do not claim quality, compatibility, performance, or platform support beyond what was tested.
5. Write the final notes in concise English Markdown at `doc/releases/vX.Y.Z.md`. Include only sections that apply; do not add empty sections or turn the notes into a commit-by-commit changelog.

### Patch release template

Patch releases should preserve existing behavior and defaults while fixing defects or improving reliability. If a change alters a default or requires migration, reconsider whether the release should be minor instead.

```markdown
# SmartVoice vX.Y.Z

SmartVoice vX.Y.Z fixes [user-visible problem] and improves [affected behavior].

## Fixes

- Fix [observable defect] that occurred when [condition].
- Improve [reliability or compatibility behavior] for [affected users or models].

## Compatibility

This release preserves the existing API and configuration defaults.
```

Use `## Compatibility` only when it helps confirm an important compatibility point. If there is a user action or limitation, replace it with a concise `## Notes` or `## Migration` section rather than claiming full compatibility.

### Minor release template

Minor releases may add capabilities and, while the major version is `0`, may include breaking changes. List each breaking change explicitly and give users a concrete migration path.

```markdown
# SmartVoice vX.Y.0

SmartVoice vX.Y.0 adds [main capability] and improves [important user workflow].

## Highlights

- **[Capability].** [What users can do and where/how to use it.]
- **[Capability].** [Important limits or platform scope, when relevant.]

## Breaking changes

- [Behavior or contract that changed.] To preserve the previous behavior, [migration step].

## Migration

- [Required user action, configuration change, or client update.]
```

Include `## Breaking changes` and `## Migration` only when applicable. If the release has no breaking changes, omit those sections and use `## Fixes` for compatible corrections. State response defaults, configuration names, supported formats, and required commands precisely; distinguish features included in the standard package from optional or separately built components.

## Create a GitHub release

1. Merge the release changes into `main` after the required checks pass. Run `python scripts/test.py ci` and the distribution checks locally or through the manual Release workflow.
2. Update `__version__` in `src/smartvoice/_version.py` to the release version.
3. Add the matching `doc/releases/vX.Y.Z.md` release notes.
4. Optionally run the Release workflow manually on `main` to build and check the source archive and platform wheels. Manually running it on a version tag also creates the GitHub Release and publishes to PyPI when `ENABLE_PYPI_PUBLISH` is `true`.
5. Create and push the matching tag, including the `v` prefix. For example, for `0.1.0`:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

6. The pushed tag runs the same checks, then creates the GitHub Release using the matching notes file and publishes the distributions to PyPI when `ENABLE_PYPI_PUBLISH` is `true`.

The tag is the release record. Do not move or reuse a published version tag. Fix a bad release with a new patch version.

## Enable PyPI publishing

PyPI publishing is disabled until explicitly enabled for this repository. To turn it on:

1. On PyPI, add a pending Trusted Publisher for owner `dxhome`, repository `SmartVoice`, workflow `release.yml`, and GitHub environment `pypi`.
2. In GitHub repository settings, create an environment named `pypi`. Add required reviewers if releases should wait for approval before publishing.
3. In **Settings → Secrets and variables → Actions → Variables**, add `ENABLE_PYPI_PUBLISH` with value `true`.
4. Push the matching version tag. After validation, GitHub Actions publishes the same built wheel and source archive to PyPI using Trusted Publishing; no PyPI API token is stored in GitHub.

Once published, users can install or upgrade with:

```bash
python -m pip install smartvoice
python -m pip install --upgrade smartvoice
```

The PyPI distribution contains the application and default metadata/configuration files. Large speech model weights remain separate and are installed through SmartVoice after package installation.
