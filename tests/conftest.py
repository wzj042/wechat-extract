"""pytest 全局配置：未显式传 --basetemp 时，把临时目录固定到仓库根下的 .test-tmp。

默认的系统临时目录在部分 Windows 环境下清理时会报权限错误；临时目录路径过长时，
媒体测试的嵌套目录又会超过 Windows 260 字符的路径上限。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # 必须早于内置 tmpdir 插件的 pytest_configure：它在那时读取 basetemp。
    if not config.option.basetemp:
        config.option.basetemp = str(REPO_ROOT / ".test-tmp")
