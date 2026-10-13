#!/bin/sh
# 容器入口：按需在容器内起一套虚拟显示器 + noVNC。
#
# 为什么要这层：面板的「浏览器登录」按钮**故意开有界面的浏览器**（要人手动点
# GitHub/LinuxDO 授权），无显示器的容器里 Chromium 直接崩：
#   TargetClosedError ... Missing X server or $DISPLAY
# 上游的解法是在 docker-compose 里按需起一个 VNC 容器（要手工改 yml + SSH 隧道）。
# 常驻服务（不是开发者本机）更适合把虚拟显示器放在容器内，VNC 按需暴露：
#   Xvfb :99   → 有头 Chromium 的「屏幕」
#   x11vnc     → 把 :99 转成 VNC（只绑 127.0.0.1）
#   websockify → 把 VNC 转成网页可看的 noVNC（绑 0.0.0.0:6080，外层代理加鉴权）
#
# 开关：CHECKIN_VNC=1 打开（默认关）。
# **不打开也能正常用**：纯 HTTP 签到不受影响。
# 注意一点：Sub2API 那类站的自动登录用的是 headless=False（无头会被 Cloudflare 判为
# 机器人），所以它**需要**这一层；其它站不需要。

# 故意不设 `set -e`。
# 踩过的坑（2026-10-12）：`docker restart` / 崩溃重启后，`/tmp` 会留下上一轮的
# `/tmp/.X99-lock` 和 `X99` socket。Xvfb 见到残留锁就直接退出，而 `set -e` 会让它
# 顺带把整个 entrypoint 带走 —— 容器陷入「重启 → Xvfb 失败 → 退出 → 重启」死循环，
# **面板彻底起不来**。辅助组件坏掉只该让「浏览器登录」不可用，不该让面板挂掉。
set -u

# profile 里崩溃残留的 Singleton* 锁每次启动都清一次，和开不开 VNC 无关。
# 踩过的坑（2026-10-03）：浏览器进程被强杀后，profile 里留下 SingletonLock /
# SingletonSocket，下次启动 Chromium 认为「profile 被另一台机器的进程占用」，想弹
# 确认框又弹不出来（无 GUI），最后报出的却是误导性的 "Missing X server or $DISPLAY"
# —— 让人往显示器方向查半天。只在确认进程已死时才清（见 browser.py 的实现）。
if [ -n "${CHECKIN_BROWSER_PROFILE_DIR:-}" ] && [ -d "${CHECKIN_BROWSER_PROFILE_DIR}" ]; then
	find "$CHECKIN_BROWSER_PROFILE_DIR" -name 'Singleton*' -delete 2>/dev/null || true
fi

if [ "${CHECKIN_VNC:-0}" = "1" ]; then
	echo '[VNC] 清理上次残留的 X 锁 ...'
	rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

	echo '[VNC] 启动虚拟显示器 :99 ...'
	Xvfb :99 -screen 0 1440x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
	sleep 2

	echo '[VNC] 启动 x11vnc（只绑 127.0.0.1）...'
	x11vnc -display :99 -rfbport 5900 -localhost -nopw -forever -shared \
		-o /tmp/x11vnc.log -bg >/dev/null 2>&1 || true
	sleep 1

	echo '[VNC] 启动 noVNC 网页桥（0.0.0.0:6080，外层代理要加鉴权）...'
	websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 \
		>/tmp/websockify.log 2>&1 &
	sleep 1

	# 让面板的浏览器出现在这块虚拟屏幕上（有头模式需要 DISPLAY）。
	export DISPLAY=:99
fi

# 交给上游原来的入口（run.py 会做 sandbox.prepare + uvicorn.run）。
exec python run.py
