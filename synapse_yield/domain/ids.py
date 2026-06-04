from uuid import uuid4

# 用当前时间戳 + 随机数 + 机器信息组合成一个 128 位的数字，靠随机性和巨大的取值空间保证全球唯一。
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"

