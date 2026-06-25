class AppMagicSession:
    """AppMagic 的 cookie/会话托管（§7）：`cf_clearance`（过 Cloudflare）+ `dashly_auth_token`（登录态）。

    本期最简形态——**外部注入** cookie（来自 config/env）。两者齐备才 `available()`；缺任一即降级
    （采集器返回空、不阻断其它源）。`invalidate()` 供探测到过期（401/403/Cloudflare 验证页）后置失效。
    Playwright 常驻登录 context 自动刷新留待后续运维方案。
    """

    def __init__(self, cf_clearance: str | None = None, auth_token: str | None = None):
        self._cf_clearance = cf_clearance or None
        self._auth_token = auth_token or None
        self._valid = True

    def available(self) -> bool:
        return self._valid and bool(self._cf_clearance) and bool(self._auth_token)

    def cookie_header(self) -> str:
        return f"cf_clearance={self._cf_clearance}; dashly_auth_token={self._auth_token}"

    def invalidate(self) -> None:
        # 探测到过期后置失效；外部注入模式下需重新提供 cookie 才恢复。
        self._valid = False
