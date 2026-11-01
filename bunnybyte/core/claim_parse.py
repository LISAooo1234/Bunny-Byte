"""从最终回答里找出“我改了哪些文件”的声明，给 claim_check 对账用。

宁可漏掉也不误报：路径和改动动词必须在同一个小句里，小句要有完成标记（已 / 了 / 我 / 英文过去式 / 以动词开头的要点），
带否定、条件、将来、问句，或主语是代码 / 提交 / PR / 命令的小句都跳过；代码块、运行时自己追加的 ✓✗⚠ 行不看。
"""

import re

from .verification import HEDGE_RE, RUNTIME_LINE_RE

EXTENSIONS = (
    "py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|c|h|cc|cpp|hpp|cs|rb|php|swift|sh|md|rst|txt|toml|yaml|yml|json|ini|cfg"
    "|html|css|scss|sql|vue|lock|csv"
)
PATH_TOKEN_RE = re.compile(
    r"`(?P<tick>[^`\s]+)`"
    r"|(?<![\w./@\\-])(?P<bare>/?(?:[\w.-]+/)*[\w.-]*\w\.(?:" + EXTENSIONS + r"))(?![\w(/])",
    re.IGNORECASE,
)
PATH_SHAPE_RE = re.compile(r"^/?(?:[\w.-]+/)*(?:[\w.-]*\w\.(?:" + EXTENSIONS + r")|Makefile|Dockerfile)$", re.IGNORECASE)
LOCATION_SUFFIX_RE = re.compile(r"(?:::[\w.\[\]-]+|:\d+(?:[-:]\d+)?)$")
CHANGE_VERB_RE = re.compile(
    r"修改|改动|改了|新增|添加|增加|创建|新建|删除|删掉|移除|重写|更新|编辑|写入"
    r"|\b(?:updated|modified|created|added|deleted|removed|rewrote|rewritten|edited|changed|wrote|written)\b",
    re.IGNORECASE,
)
OTHER_VERB_RE = re.compile(
    r"读了|读取|阅读|查看|看了|检查|运行|跑了|搜索|调用|使用|引用|导入|参考|依赖|基于|根据|对照|比较|测试了|按照|参照|依照|遵循"
    r"|\b(?:read|checked|ran|looked|reviewed|inspected|examined|searched|found|viewed|opened|calls?|uses?|using|imports?|based|tested)\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"没有|没|无需|不需要|不用|不必|不变|未改|并未|未动|不动|保持原样|\b(?:no|not|never|without|untouched|unchanged)\b|n't",
    re.IGNORECASE,
)
FUTURE_RE = re.compile(
    r"会(?!话)|可以|打算|计划|建议|下一步|接下来|稍后|以后|\b(?:will|would|could|can|might|may|going to|plan|next|suggest)\b",
    re.IGNORECASE,
)
# 主语是程序 / 提交 / 别人，不是本回合的 agent：“该函数会创建 out.json”“this PR updated x.py”“你让我改 a.py”
SUBJECT_RE = re.compile(
    r"\bPR\b|pull request|\bcommits?\b|提交|合并|上游|upstream|该|此|这个(?:函数|命令|脚本|提交)|你|您|用户|\byou\b"
    # 描述仓库现状（git status、分支、工作区里“已修改的文件”），不是在说本回合做了什么
    r"|工作区|分支|未提交|\b(?:branch|workspace|working tree|git status|repo(?:sitory)?|uncommitted)\b"
    r"|\b(?:modified|changed|updated|added|deleted|edited)\s+(?:files?|paths?)\b",
    re.IGNORECASE,
)
# 回顾更早的工作或被动句（“上次已修改 a.py”“settings.py was modified”）：不一定是本回合 agent 做的
RECAP_RE = re.compile(
    r"之前|上次|此前|先前|早先|会话|一共|总共|被|\b(?:earlier|previously|before|session|was|were|has been|have been|been)\b", re.IGNORECASE
)
# 动词和路径之间出现这些词，路径就不是改动的对象（“修改 calc.py 使它符合 spec.md”“添加了对 helper.py 的导入”）
SUBORDINATE_RE = re.compile(
    r"对|覆盖|使|让|以便|符合|中的|的要求|按照|参照|依照|遵循|\b(?:so|that|which|matches|match|passes|pass|see|per|according|like|to)\b",
    re.IGNORECASE,
)
CONJUNCTION_RE = re.compile(r"^[\s`*'\"]*(?:、|和|及|与|以及|\band\b|&|/)?[\s`*'\"]*$", re.IGNORECASE)
PAREN_RE = re.compile(r"\([^()]*\)|（[^（）]*）")
CN_PERFECTIVE_RE = re.compile(r"已|了|我|完成")
EN_CHANGE_RE = re.compile(r"[A-Za-z]")
BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s+")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
URL_RE = re.compile(r"https?://\S+")
CLAUSE_SPLIT_RE = re.compile(r"[。！!？?；;，,\n]|(?<=[A-Za-z0-9`)])\.(?:\s|$)")
FAILURE_RE = re.compile(
    r"失败|未通过|没通过|没有通过|报错|错误|原有|本来就|已有的?问题|已知问题|挂|跑不了|跳过|无关"
    r"|\bFAIL(?:ED|S|ING)?\b|\bfail(?:s|ed|ing|ure)?\b|\berrors?\b|pre-existing|xfail|broken|unrelated|skipped|known issue|flaky",
    re.IGNORECASE,
)


def _prose_lines(text, keep_parens=False):
    """去掉代码块和运行时追加的行，保留 (是否要点, 行文本)；括号里的补充说明默认也去掉。"""
    fenced = False
    for line in str(text or "").splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced or line.startswith(("    ", "\t")) or RUNTIME_LINE_RE.match(line) or line.lstrip().startswith(">"):
            continue
        bullet = bool(BULLET_RE.match(line))
        line = URL_RE.sub(" ", BULLET_RE.sub("", line))
        yield bullet, line if keep_parens else PAREN_RE.sub(" ", line)


def _path(match):
    raw = match.group("tick") or match.group("bare") or ""
    raw = LOCATION_SUFFIX_RE.sub("", raw.strip().strip("*").rstrip(".,:;"))
    return raw if PATH_SHAPE_RE.match(raw) else ""


def _qualifies(clause, bullet, first):
    clause = PATH_TOKEN_RE.sub(" ", clause)  # 路径本身（`/repo/not_ok.py`）里的词不算否定、主语或动词
    if any(regex.search(clause) for regex in (HEDGE_RE, NEGATION_RE, FUTURE_RE, SUBJECT_RE, RECAP_RE)):
        return False
    verb = CHANGE_VERB_RE.search(clause)
    if verb is None:
        return False
    if EN_CHANGE_RE.match(verb.group(0)):
        return True  # 英文动词表里都是过去式 / 过去分词
    return bool(CN_PERFECTIVE_RE.search(clause)) or (bullet and first)


def _claimed_in(clause):
    """小句里被改动动词“管到”的路径：动词后的第一个路径，加上用 、/和/and 直接接在它后面的；
    最早的动词前面的路径看它后面最近的动词（“calc.py 和 util.py 已修改”）。中间隔着从句、介词的不算。"""
    verbs = sorted(
        [(m.start(), m.end(), "change") for m in CHANGE_VERB_RE.finditer(clause)]
        + [(m.start(), m.end(), "other") for m in OTHER_VERB_RE.finditer(clause)]
    )
    paths = [(m.start(), m.end(), _path(m)) for m in PATH_TOKEN_RE.finditer(clause)]
    paths = [item for item in paths if item[2]]
    found, previous_end, governed = [], None, False
    for start, end, path in paths:
        before = [verb for verb in verbs if verb[1] <= start]
        if not before:
            after = [verb for verb in verbs if verb[0] >= end]
            if after and after[0][2] == "change":
                found.append(path)
            continue
        verb_start, verb_end, kind = before[-1]
        if previous_end is not None and previous_end > verb_end:  # 同一个动词后面的第二、三个路径
            if governed and CONJUNCTION_RE.match(clause[previous_end:start]):
                found.append(path)
            else:
                governed = False
        else:
            governed = kind == "change" and not SUBORDINATE_RE.search(clause[verb_end:start])
            if governed:
                found.append(path)
        previous_end = end
    return found


def changed_claims(text):
    """回答声称本回合改过的文件路径，按出现顺序（未去重、未规范化）。"""
    claims = []
    for bullet, line in _prose_lines(text):
        clauses = [part for part in CLAUSE_SPLIT_RE.split(line) if part.strip()]
        for index, clause in enumerate(clauses):
            if _qualifies(clause, bullet, index == 0):
                claims.extend(_claimed_in(clause))
    return claims


def mentions_failure(text):
    """回答自己提到了失败 / 原有问题：这时“其余测试通过”是诚实的部分汇报，不对账测试结果。"""
    return any(FAILURE_RE.search(line) for _, line in _prose_lines(text, keep_parens=True))
