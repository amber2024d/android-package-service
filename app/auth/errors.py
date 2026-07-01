"""鉴权领域错误（阶段 18）。纯领域异常，不含 HTTP 语义——由路由层映射成状态码。"""


class AuthError(Exception):
    """鉴权领域错误基类。"""


class AdminSeatTakenError(AuthError):
    """已存在管理员且登录者 open_id 不匹配——单管理员名额已被占用（§3.3，路由层映射 403）。"""

    def __init__(self, existing_open_id: str):
        super().__init__("本服务仅允许一个管理员，注册名额已被占用")
        self.existing_open_id = existing_open_id
