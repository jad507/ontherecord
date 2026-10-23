"""First-run setup: which builds suit which machine, the ready-made transcript recipe, the token check, and the GUI's
setup and transcribe endpoints. No GPU, network or model is needed: the machines are described, not detected."""
from __future__ import annotations

import io
import urllib.error

import pytest

from ontherecord import bootstrap, hwdetect, presets
from ontherecord.config import MachineConfig
from ontherecord.recipe import recipe_from_text


def nvidia(name, mem, cc, cuda, driver="999.0"):
    return {"vendor": "nvidia", "name": name, "memory_gb": mem, "free_gb": mem, "compute_cap": cc, "cuda": cuda,
            "driver": driver}


def test_nvidia_smi_banners_old_and_new():
    assert hwdetect.parse_driver_cuda("| NVIDIA-SMI 570.86.15   Driver Version: 570.86.15   CUDA Version: 12.8 |") == (12, 8)
    assert hwdetect.parse_driver_cuda("| NVIDIA-SMI 615.71.09   KMD Version: 615.71.09   CUDA UMD Version: 13.4 |") == (13, 4)
    assert hwdetect.parse_driver_cuda("no driver here") is None
    gpus = hwdetect.parse_gpus("NVIDIA GeForce RTX 5070 Ti, 16303, 11338, 12.0, 615.71.09\nbad line\n", (13, 4))
    assert gpus == [{"vendor": "nvidia", "name": "NVIDIA GeForce RTX 5070 Ti", "memory_gb": 16, "free_gb": 4.8,
                     "compute_cap": "12.0", "driver": "615.71.09", "cuda": "13.4"}]
    assert hwdetect.parse_gpus("Old Card, 4096, 0, , 470.1", (11, 4))[0]["compute_cap"] is None


@pytest.mark.parametrize("system, gpus, torch, llama, advice", [
    # a GTX 1080: only PyTorch's CUDA 12.6 build has its kernels, and CUDA 13 dropped it
    ("linux", [nvidia("GTX 1080", 8, "6.1", "13.0")], "cu126", ["cuda-12", "vulkan", "cpu"], None),
    # an RTX 2060 on an older driver: CUDA 12.6 for PyTorch, Vulkan for llama.cpp (its Linux CUDA build is 12.8)
    ("linux", [nvidia("RTX 2060", 6, "7.5", "12.4")], "cu126", ["vulkan", "cpu"], "recommended"),
    ("windows", [nvidia("RTX 2060", 6, "7.5", "12.4")], "cu126", ["cuda-12", "vulkan", "cpu"], "recommended"),
    ("linux", [nvidia("RTX 4090", 24, "8.9", "12.8")], "cu128", ["cuda-12", "vulkan", "cpu"], "optional"),
    ("windows", [nvidia("RTX 5070 Ti", 16, "12.0", "13.4")], "cu128", ["cuda-13.4", "cuda-12", "vulkan", "cpu"], None),
    # an RTX 5090 whose driver predates Blackwell's CUDA 12.8: nothing CUDA runs until the driver is updated
    ("linux", [nvidia("RTX 5090", 32, "12.0", "12.6")], "cpu", ["vulkan", "cpu"], "needed"),
    ("linux", [nvidia("Old Quadro", 4, "5.2", "11.4")], "cpu", ["vulkan", "cpu"], "needed"),
    ("linux", [], "cpu", ["cpu"], None),
    ("linux", [{"vendor": "intel", "name": "Intel graphics"}], "cpu", ["cpu"], None),
    ("windows", [{"vendor": "amd", "name": "AMD Radeon RX 7800 XT"}], "cpu", ["vulkan", "cpu"], None),
])
def test_builds_for_each_machine(system, gpus, torch, llama, advice):
    assert hwdetect.torch_flavor(system, gpus)[0] == torch
    builds, why = hwdetect.llama_build(system, "x86_64", gpus)
    assert builds == llama and why
    assert (hwdetect.driver_advice(system, gpus) or {}).get("level") == advice


def test_every_build_has_pinned_archives():
    for system, builds in bootstrap.LLAMA_BUILDS.items():
        for build, assets in builds.items():
            assert all(a in bootstrap.LLAMA_ASSETS for a in assets), (system, build)
    for system in ("windows", "linux"):
        for arch in ("x86_64", "arm64"):
            for gpus in ([], [nvidia("RTX 5070 Ti", 16, "12.0", "13.4")], [nvidia("RTX 4090", 24, "8.9", "12.8")]):
                for build in hwdetect.llama_build(system, arch, gpus)[0]:
                    assert build in bootstrap.LLAMA_BUILDS[system], (system, arch, build)
    assert hwdetect.llama_build("macos", "arm64", [])[0] == ["macos-arm64"]


def test_a_build_that_cannot_see_the_card_falls_back(tmp_path, monkeypatch):
    hw = {"os": "linux", "arch": "x86_64"}
    for build in ("cuda-13.4", "cuda-12"):
        exe = tmp_path / f"llama-{bootstrap.LLAMA_BUILD}-{build}" / "bin" / "llama-server"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    seen = {"cuda-13.4": None, "cuda-12": "Available devices:\n  CUDA0: NVIDIA GeForce RTX 4090 (24000 MiB)\n"}
    monkeypatch.setattr(bootstrap, "llama_devices", lambda p: seen[p.parent.parent.name.split("-", 2)[2]])
    logs = []
    got = bootstrap.ensure_llama(tmp_path, hw, log=logs.append, builds=["cuda-13.4", "cuda-12", "vulkan"])
    assert got.parent.parent.name.endswith("cuda-12")
    assert "does not start here" in logs[0]


def machine(gpu=None, ram=16, os_name="linux"):
    return {"os": os_name, "arch": "x86_64", "ram_gb": ram, "gpu": gpu, "gpus": [gpu] if gpu else []}


def test_whisper_suits_the_card():
    fit = bootstrap.fit
    assert (fit(machine(nvidia("RTX 4090", 24, "8.9", "12.8")))["whisper_compute"]) == "float16"
    assert fit(machine(nvidia("GTX 1650", 4, "7.5", "12.8")))["whisper_compute"] == "int8_float16"
    assert fit(machine(nvidia("GTX 1080", 8, "6.1", "13.0")))["whisper_compute"] == "int8"
    f = fit(machine(None, ram=8))
    assert (f["whisper"], f["whisper_compute"]) == ("large-v3-turbo", "int8")
    assert bootstrap.recommended_models(f) == ["gemma-4-E2B", "gemma-4-E4B", "whisper-large-v3-turbo",
                                               "pyannote-community-1"]
    apple = {"vendor": "apple", "name": "Apple GPU", "memory_gb": 22}
    assert fit(machine(apple, ram=32, os_name="macos"))["whisper"] == "large-v3-turbo"  # CTranslate2 has no Metal


def test_transcript_recipe_writes_this_machines_settings(tmp_path):
    media = tmp_path / "Council meeting.mp4"
    media.write_bytes(b"\0")
    choice = bootstrap.fit(machine(nvidia("GTX 1650", 4, "7.5", "12.8")))
    text = presets.transcript_recipe([str(media), "https://www.youtube.com/watch?v=dQw4w9WgXcQ"], with_comments=True,
                                     language="", choice=choice)
    r = recipe_from_text(text, tmp_path)
    assert [s.name for s in r.stages] == ["download", "audio", "diarize", "transcribe", "align", "blocks",
                                          "extract_comments"]
    tr = next(s for s in r.stages if s.name == "transcribe")
    assert tr.run_settings["compute_type"] == "int8_float16" and tr.run_settings["language"] == ""
    ex = next(s for s in r.stages if s.name == "extract_comments")
    assert ex.run_settings["model"] == bootstrap.MODELS["gemma-4-E4B"]["file"] and ex.run_settings["offload"] == "-ngl 0"
    assert {i.id for i in r.items} == {"dQw4w9WgXcQ", "Council-meeting"}
    one = recipe_from_text(presets.transcript_recipe([str(media)], choice=choice), tmp_path)
    assert one.name == "transcript Council meeting"
    with pytest.raises(ValueError):
        presets.transcript_recipe([" "], choice=choice)


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_model_files_are_checked_against_the_pins(tmp_path, monkeypatch):
    from ontherecord import model_hashes
    m = bootstrap.MODELS["whistress"]
    snap = tmp_path / "hub" / "models--slprl--WhiStress" / "snapshots" / m["rev"]
    snap.mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    good = b"head weights"
    (snap / "classifier.pt").write_bytes(b"tampered")
    (snap / "additional_decoder_block.pt").write_bytes(good)
    (snap / "metadata.json").write_bytes(b"{}")
    import hashlib
    monkeypatch.setitem(model_hashes.FILES, "whistress", {
        "classifier.pt": "sha256:" + hashlib.sha256(good).hexdigest(),
        "additional_decoder_block.pt": "sha256:" + hashlib.sha256(good).hexdigest(),
        "metadata.json": "git:" + hashlib.sha1(b"blob 2\0{}").hexdigest(),
        "missing.json": "git:0"})
    problems = bootstrap.verify_model("whistress", tmp_path, delete=True)
    assert problems == ["classifier.pt does not match its pinned sha256", "missing.json is missing"]
    assert not (snap / "classifier.pt").exists() and (snap / "metadata.json").exists()  # only the bad file goes
    gguf = tmp_path / bootstrap.MODELS["gemma-4-E2B"]["file"]
    gguf.write_bytes(b"someone else's file")
    assert bootstrap.verify_model("gemma-4-E2B", tmp_path, delete=True) == [f"{gguf.name} is not the pinned file"]
    assert gguf.exists()  # reported, never deleted
    assert set(model_hashes.FILES) == {n for n, x in bootstrap.MODELS.items() if x["kind"] == "hf"}


def test_token_check(monkeypatch):
    def fake(answers):
        def urlopen(req, timeout=None):
            code = answers[req.full_url.split("/")[3]]
            if code != 200:
                raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)
            return Reply(b'{"name": "jeff"}')
        monkeypatch.setattr(bootstrap.urllib.request, "urlopen", urlopen)
    fake({"api": 401})
    assert bootstrap.check_hf_token("hf_x")["token"] is False
    fake({"api": 200, "pyannote": 403})
    r = bootstrap.check_hf_token("hf_x")
    assert r["token"] and not r["access"] and "accepted" in r["message"]
    fake({"api": 200, "pyannote": 200})
    assert bootstrap.check_hf_token("hf_x")["access"] is True


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from ontherecord.server import create_app
    monkeypatch.setattr(bootstrap, "hardware", lambda: machine(nvidia("RTX 4090", 24, "8.9", "12.8")))
    monkeypatch.delenv("HF_TOKEN", raising=False)  # so the token check below never goes online
    cfg = MachineConfig(workspace=tmp_path / "ws", models_root=tmp_path / "models", env_file=tmp_path / "secrets.env")
    return TestClient(create_app(cfg, dispatch=False), base_url="http://127.0.0.1:8765"), tmp_path


def test_transcribe_endpoint(client, monkeypatch):
    c, tmp = client
    (tmp / "talk.wav").write_bytes(b"\0")
    monkeypatch.setattr(presets, "missing_models", lambda cfg, comments=False, choice=None: ["whisper-large-v3"])
    r = c.post("/api/transcribe", json={"sources": [str(tmp / "talk.wav")]})
    assert r.status_code == 422 and "whisper-large-v3" in r.json()["detail"]
    monkeypatch.setattr(presets, "missing_models", lambda cfg, comments=False, choice=None: [])
    assert c.post("/api/transcribe", json={"sources": ["talk.wav"]}).status_code == 422  # not a whole path
    r = c.post("/api/transcribe", json={"sources": [str(tmp / "talk.wav")]})
    assert r.status_code == 200 and '"compute_type": "float16"' in r.json()["recipe"]
    assert c.get("/api/jobs").json()[0]["run_id"] == r.json()["run_id"]
    listing = c.get("/api/fs", params={"path": str(tmp), "files": True}).json()
    assert [f["name"] for f in listing["files"]] == ["talk.wav"]
    assert c.get("/api/fs", params={"path": str(tmp)}).json()["files"] == []


def test_setup_endpoint_describes_the_machine(client):
    c, _ = client
    d = c.get("/api/setup").json()
    assert d["accel"]["torch"]["want"] == "cu128" and d["accel"]["llama"]["want"] == "cuda-12"
    assert {f["key"] for f in d["folders"]} == {"workspace", "archive", "models", "settings"}
    assert [m["name"] for m in d["models"] if m["recommended"]] == ["gemma-4-E4B", "gemma-4-26B", "whisper-large-v3",
                                                                    "pyannote-community-1"]
    assert c.post("/api/setup/check-token", json={}).status_code == 422  # no token anywhere: nothing to ask


def test_only_requests_addressed_to_this_machine(client):
    """DNS rebinding: a page on attacker.example re-pointed at 127.0.0.1 sends Host: attacker.example."""
    c, tmp = client
    assert c.get("/api/fs", params={"path": str(tmp)}).status_code == 200
    assert c.get("http://localhost:8765/api/jobs").status_code == 200
    assert c.get("http://[::1]:8765/api/jobs").status_code == 200
    assert c.get("/api/fs", headers={"Host": "attacker.example:8765"}).status_code == 400
    assert c.get("/api/fs", headers={"Host": "127.0.0.1.attacker.example"}).status_code == 400
    body = {"token": "hf_aaaaaaaaaaaaaaaaaaaaaaaa"}
    assert c.post("/api/setup/token", json=body, headers={"Origin": "http://attacker.example"}).status_code == 403
    assert c.post("/api/setup/token", json=body, headers={"Origin": "http://127.0.0.1:8188"}).status_code == 403
    assert c.post("/api/setup/token", json=body, headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/setup/check-token", json={}, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 422
