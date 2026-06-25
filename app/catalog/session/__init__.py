"""版本目录的会话/cookie 托管（阶段 17）。

AppMagic 这类源既要过 Cloudflare（`cf_clearance`）又要带登录态（`dashly_auth_token`），需要持续运维。
本包先实现「外部注入 cookie」的最简形态；Playwright 常驻登录 context 自动刷新留待后续。
"""
