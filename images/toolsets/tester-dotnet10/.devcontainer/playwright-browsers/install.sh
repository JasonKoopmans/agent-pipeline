#!/usr/bin/env bash
set -euo pipefail
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Playwright's own --with-deps maps to Ubuntu/Debian 12 package names, which
# Debian 13 (the core image's base) renamed, so install Chromium's runtime
# libraries explicitly and let Playwright download only the browser.
apt-get update
apt-get install -y --no-install-recommends \
  libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libdbus-1-3 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
  libcairo2 libasound2t64 libatspi2.0-0t64 libx11-6 libxcb1 libxext6 \
  fonts-liberation fonts-noto-color-emoji fonts-unifont
only_shell=""; [ "${ONLYSHELL:-true}" = "true" ] && only_shell="--only-shell"
# shellcheck disable=SC2086
npx -y "playwright@${VERSION}" install ${only_shell} ${BROWSERS}
chmod -R a+rX /ms-playwright
rm -rf /root/.npm /var/lib/apt/lists/*
