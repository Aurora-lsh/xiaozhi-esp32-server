"""Add the NAV1 camera prompt contract without modifying camera firmware.

Run with the path to the existing camera_mcp.py. Existing ordinary camera
requests retain their current prompts. The source is backed up before editing.
"""
from pathlib import Path
import ast
import sys


OLD = "def _vision_prompt(question):\n    question = question.strip()\n"
NEW = '''def _vision_prompt(question):
    question = question.strip()
    if question.startswith("NAV1 "):
        prompt = question[5:]
        if not prompt.startswith("Find ") or len(quote(prompt, safe="")) > 255:
            raise ValueError("寻药定位提示词无效或超过固件长度限制")
        return prompt
'''


def patch_source(source):
    if NEW in source:
        return source
    if source.count(OLD) != 1:
        raise ValueError("Camera source changed; inspect _vision_prompt before patching")
    result = source.replace(OLD, NEW)
    ast.parse(result)
    return result


if __name__ == "__main__":
    path = Path(sys.argv[1]).resolve()
    original = path.read_text(encoding="utf-8")
    updated = patch_source(original)
    if updated != original:
        backup = path.with_name("camera_mcp.before-medicine-navigation.py.bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        path.write_text(updated, encoding="utf-8")
    print("Camera NAV1 prompt support is ready; restart the camera MCP child to load it.")
