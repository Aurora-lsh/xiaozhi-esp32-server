"""Conservative fast path: complete story requests, never mixed device commands."""
import re
from .stories import plain_text


def story_only_request(query):
    clauses = [part.strip() for part in re.split(r"[，,。！？!?；;\n]", plain_text(query)) if part.strip()]
    requested = False
    for clause in clauses:
        if clause in {"小智", "海绵宝宝"}:
            continue
        if re.fullmatch(r"(?:请|能不能|可以)?(?:给我|帮我)?(?:讲|编|说)(?:一)?个.{0,100}(?:故事|日常)(?:吧|好吗)?", clause):
            requested = True
        elif re.fullmatch(r"(?:继续|接着).{0,100}(?:故事|日常|讲[零一二三四五六七八九十百0-9]+字)|(?:后来|然后|接下来)呢", clause):
            requested = True
        elif re.fullmatch(r"(?:大约|大概|约|用)?[零一二三四五六七八九十百0-9]+(?:字|句话)(?:左右|就好)?", clause):
            continue
        elif clause.startswith("故事里") or clause == "沿用人物、物品和地点":
            continue
        else:
            return False
    return requested
