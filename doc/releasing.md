# Releasing SmartVoice

## Versioning

The release version is defined once in `src/smartvoice/_version.py`. Package metadata, the Python API, `/health`, and OpenAPI read that value. Use Semantic Versioning: increment the patch for compatible fixes, minor for compatible features, and major for breaking changes. While the major version is `0`, minor releases may still contain breaking changes; call those out in the GitHub release notes.

## Create a GitHub release

1. Merge the release changes into `main` after CI passes.
2. Update `__version__` in `src/smartvoice/_version.py` to the release version.
3. Create and push the matching tag, including the `v` prefix. For example, for `0.1.0`:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. The release workflow checks that the tag matches the package version, runs tests, builds and checks the wheel and source archive, and verifies that default resources are in the wheel. It then creates a GitHub Release with generated notes and attaches both distributions.

The tag is the release record. Do not move or reuse a published version tag. Fix a bad release with a new patch version.

## Enable PyPI publishing

PyPI publishing is disabled until explicitly enabled for this repository. To turn it on:

1. On PyPI, add a pending Trusted Publisher for owner `dxhome`, repository `SmartVoice`, workflow `release.yml`, and GitHub environment `pypi`.
2. In GitHub repository settings, create an environment named `pypi`. Add required reviewers if releases should wait for approval before publishing.
3. In **Settings → Secrets and variables → Actions → Variables**, add `ENABLE_PYPI_PUBLISH` with value `true`.
4. Push the matching version tag. After validation, GitHub Actions publishes the same built wheel and source archive to PyPI using Trusted Publishing; no PyPI API token is stored in GitHub.

Once published, users can install or upgrade with:

```bash
python -m pip install "smartvoice[inference]"
python -m pip install --upgrade "smartvoice[inference]"
```

The PyPI distribution contains the application and default metadata/configuration files. Large speech model weights remain separate and are installed through SmartVoice after package installation.
