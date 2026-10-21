"""One authorized identification photo through the camera bridge; no motion."""
from pathlib import Path
import json
import sys

server = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(server))
sys.path.insert(0, sys.argv[1])
from core.providers.tools.medicine_navigation import identification_prompt, identification_position
import camera_mcp
import yaml

options = yaml.safe_load((server / "data/.config.yaml").read_text(encoding="utf-8"))["medicine_navigation"]
target = options["targets"]["感冒药"]
prompt = identification_prompt(target, options.get("identity_mode", "marker"))
assert camera_mcp._vision_prompt(prompt) == prompt[5:]
before = camera_mcp._CLIENT.status()
assert not before.get("isProcessing"), "Camera is busy; no new photo requested"
payload = camera_mcp.look_at_camera(prompt)
report = {"camera_success": payload.get("success"), "description": payload.get("description"),
          "error": payload.get("error"), "target": target,
          "duration_ms": payload.get("duration_ms"), "motion_calls": 0,
          "authorized_recognition_requests": 1, "capture_tool_calls": 1}
try:
    report["position"] = identification_position(payload)
    report["protocol_valid"] = True
except (ValueError, TypeError) as exc:
    report["protocol_valid"] = False
    report["reason"] = str(exc)
output = server.parents[1] / ".runtime/medicine-navigation-20261020"
try:
    after = camera_mcp._CLIENT.status()
    report["request_count_before"] = before.get("requestCount")
    report["request_count_after"] = after.get("requestCount")
    if payload.get("success") is True and not after.get("isProcessing"):
        photo = camera_mcp._CLIENT.download_image()
        image_path = output / "live-identification-photo.jpg"
        image_path.write_bytes(photo)
        report["photo_path"] = str(image_path)
except Exception as exc:
    report["local_photo_error"] = type(exc).__name__
out = output / "live-camera.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
