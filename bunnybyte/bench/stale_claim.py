"""回答把本回合自己做的事说成“之前的会话已经做完了”：全新临时仓库里不可能有之前的会话，出现即误认。

只看回答开头附近：这类误认几乎都在第一句（“I can see from the transcript that this task has already
been completed in a prior session”、“根据转录记录，这个任务已经完成了”）。正常回答里的“之前的实现用了 //”
“the last run of pytest passed”“已完成修复”不算。
"""

import re

HEAD_CHARS = 400
STALE_RE = re.compile(
    r"\b(?:prior|previous|earlier)\s+(?:session|turn|conversation|context|interaction)\b"
    r"|\bfrom\s+the\s+(?:transcript|conversation\s+history)\b"
    r"|\bthe\s+transcript\s+(?:shows|indicates)\b"
    r"|\b(?:has|have|had)\s+already\s+been\s+(?:completed|done|finished|fixed|implemented|applied|resolved)\b"
    r"|\bI(?:'ve|\s+have)\s+already\s+(?:completed|finished|done|fixed|implemented)\b"
    r"|\b(?:task|work|fix|change|refactoring)s?\s+(?:is|are|was)\s+already\s+(?:complete|completed|done|finished)\b"
    r"|根据(?:之前的?)?(?:转录|会话)(?:记录)?"
    r"|(?:之前|此前|先前|上一轮|上一次|上次)的?(?:任务|会话|回合|对话|轮次)"
    r"|(?:之前|此前|先前|上一轮|上一次|上次)(?:就|已经|已)+(?:完成|修复|实现|改好|做完)",
    re.IGNORECASE,
)


def stale_claim(answer):
    """回答开头把本回合的工作说成之前做完的，返回命中的那段文字，否则返回空串。"""
    match = STALE_RE.search(str(answer or "")[:HEAD_CHARS])
    return match.group(0) if match else ""
