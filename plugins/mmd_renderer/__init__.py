"""plugins/mmd_renderer — MMD 渲染插件（QWebEngineView + three.js 骨架）。

具体渲染器 :class:`MMDRenderer` 依赖 QtWebEngine，按需从 ``.renderer`` 延迟
导入，不在包初始化期触发，避免无 QtWebEngine 的环境在 import 期失败。
纯 Python 的配置/协议部分（config / bridge / channel）可安全直接导入。
"""

from __future__ import annotations
