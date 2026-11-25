"""对指定目标用例重复执行，不修改业务或测试文件。"""

import pytest


@pytest.fixture(autouse=True, params=range(10))
def repeat_case(request):
    return request.param
