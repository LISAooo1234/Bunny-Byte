"""内置热门包名单 + 拼写近似检查（离线）。

名单在 bunnybyte/data/top_pypi.txt、top_npm.txt，按热度排序（scripts/update_top_packages.py 刷新）。
- 整份名单里的名字 = 常见包，直接放行，不联网；
- 不在名单里、又和前 TYPO_TARGETS 个热门包只差一个字符（增、删、改、相邻对调）的名字 = 疑似拼错 / 仿冒。
5 个字符以下的名字不比（`six`、`ms` 这种短名字互相之间本来就只差一两个字母）。
"""

from functools import lru_cache
from importlib import resources

from .dependency_extract import NPM, PYPI, normalize

LIST_FILES = {PYPI: "top_pypi.txt", NPM: "top_npm.txt"}
TYPO_TARGETS = 2000
MIN_TYPO_LENGTH = 5


@lru_cache(maxsize=None)
def popular_names(ecosystem):
    """(全部名字的集合, 按热度排的前 TYPO_TARGETS 个) ，都已规范化。名单缺失时为空。"""
    try:
        text = resources.files("bunnybyte.data").joinpath(LIST_FILES[ecosystem]).read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return frozenset(), ()
    names = [normalize(ecosystem, line.strip()) for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return frozenset(names), tuple(dict.fromkeys(names))[:TYPO_TARGETS]


def is_popular(ecosystem, name):
    return normalize(ecosystem, name) in popular_names(ecosystem)[0]


def lookalike(ecosystem, name):
    """不在名单里、和某个热门包只差一个字符时返回那个热门包名，否则返回空串。"""
    key = normalize(ecosystem, name)
    known, targets = popular_names(ecosystem)
    if key in known or len(key) < MIN_TYPO_LENGTH:
        return ""
    for target in targets:
        if len(target) >= MIN_TYPO_LENGTH and abs(len(target) - len(key)) <= 1 and within_one_edit(key, target):
            return target
    return ""


def within_one_edit(a, b):
    """Damerau-Levenshtein 距离恰好为 1（一次增 / 删 / 改 / 相邻对调）。"""
    if a == b:
        return False
    if len(a) == len(b):
        diff = [index for index in range(len(a)) if a[index] != b[index]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long = (a, b) if len(a) < len(b) else (b, a)
    if len(long) - len(short) != 1:
        return False
    index = 0
    while index < len(short) and short[index] == long[index]:
        index += 1
    return short[index:] == long[index + 1 :]
