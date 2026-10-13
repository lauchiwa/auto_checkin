"""图形验证码识别 —— 走本机 <你的网关> 网关的视觉模型。

有些中转站把「签到」和一张图形验证码绑在一起（relay3 的
`checkin_setting.captcha_enabled`，后台原话是「提高脚本批量签到的成本」）。
流程固定两步：

    POST <checkin_path>/captcha   -> {captcha_id, captcha_image(base64 PNG)}
    POST <checkin_path>           -> {captcha_id, captcha_answer}

答案错了站点回「验证码错误，请重试」，重新取一张再试即可，所以只要单次识别
够准，重试几次就能过（实测 auto 模型单次 ~80%，3 次重试 ≈ 99%）。

识别走视觉模型，默认用本机 <你的网关> 网关的 `auto` 模型；地址/密钥/模型都可由
环境变量覆盖。容器访问网关走公网入口（https://<你的网关域名>），因为网关只
监听 127.0.0.1、而宿主 iptables 不放行容器访问 <网关端口> —— 这条链路实测可用。
"""
from __future__ import annotations

import base64
import os
import re
from typing import Optional

import httpx

# 站点在 checkin_path 后面挂的取验证码路由，按顺序试。
CAPTCHA_PATHS = ('/captcha',)
# 站点说「要验证码」的措辞；只有命中才值得去取图识别，别的失败照原样报。
CAPTCHA_REQUIRED = re.compile(r'验证码|captcha', re.I)
# 「答案错了」的措辞：这种要重取重试；别的失败（如「今日已签到」）直接返回。
CAPTCHA_WRONG = re.compile(r'验证码错误|验证码不正确|验证码已过期|captcha\s*(invalid|wrong|expired)', re.I)

VISION_TIMEOUT_S = 60.0
DEFAULT_MODEL = 'auto'
# 站点验证码是「大写字母 + 数字」的定长串，实测 5 位。提示里说清楚，模型更稳。
PROMPT = (
	'这是一张登录签到用的图形验证码，字符为大写字母和数字，共5位。'
	'只输出这5个字符，不要任何解释、标点、空格或换行。'
)


def vision_enabled() -> bool:
	"""识别要用的网关地址和密钥都在，才值得去试。"""
	return bool(os.getenv('CHECKIN_VISION_BASE_URL', '').strip()
	            and os.getenv('CHECKIN_VISION_API_KEY', '').strip())


def _clean(text: str) -> str:
	"""把模型的输出收成一行纯字符：去 markdown、引号、空白与解释性尾巴。"""
	line = (text or '').strip()
	# 去掉 ``` 围栏和常见前后缀
	line = re.sub(r'```[a-z]*', '', line, flags=re.I).strip('`').strip()
	# 取最后一个非空行：模型偶尔会先复述题目再给答案
	for candidate in reversed([l.strip() for l in line.splitlines() if l.strip()]):
		token = re.sub(r'[^0-9A-Za-z]', '', candidate)
		if token:
			return token[:8]
	return ''


async def solve_image(image: str) -> Optional[str]:
	"""让视觉模型读一张验证码。返回识别到的字符；不可用或失败时返回 None。

	`image` 可以是 `data:image/png;base64,...` 也可以是一段裸 base64。
	"""
	base = os.getenv('CHECKIN_VISION_BASE_URL', '').strip().rstrip('/')
	key = os.getenv('CHECKIN_VISION_API_KEY', '').strip()
	model = os.getenv('CHECKIN_VISION_MODEL', DEFAULT_MODEL).strip() or DEFAULT_MODEL
	if not (base and key):
		return None

	raw = (image or '').strip()
	if raw.startswith('data:'):
		url = raw
	elif raw:
		url = f'data:image/png;base64,{raw}'
	else:
		return None

	payload = {
		'model': model,
		'messages': [{
			'role': 'user',
			'content': [
				{'type': 'text', 'text': PROMPT},
				{'type': 'image_url', 'image_url': {'url': url}},
			],
		}],
		# 够宽：网关的 auto 常路由到**推理模型**（deepseek-v4.1-flash 等），思考过程也计入
		# completion_tokens。给窄了（如 200）它会先把预算烧在 reasoning 上，content 直接空。
		'max_tokens': 1500,
	}
	headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
	# 走面板同一个出口代理，而不是直连：容器内 `<你的网关域名>` 解析到 127.0.0.1
	# （容器自己的回环，不是宿主的），直连必然 ConnectError；代理在**宿主机侧**解析域名，
	# 落到宿主 nginx → <你的网关>，实测 200。网关只监听宿主 127.0.0.1、iptables 又不放行
	# 容器访问 <网关端口>，所以经代理是唯一不需要改动生产服务的通路。
	proxy = (os.getenv('CHECKIN_HTTP_PROXY') or os.getenv('CHECKIN_PROXY_URL') or '').strip() or None
	try:
		async with httpx.AsyncClient(timeout=VISION_TIMEOUT_S, proxy=proxy, trust_env=False) as client:
			resp = await client.post(f'{base}/chat/completions', json=payload, headers=headers)
		body = resp.json()
		content = ((body.get('choices') or [{}])[0].get('message') or {}).get('content', '')
		return _clean(content) or None
	except Exception as exc:
		# 识别失败不是致命错误：调用方会当作「这一轮没答对」继续重试/收尾。
		# 但静默失败会让人分不清「模型没认出」和「压根没连上」，所以留一条可查的线。
		if os.getenv('CHECKIN_DEBUG', '').strip() in ('1', 'true', 'yes'):
			import traceback

			print(f'[CAPTCHA] 识别失败: {type(exc).__name__}: {exc}')
			traceback.print_exc()
		return None
