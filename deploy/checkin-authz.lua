-- 中转站签到面板 /checkin/ 访问控制：复用 <你的域名> 自己的登录态 + 账号白名单。
-- （从 /etc/nginx-auth/kb-authz.lua 派生，判定逻辑完全一致，仅改文案与回跳地址）
--
-- 部署位置：/etc/nginx-auth/checkin-authz.lua（源码副本在
--   /opt/checkin-panel/deploy/checkin-authz.lua，改完要 install 覆盖再 reload）
-- 不能直接放 /root —— nginx worker 跑在 www 下，穿不过 /root（700）。
--
-- 为什么不用 nginx 原生 auth_request：
--   后端 /api/session 对未登录也返 200（body 里 logged_in:false），
--   auth_request 只看状态码，用它等于放行所有人。必须读 body 才能判断。
--
-- 为什么不用 ngx.location.capture 做子请求：
--   本 vhost 开了 http2，ngx_lua 0.10.26 的子请求不支持 HTTP/2，会抛
--   "http2 requests not supported yet" 然后 500。cosocket 不受这个限制，
--   所以这里手写一个 HTTP/1.1 请求。
--
-- 改白名单只动下面 ALLOW 一行，reload 即生效（不用重启 voice2word）。

local ALLOW = { xiao = true }

local BACKEND_HOST = "127.0.0.1"
local BACKEND_PORT = <鉴权后端端口>
local SESSION_PATH = "/api/session"
local COOKIE_NAME  = "utils_session"
local TIMEOUT_MS   = 2000

local cjson = require("cjson.safe")

local function html_escape(s)
  if not s then return "" end
  s = tostring(s)
  s = s:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;")
  s = s:gsub('"', "&quot;"):gsub("'", "&#39;")
  return s
end

-- 整页导航给 HTML/跳转，其余（fetch/xhr）给 JSON，别把 HTML 塞进 JSON 响应
local function wants_html()
  local a = ngx.var.http_accept or ""
  return ngx.re.find(a, "text/html", "jo") ~= nil
end

local function deny_anonymous()
  if wants_html() then
    -- 送去 utils 那个带样式的登录页，并记住回跳地址
    return ngx.redirect("/login?next=" .. ngx.escape_uri(ngx.var.request_uri), 302)
  end
  ngx.status = 401
  ngx.header["Content-Type"] = "application/json; charset=utf-8"
  ngx.header["Cache-Control"] = "no-store"
  ngx.say('{"error":"login required"}')
  return ngx.exit(401)
end

local function deny_wrong_user(who)
  -- 已登录但不在白名单：不能跳登录页（已有会话会被弹回首页 → 来回打转），
  -- 给一个说明页 + 切换账号入口。
  if not wants_html() then
    ngx.status = 403
    ngx.header["Content-Type"] = "application/json; charset=utf-8"
    ngx.header["Cache-Control"] = "no-store"
    ngx.say('{"error":"forbidden"}')
    return ngx.exit(403)
  end
  ngx.status = 403
  ngx.header["Content-Type"] = "text/html; charset=utf-8"
  ngx.header["Cache-Control"] = "no-store"
  ngx.say([[<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>无权访问 · 签到面板</title>
<meta name="robots" content="noindex, nofollow">
<style>
body{min-height:100vh;display:grid;place-items:center;margin:0;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  background:#f6f7f2;color:#1f2328}
.box{max-width:420px;padding:36px 32px;text-align:center;background:#fff;
  border-radius:16px;box-shadow:0 1px 3px rgba(0,0,0,.06),0 8px 24px rgba(0,0,0,.06)}
.mark{width:46px;height:46px;border-radius:13px;margin:0 auto 16px;background:#3f51b5;
  color:#fff;display:grid;place-items:center;font-size:22px;font-weight:700}
h1{font-size:19px;margin:0 0 10px;font-weight:650}
p{font-size:13.5px;line-height:1.75;color:#656d76;margin:0 0 22px}
b{color:#3f51b5;font-weight:640}
a{display:inline-block;padding:9px 20px;border-radius:8px;background:#3f51b5;
  color:#fff;text-decoration:none;font-size:13.5px;font-weight:520}
a:hover{background:#354497}
</style></head><body><div class="box"><div class="mark">🔒</div>
<h1>签到面板不对你开放</h1>
<p>当前登录账号 <b>]] .. html_escape(who) .. [[</b> 不在允许名单里。<br>
换个有权限的账号登录试试。</p>
<a href="/login?next=%2Fcheckin%2F">切换账号</a></div></body></html>]])
  return ngx.exit(403)
end

-- 没有会话 cookie 就不必打后端，直接当未登录（省一次 TCP）
local cookie = ngx.var.http_cookie
if not cookie or not ngx.re.find(cookie, COOKIE_NAME .. "=", "jo") then
  return deny_anonymous()
end

-- 手写 HTTP/1.1 请求问后端"这个会话是谁"
local sock = ngx.socket.tcp()
sock:settimeouts(TIMEOUT_MS, TIMEOUT_MS, TIMEOUT_MS)

local ok, err = sock:connect(BACKEND_HOST, BACKEND_PORT)
if not ok then
  ngx.log(ngx.ERR, "kb-authz: 连不上后端 ", BACKEND_HOST, ":", BACKEND_PORT, " — ", err)
  -- 后端挂了就拒绝而不是放行（fail closed）
  return ngx.exit(503)
end

local req = table.concat({
  "GET " .. SESSION_PATH .. " HTTP/1.1",
  "Host: " .. (ngx.var.host or "localhost"),
  "Cookie: " .. cookie,
  -- 后端据此判断是否按 https 校验 cookie，不带会走错分支
  "X-Forwarded-Proto: " .. (ngx.var.scheme or "https"),
  "X-Real-IP: " .. (ngx.var.remote_addr or ""),
  "Accept: application/json",
  "Connection: close",
  "", ""
}, "\r\n")

local bytes, serr = sock:send(req)
if not bytes then
  ngx.log(ngx.ERR, "kb-authz: 发送失败 — ", serr)
  sock:close()
  return ngx.exit(503)
end

local line, rerr = sock:receive("*l")
if not line then
  ngx.log(ngx.ERR, "kb-authz: 读状态行失败 — ", rerr)
  sock:close()
  return ngx.exit(503)
end
local status = tonumber(line:match("^HTTP/%d%.%d%s+(%d%d%d)") or "0")

local content_length
while true do
  local h = sock:receive("*l")
  if not h or h == "" then break end
  local n = h:lower():match("^content%-length:%s*(%d+)")
  if n then content_length = tonumber(n) end
end

local body
if content_length and content_length > 0 then
  body = sock:receive(content_length)
else
  body = sock:receive("*a")
end
sock:close()

if status ~= 200 or not body then
  ngx.log(ngx.ERR, "kb-authz: 后端返回异常 status=", status)
  return ngx.exit(503)
end

local data = cjson.decode(body)
if not data then
  ngx.log(ngx.ERR, "kb-authz: session JSON 解析失败")
  return ngx.exit(503)
end

if data.logged_in ~= true then
  return deny_anonymous()
end

local who = data.user
if not who or not ALLOW[who] then
  return deny_wrong_user(who or "(未知)")
end

-- 放行
return
