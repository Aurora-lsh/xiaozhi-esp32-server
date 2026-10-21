"""Install user-confirmed demo markers, preserving unrelated local settings."""
from pathlib import Path
import argparse
import re
import yaml

parser = argparse.ArgumentParser()
parser.add_argument("--container", choices=("容器", "瓶子", "易拉罐"), default="容器")
container = parser.parse_args().container
path = Path(__file__).resolve().parents[1] / "data/.config.yaml"
text = path.read_text(encoding="utf-8-sig")
config = yaml.safe_load(text)
backup = path.with_name(".config.before-medicine-navigation.yaml")
if not backup.exists():
    backup.write_bytes(path.read_bytes())
options = {
    "enabled": True,
    "identity_mode": "marker",
    "targets": {
        "感冒药": "蓝色" + container,
        "发烧药": "绿色" + container,
        "退烧药": "绿色" + container,
        "头痛药": "橙色" + container,
        "头疼药": "橙色" + container,
    },
    "max_observations": 12,
    "stop_height_ratio": 0.45,
}
block = yaml.safe_dump({"medicine_navigation": options}, allow_unicode=True, sort_keys=False)
pattern = r"(?m)^medicine_navigation:[^\n]*(?:\n(?:[ \t]+[^\n]*|[ \t]*|#[^\n]*))*"
if "medicine_navigation" in config:
    text, count = re.subn(pattern, lambda _: block.rstrip(), text, count=1)
    assert count == 1, "Inspect local config before replacing navigation settings"
else:
    text = text.rstrip() + "\n\n" + block
updated = yaml.safe_load(text)
expected = dict(config, medicine_navigation=options)
assert updated == expected, "Unrelated settings must remain unchanged"
path.write_text(text, encoding="utf-8")
print("Installed 3 user-confirmed colors and 2 name aliases; unrelated settings preserved.")
