# The container way to run the panel. One of three (ADR-0016).
#
#   docker compose up -d
#
# Two stages: Node builds the SPA, Python runs it. Node is not in the final image.

# ---------------------------------------------------------------------------
# Stage 1 — the SPA
# ---------------------------------------------------------------------------
FROM node:24-alpine AS spa

WORKDIR /build

# Lockfile first, so a source-only change does not reinstall node_modules.
COPY frontend/package.json frontend/package-lock.json ./
# --legacy-peer-deps is not a container workaround: plain `npm ci` fails on the host too
# (measured). `@heroui/theme@2.4.26` declares `peerDependencies: tailwindcss >=4.0.0` while
# the project is on `tailwindcss@^3.4.17`, so npm 7+ refuses the tree that `node_modules/` was
# actually installed with. This flag reproduces that same tree rather than papering over a
# difference. The real fix is a frontend decision -- tailwind v4, or a @heroui/theme that
# accepts v3 -- and does not belong to packaging.
# 本机部署: 走国内镜像，构建机在大陆直连 registry.npmjs.org 慢且不稳。
RUN npm ci --legacy-peer-deps --registry=https://registry.npmmirror.com

COPY frontend/ ./
# `npm run build` is plain `vite build` -- no tsc, so a type error does not fail the image.
# Type-checking stays a separate `npx tsc --noEmit`, as it is for a local build.
# 本机部署: 本部署挂在 https://<你的域名>/checkin/ 子路径，vite base 由 build arg
# 注入（vite.config.ts 读 CHECKIN_BASE；缺省 '/' = 上游原行为）。
ARG CHECKIN_BASE=/
ENV CHECKIN_BASE=${CHECKIN_BASE}
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 2 — the panel
# ---------------------------------------------------------------------------
FROM python:3.14-slim

# tzdata: `checkin_after` and the scheduler's day boundary are local time
# (service.window_start), so a container on UTC would open a site's window at the wrong hour.
# Set TZ in compose to whatever the accounts' sites use.
#
# The Chromium libraries come from playwright's own `install-deps` rather than a list written
# here: it is maintained against each Debian release, and this image is trixie. The browser
# *binary* is not installed -- see CLOAKBROWSER_CACHE_DIR below.
#
# No CJK fonts on purpose: the panel's UI renders in the reader's own browser, and the browser
# login matches DOM text, not pixels (panel/browser_login.py saves no screenshots). Add
# fonts-noto-cjk only if you start capturing them, and expect ~100MB.
# 本机部署: Debian/pip 均切到腾讯镜像（trixie 的 deb822 源文件也要改）。
# 本机部署: Debian/pip 均切到腾讯镜像（trixie 的 deb822 源文件也要改）。
# 本机部署: 额外装 Xvfb（playwright install-deps 通常已带，这里显式声明）+ x11vnc/novnc
#   做「有头浏览器 + 网页 VNC」——面板的「浏览器登录」要人工点授权按钮，无显示器
#   的容器里必须有虚拟显示 + 看得到的画面。x11vnc/novnc 是可见性所必需，不装就会
#   复现 TargetClosedError: Missing X server or $DISPLAY。
RUN sed -i 's@deb.debian.org@mirrors.tencent.com@g' /etc/apt/sources.list.d/debian.sources \
	&& apt-get update \
	&& apt-get install -y --no-install-recommends tzdata xvfb x11vnc novnc websockify \
	&& pip install --no-cache-dir -i https://mirrors.tencent.com/pypi/simple playwright==1.62.0 \
	&& playwright install-deps chromium \
	&& pip uninstall -y -q playwright \
	&& apt-get clean \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 本机部署: 部署层入口脚本（Xvfb + x11vnc + noVNC 包装原 CMD）。
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 本机部署: 修 Debian noVNC 1.6.0 的一个 bug —— app/ui.js 的 addOption() 用
# `selectbox.options.add(optn)`（HTMLSelectElement.options 是 HTMLOptionsCollection，
# 没有 add 方法，现代浏览器直接抛 "selectbox.options.add is not a function"，
# 整个 noVNC 界面白屏只剩错误框）。换成标准 appendChild。
# 实测 2026-10-03：不修则 vnc.html 直接报错，修后 canvas 正常出画面。
RUN sed -i 's/selectbox\.options\.add(optn);/selectbox.appendChild(optn);/' /usr/share/novnc/app/ui.js \
	&& grep -q 'selectbox.appendChild(optn)' /usr/share/novnc/app/ui.js

# Into a directory of the same name, not flattened with `./`: browser.txt includes base.txt
# with a bare `-r base.txt`, which pip resolves against the *including file's* directory.
COPY requirements/base.txt requirements/browser.txt ./requirements/
RUN pip install --no-cache-dir -i https://mirrors.tencent.com/pypi/simple -r requirements/browser.txt

# Only what the panel imports. `panel/vendor/` rides along inside this one copy: the
# cloakbrowser helpers and, beside them, the BSD-2 notice clause 2 asks a binary
# redistribution to reproduce — a published image being one.
COPY panel/ ./panel/
COPY run.py pytest.ini CONTEXT.md ./
COPY --from=spa /build/dist ./frontend/dist

# ADR-0006 says dependencies live inside the project folder. In a container "the project
# folder" is /app, and these three are volumes rather than layers: the database because it is
# the only copy of the accounts (ADR-0003), the profiles because they hold live sessions, and
# the browser because it is ~500MB that must survive `docker compose down`.
ENV CLOAKBROWSER_CACHE_DIR=/app/.local/cloakbrowser \
	CHECKIN_BROWSER_PROFILE_DIR=/app/.browser_profiles \
	PYTHONUNBUFFERED=1

# 0.0.0.0 is not the trust boundary here -- it cannot be. A container reaches its own
# published port through the bridge, so binding loopback inside would make the panel
# unreachable. What limits exposure is compose's `127.0.0.1:8000:8000`, and that is the line
# to change (see docker-compose.yml, and never publish this on a LAN: no auth, and
# /api/accounts answers with credentials in the clear).
ENV PANEL_HOST=0.0.0.0 \
	PANEL_PORT=8000

# Not root: the process that holds plaintext credentials should not own the filesystem it
# runs on. The three directories are created and handed over here so named volumes inherit
# the ownership; a *bind* mount keeps the host's, so chown it to 10001 or the panel cannot
# write its database.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin panel \
	&& mkdir -p /app/data /app/.browser_profiles /app/.local/cloakbrowser \
	&& chown -R panel:panel /app
USER panel

EXPOSE 8000

# /api/health is static and touches no account, so a probe every 30s costs nothing and cannot
# trip a site's rate limit. urllib rather than curl: it is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
	CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"]

# 本机部署: 经入口脚本启动（先起 Xvfb/x11vnc/noVNC 再 exec run.py）。原 CMD 是 ["python","run.py"]。
CMD ["/app/entrypoint.sh"]
