# Thin SickChill image for self-hosted (UnRAID) deploys.
#
# Reuses the LinuxServer.io runtime (s6 init, PUID/PGID, the /config datadir contract that existing
# appdata and UnRAID templates depend on) and installs THIS repo's SickChill + the optional AI extra
# on top of it. Published to GHCR by .github/workflows/deploy-image.yml on release tags.
#
# The base is a build-arg so a release can repin it. It defaults to a specific LinuxServer.io version
# tag rather than :latest, because LSIO's rolling linuxserver/sickchill:latest is currently an EMPTY
# OCI index (no platform manifests) and fails to pull in a clean build. This pinned tag is the exact
# runtime the target box already runs (2024.3.1-ls258) and ships linux/amd64; it gives us a stable,
# reproducible runtime shell while our own app code still auto-updates via our release tags.
ARG BASE_IMAGE=linuxserver/sickchill:2024.3.1-ls258
FROM ${BASE_IMAGE}

# Replace the pip-installed stock SickChill with this fork (uninstall first: our version string matches
# the stock release, so a plain install would be a no-op), then install with the [ai] extra so the only
# new dependency, anthropic, comes along. The final import is a build-time gate that fails the build if
# our code or a required dependency cannot be imported.
COPY . /tmp/sc-src
RUN /lsiopy/bin/pip uninstall -y sickchill \
 && /lsiopy/bin/pip install --no-cache-dir "/tmp/sc-src[ai]" \
 && rm -rf /tmp/sc-src \
 && /lsiopy/bin/python -c "import sickchill, anthropic, subliminal.providers.opensubtitlescom; print('build import OK', anthropic.__version__)"
