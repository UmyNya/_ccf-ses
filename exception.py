class SESError(Exception):
    """系统错误"""
    __module__ = "ses"


class ConfigError(Exception):
    """用户配置错误"""
    __module__ = "ses"

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return "Config error: " + self.message


class NumberTypeParamValueError(ConfigError):

    def __init__(self, param_name, value, greater_than=0, prefix=None):
        if not prefix:
            prefix = ""

        self.message = (f"{prefix}Invalid parameter value: {param_name}={value}. "
                        f"Requires a number greater than {greater_than}")


class SceneParameterNotFound(Exception):
    """缺少场景参数"""
    __module__ = "ses"

    def __init__(self, param_name: str):
        param_name = param_name.strip("'")
        self.param_name = param_name

    def __str__(self):
        return f"Config error: Requires global/scene parameter '{self.param_name}'"


class EnvironmentValidationError(Exception):
    """环境检验异常"""
    __module__ = "ses"


class CustomAPIError(Exception):
    """外部API异常"""
    __module__ = "ses"


class CaseTimeoutError(Exception):
    """用例内部操作超时异常"""
    __module__ = "ses"


class CasePreConditionError(Exception):
    """用例前置条件校验失败"""
    __module__ = "ses"


class CaseFailedError(Exception):
    """用例执行失败"""
    __module__ = "ses"
