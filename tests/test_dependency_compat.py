from pathlib import Path


def test_python39_pins_greenlet_to_a_windows_wheel_release():
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert 'greenlet==3.2.4; python_version == "3.9"' in requirements

