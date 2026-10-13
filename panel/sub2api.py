"""Sub2API 站点签到 —— 第三套后端（不是 New API，也不是 relay4 那种自建）。

`relay7`、`relay6` 这类站跑的是 **Sub2API**
（`Subscription to API Conversion Platform`，Vue SPA），接口全在 `/api/v1/*`：

    GET  /api/v1/auth/me                -> {id, email, balance, free_balance, ...}
    POST /api/v1/auth/login             -> {access_token, refresh_token, user}
                                           需要 body 带 `turnstile_token`
    POST /api/v1/auth/refresh           -> 用 refresh_token 换新 access_token
    GET  /api/v1/check-in/status        -> {enabled, checked_in_today, current_streak,
                                            today_reward, free_balance, ...}
    POST /api/v1/check-in               -> {turnstile_token: ""}；已签则 already_checked_in
    GET  /api/v1/check-in/history?page=&page_size=

三条实测结论（2026-10-12）：

1. **只有登录要 Turnstile，签到不要**。`check-in/status` 里 `turnstile_required` 为
   false，`POST /check-in` 的 `turnstile_token` 传空串即可。所以**日常签到纯 HTTP**，
   不必每天开浏览器。
2. **登录必须用有头浏览器，且要走完整条流程**。无头下 Cloudflare 直接判为机器人
   （widget 是空框、永不出 token）。有头（容器内 Xvfb `:99`）时，widget 的复选框画在
   div 上而不是 `<input>`，面板原有的 `_click_turnstile`（只找 `input[type=checkbox]`）
   找不到它 —— 必须按坐标点 `.turnstile-wrapper` 左侧约 24px 处，并重复点几次。
   **更关键**：只「单独 render 一个 token 再拿去 HTTP 登录」极不稳定（实测连续 6 轮
   全失败）；而在登录页上把「点 Turnstile → 填表 → 点 Sign In」走完几乎一次就成 ——
   token 与页面/表单上下文是绑定的。所以这里做的是 `browser_login`（整条流程），
   不是 mint。
3. **凭据长期复用**：登录后 localStorage 里拿到 `auth_token`(JWT) + `refresh_token`。
   refresh_token 能长期换新，所以**浏览器只需成功一次**，之后每天纯 HTTP。
   面板 `access_token` 字段存 JWT，`session` 字段存 refresh_token。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import replace
from typing import Optional

import httpx

from panel.newapi import Login, Outcome, SiteInfo

API = '/api/v1'
TIMEOUT_S = 30.0
UA = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
	'(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
def _proxy() -> Optional[str]:
	return (os.getenv('CHECKIN_HTTP_PROXY') or os.getenv('CHECKIN_PROXY_URL') or '').strip() or None


def _client(base_url: str, access_token: Optional[str] = None) -> httpx.AsyncClient:
	headers = {
		'Accept': 'application/json, text/plain, */*',
		'Content-Type': 'application/json',
		'User-Agent': UA,
		'Origin': base_url.rstrip('/'),
		'Referer': base_url.rstrip('/') + '/',
		# Sub2API 用它区分「站点自己的前端」与「第三方脚本」；缺了容易被 WAF/风控挡。
		'X-User-UI-Request': '1',
	}
	if access_token:
		headers['Authorization'] = f'Bearer {access_token}'
	return httpx.AsyncClient(
		base_url=base_url.rstrip('/'),
		timeout=httpx.Timeout(TIMEOUT_S, connect=8.0),
		headers=headers,
		follow_redirects=True,
		proxy=_proxy(),
		trust_env=False,
	)


def _json(response: httpx.Response) -> dict:
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}


def _data(response: httpx.Response) -> dict:
	body = _json(response)
	data = body.get('data')
	return data if isinstance(data, dict) else {}


def _error_of(response: httpx.Response) -> str:
	body = _json(response)
	return str(body.get('message') or body.get('reason') or f'HTTP {response.status_code}')


# ───────────────────────── 登录 ─────────────────────────
# 为什么不做「只 mint 一个 Turnstile token 再拿去 HTTP 登录」：
# 实测（2026-10-12）那样极不稳定 —— 单独 render widget 出来的 token 反复 6 轮全失败，
# 而**在登录页上把整条流程走完**（点 Turnstile → 填表 → 点 Sign In）几乎一次就成。
# 差别在于 token 与页面上下文/表单绑定，独立 mint 出来的那个站方不认。
#
# 也不能复用面板的 `mint_turnstile` / `_click_turnstile`：前者无头（这站无头必被
# Cloudflare 判为机器人，widget 是空框）；后者只找 `input[type=checkbox]`，而这站的
# 复选框画在 div 上（`.turnstile-wrapper` 左侧），必须按坐标点。
async def browser_login(base_url: str, username: str, password: str, profile_name: str) -> dict:
	"""在登录页上完整走一遍：点 Turnstile -> 填表单 -> 提交 -> 读凭据。

	返回 `{access_token, refresh_token, user}`；失败抛异常。**必须 headless=False**
	（容器内 Xvfb `:99`），无头下 Cloudflare 不给 token。
	"""
	from dataclasses import replace as _replace

	from panel.browser_login import (
		launch_login_context,
		load_browser_login_settings,
		prepare_browser_page,
	)

	settings = _replace(
		load_browser_login_settings(profile_name, 'password', persist_profile=False),
		headless=False,
	)
	context = await launch_login_context(settings, use_proxy=True)
	try:
		page = await context.new_page()
		await prepare_browser_page(page)
		# `ERR_ABORTED` 是 SPA 重定向打断首次导航的常态，不是失败；重试即可。
		url = f'{base_url.rstrip("/")}/login'
		for attempt in range(3):
			try:
				await page.goto(url, wait_until='domcontentloaded', timeout=45000)
				break
			except Exception:
				if attempt == 2:
					raise
				await asyncio.sleep(2)
		# 不要等 `networkidle`：这站有长连接/轮询，它必然超时，白等 15 秒 ——
		# 而 Cloudflare 的 widget 在页面稳定前就放弃渲染了（实测加了这 15 秒反而
		# 全落空）。按固定时间等水合即可。
		await asyncio.sleep(8)

		# 1) 先点 Turnstile 拿到 token（提交前必须有）
		box = None
		try:
			box = await page.locator('.turnstile-wrapper').first.bounding_box()
		except Exception:
			box = None
		token = None
		for i in range(25):
			token = await page.evaluate(
				"() => { const e = document.querySelector('input[name=cf-turnstile-response]');"
				' return e && e.value && e.value.length > 20 ? e.value : null; }'
			)
			if token:
				break
			if box:
				# 复选框在容器左侧（不是 `<input>`）；几个偏移轮流点，单偏移容易空手。
				dx = (24, 30, 28, 24)[i % 4]
				await page.mouse.click(box['x'] + dx, box['y'] + box['height'] / 2)
			await asyncio.sleep(2)
		if not token:
			raise RuntimeError('Turnstile 未通过（Cloudflare 没给 token）')

		# 2) 填表单 + 提交
		await page.fill('#email', username)
		await page.fill('#password', password)
		await asyncio.sleep(1)
		await page.get_by_role('button', name='Sign In').click(timeout=10_000)
		await asyncio.sleep(8)

		# 3) 从 localStorage 读凭据（这站把 token 存这里，key 固定）
		creds = await page.evaluate("""() => ({
			access_token: localStorage.getItem('auth_token'),
			refresh_token: localStorage.getItem('refresh_token'),
			user: localStorage.getItem('auth_user'),
		})""")
		access = creds.get('access_token')
		if not access:
			raise RuntimeError(f'登录后 localStorage 无 auth_token（当前 URL {page.url}）')
		user = {}
		try:
			user = json.loads(creds.get('user') or '{}')
		except Exception:
			pass
		return {
			'access_token': access,
			'refresh_token': creds.get('refresh_token'),
			'user': user,
		}
	finally:
		try:
			await context.close()
		except Exception:
			pass


async def _refresh(base_url: str, refresh_token: str) -> dict:
	async with _client(base_url) as client:
		response = await client.post(f'{API}/auth/refresh', json={'refresh_token': refresh_token})
		if response.status_code != 200:
			return {}
		return _data(response)


# ───────────────────────── 签到 ─────────────────────────
async def check_in(login: Login, site: Optional[SiteInfo] = None) -> Outcome:
	"""签到：日常纯 HTTP；凭据失效时才开一次浏览器重新登录。

	三段凭据降级：现成的 access_token → refresh_token 换新 → 浏览器真实登录。
	只有最后一段要浏览器（且只需成功一次 —— refresh_token 可长期复用）。
	"""
	base_url = login.base_url
	token = login.access_token
	session = login.session

	# 三段凭据降级：现成 token -> refresh_token 换新 -> 重新登录（要 mint Turnstile）。
	# 1) 有 token 就直接读状态
	status = None
	if token:
		async with _client(base_url, token) as client:
			response = await client.get(f'{API}/check-in/status')
			if response.status_code == 200:
				status = _data(response)
	# 2) token 不可用 -> refresh
	if status is None and session:
		fresh = await _refresh(base_url, session)
		if fresh.get('access_token'):
			token = fresh['access_token']
			refresh_rotated = fresh.get('refresh_token')
			session = refresh_rotated or session
			async with _client(base_url, token) as client:
				response = await client.get(f'{API}/check-in/status')
				if response.status_code == 200:
					status = _data(response)
	# 3) 还不行 -> 浏览器真实登录（慢，但稳；见 browser_login 的说明）
	if status is None:
		if not (login.username and login.password):
			return Outcome(False, error='凭据已失效，且该站重新登录需要账号密码', session=session)
		try:
			fresh = await browser_login(
				base_url, login.username, login.password, f'sub2api-{_profile_hint(login)}'
			)
		except Exception as exc:
			return Outcome(False, error=f'浏览器登录失败: {exc}', session=session)
		token = fresh.get('access_token')
		session = fresh.get('refresh_token') or session
		async with _client(base_url, token) as client:
			response = await client.get(f'{API}/check-in/status')
			status = _data(response) if response.status_code == 200 else None
		if status is None:
			return Outcome(False, error='登录成功但读不到签到状态', session=session)

	before = _usd(status.get('free_balance'))
	# `checked_in_today` 是站点自己的判断，比本地时钟可靠（本机时钟被桩化过）。
	if status.get('checked_in_today'):
		return Outcome(True, checked_in=False, before_quota=before, after_quota=before,
		               session=session, access_token=token, api_user=_uid(status))
	if not status.get('enabled'):
		return Outcome(False, error='站点未开启签到', session=session, access_token=token)

	async with _client(base_url, token) as client:
		# 签到不需要 Turnstile（status.turnstile_required=False 实测）；空串是站点自己的前端行为。
		ts = '' if not status.get('turnstile_required') else None
		if ts is None:
			return Outcome(False, error='该站签到也要 Turnstile（未支持）', session=session, access_token=token)
		response = await client.post(f'{API}/check-in', json={'turnstile_token': ts})
		body = _data(response)
		after_read = await client.get(f'{API}/check-in/status')
		after_status = _data(after_read) if after_read.status_code == 200 else {}
	after = _usd(after_status.get('free_balance')) or _usd(body.get('free_balance')) or before

	if response.status_code != 200:
		message = str(body.get('message') or _error_of(response))
		if 'already' in message.lower() or '已' in message:
			return Outcome(True, checked_in=False, before_quota=before, after_quota=after,
			               session=session, access_token=token)
		return Outcome(False, error=f'签到失败: {message}', before_quota=before,
		               after_quota=after, session=session, access_token=token)
	reward = body.get('reward_amount') or after_status.get('today_reward')
	return Outcome(
		True, checked_in=True, before_quota=before, after_quota=after,
		session=session, access_token=token,
		awarded=float(reward) if isinstance(reward, (int, float)) else None,
	)


def _profile_hint(login: Login) -> str:
	"""profile 名对 mint 只是隔离用，取账号地址派生一个稳定值即可。"""
	return 'sub2api-' + hashlib.sha256(login.base_url.encode()).hexdigest()[:10]


def _usd(value) -> Optional[float]:
	return round(float(value), 2) if isinstance(value, (int, float)) else None


def _uid(status: dict) -> Optional[str]:
	value = status.get('user_id') or status.get('id')
	return str(value) if value else None


async def looks_like_sub2api(base_url: str) -> bool:
	"""这个站是不是 Sub2API：`/api/v1/check-in/status` 未登录回 401 JSON。

	New API 系没有 `/api/v1/*`；relay4 那种自建后端是 `/user/*`。用 401+JSON 判定，
	两者都不会误命中。
	"""
	try:
		async with _client(base_url) as client:
			response = await client.get(f'{API}/check-in/status')
	except Exception:
		return False
	if response.status_code != 401:
		return False
	if 'json' not in response.headers.get('content-type', ''):
		return False
	return bool(_json(response))
