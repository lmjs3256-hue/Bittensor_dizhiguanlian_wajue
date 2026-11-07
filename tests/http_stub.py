"""测试辅助：把 ``fake(url, **kwargs)`` 形状的桩适配到 ``taostats._http_get``。

0.8.16 起所有出站请求都经过 ``taostats._http_get``（比 ``httpx.get`` 多一个
``proxy_url`` 参数），各测试里的桩只关心 url / params / headers。用这个适配器
包一层，测试意图不必跟着实现细节一起改。
"""

from __future__ import annotations


def as_http_get(fake):
    def wrapper(url, *, params=None, headers=None, timeout=None, proxy_url=None):
        return fake(url, params=params, headers=headers, timeout=timeout)

    return wrapper
