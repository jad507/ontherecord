"""The runner's promises, checked with model-free stages: reuse exactly when nothing that matters changed, record
every setting, stop an item at a failed check, and leave nothing half-finished behind a cancel."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from ontherecord.config import MachineConfig, load_config
from ontherecord.recipe import RecipeError, recipe_from_text
from ontherecord.runner import Runner

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def test_stages(monkeypatch):
    monkeypatch.setenv("ONTHERECORD_STAGES", "upper=stages_for_tests:Upper;count=stages_for_tests:Count;"
                                             "slow=stages_for_tests:Slow;pair=stages_for_tests:Pair")
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    monkeypatch.syspath_prepend(str(HERE))


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "prompt.txt").write_text("count it", encoding="utf-8")
    return tmp_path


def recipe(ws: Path, stages: str):
    return recipe_from_text(f"name: t\nsource:\n  files: [src.txt]\nstages:\n{stages}", ws)


def run(ws: Path, stages: str) -> dict:
    r = Runner(recipe(ws, stages), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    return r.execute()


def statuses(rec: dict) -> dict:
    return {s: c["status"] for s, c in rec["cells"]["src"].items()}


BASE = "  - download: {}\n  - upper: {}\n  - count: {}\n"


def test_first_run_runs_everything_and_writes_receipts(ws):
    rec = run(ws, BASE)
    assert rec["status"] == "done"
    assert statuses(rec) == {"download": "ran", "upper": "ran", "count": "ran"}
    receipt = json.loads((ws / "workspace" / rec["cells"]["src"]["upper"]["receipt"]).read_text())
    assert receipt["settings"] == {"suffix": "", "case": "upper", "fail": False, "note": ""}  # defaults recorded
    assert receipt["environment"]["python"]
    count = json.loads((ws / "workspace" / rec["cells"]["src"]["count"]["receipt"]).read_text())
    assert count["settings"]["prompt"]["text"] == "count it"


def test_second_run_reuses_everything(ws):
    run(ws, BASE)
    assert statuses(run(ws, BASE)) == {"download": "cached", "upper": "cached", "count": "cached"}


def test_changed_setting_reruns_that_stage_and_after(ws):
    run(ws, BASE)
    rec = run(ws, "  - download: {}\n  - upper: {suffix: '!'}\n  - count: {}\n")
    assert statuses(rec) == {"download": "cached", "upper": "ran", "count": "ran"}


def test_setting_outside_the_key_reuses(ws):
    run(ws, BASE)
    rec = run(ws, "  - download: {}\n  - upper: {note: hi}\n  - count: {}\n")
    assert statuses(rec)["upper"] == "cached"


def test_identical_output_lets_later_stages_be_reused(ws):
    (ws / "src.txt").write_text("ALREADY UPPER", encoding="utf-8")
    run(ws, BASE)
    rec = run(ws, "  - download: {}\n  - upper: {case: keep}\n  - count: {}\n")
    assert statuses(rec) == {"download": "cached", "upper": "ran", "count": "cached"}


def test_changed_source_bytes_rerun_everything(ws):
    run(ws, BASE)
    (ws / "src.txt").write_text("hello there", encoding="utf-8")
    assert statuses(run(ws, BASE)) == {"download": "ran", "upper": "ran", "count": "ran"}


def test_changed_prompt_file_reruns_its_stage(ws):
    run(ws, BASE)
    (ws / "prompt.txt").write_text("count it, v2", encoding="utf-8")
    assert statuses(run(ws, BASE))["count"] == "ran"


def test_failed_check_blocks_later_stages(ws):
    rec = run(ws, "  - download: {}\n  - upper: {fail: true}\n  - count: {}\n")
    assert statuses(rec) == {"download": "ran", "upper": "ran", "count": "blocked"}
    rec = run(ws, "  - download: {}\n  - upper: {fail: true}\n    on_fail: continue\n  - count: {}\n")
    assert statuses(rec) == {"download": "cached", "upper": "cached", "count": "ran"}


def test_plan_matches_what_runs(ws):
    r = Runner(recipe(ws, BASE), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    assert {s: c["status"] for s, c in r.plan()["src"].items()} == \
        {"download": "run", "upper": "run-if-changed", "count": "run-if-changed"}
    r.execute()
    r2 = Runner(recipe(ws, "  - download: {}\n  - upper: {suffix: x}\n  - count: {}\n"),
                MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    assert {s: c["status"] for s, c in r2.plan()["src"].items()} == \
        {"download": "cached", "upper": "run", "count": "run-if-changed"}


def test_cancel_leaves_no_finished_looking_entry(ws):
    r = Runner(recipe(ws, "  - download: {}\n  - upper: {}\n  - slow: {seconds: 60}\n"),
               MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    t = threading.Thread(target=r.execute)
    t.start()
    store = ws / "workspace" / "store" / "slow"
    deadline = time.time() + 30
    while time.time() < deadline and not list(store.glob("*/*/started")):
        time.sleep(0.2)
    r.cancel()
    t.join(30)
    assert not t.is_alive()
    assert r.record["status"] == "cancelled"
    assert r.record["cells"]["src"]["slow"]["status"] == "cancelled"
    assert not list(store.glob("*/*/receipt.json"))
    assert not list(store.glob("*/*.partial-*")), "partial output left behind"


@pytest.mark.parametrize("stages, msg", [
    ("  - upper: {}\n", "needs"),
    ("  - download: {}\n  - upper: {colour: red}\n", "unknown setting"),
    ("  - download: {}\n  - upper: {case: title}\n", "choose one of"),
    ("  - nosuchstage: {}\n", "exactly one stage"),
])
def test_recipe_errors(ws, stages, msg):
    with pytest.raises(RecipeError, match=msg):
        recipe(ws, stages)


# ---------------------------------------------------------------- wiring: as / from, and the same_source check
MIXED = ("  - download: {}\n  - upper: {case: lower}\n    as: lower\n  - upper: {}\n"
         "  - count: {}\n    from: {text: lower}\n  - pair: {}\n")


def test_from_wires_an_earlier_named_stage(ws):
    rec = run(ws, MIXED.replace("  - pair: {}\n", ""))
    assert statuses(rec) == {"download": "ran", "lower": "ran", "upper": "ran", "count": "ran"}
    count = json.loads((ws / "workspace" / rec["cells"]["src"]["count"]["receipt"]).read_text())
    assert count["inputs"]["text"]["from_step"] == "lower"
    lower = json.loads((ws / "workspace" / rec["cells"]["src"]["lower"]["receipt"]).read_text())
    assert count["inputs"]["text"]["sha256"] == lower["outputs"]["text"]["sha256"]


def test_inputs_from_different_files_of_one_artifact_are_blocked(ws):
    r = recipe(ws, MIXED)
    assert r.stages[-1].mixed == {"text": ["lower", "upper"]}  # pair: count came from lower, text from upper
    rec = Runner(r, MachineConfig(workspace=ws / "workspace"), log=lambda *_: None).execute()
    cell = rec["cells"]["src"]["pair"]
    assert cell["status"] == "blocked" and cell["checks"] == "fail"
    assert cell["run_checks"][0]["name"] == "same_source"
    assert set(cell["run_checks"][0]["value"]["text"].values()) == {"lower", "upper"}


def test_on_fail_continue_runs_a_mixed_stage_and_records_the_check(ws):
    rec = run(ws, MIXED.replace("  - pair: {}\n", "  - pair: {}\n    on_fail: continue\n"))
    cell = rec["cells"]["src"]["pair"]
    assert cell["status"] == "ran" and cell["checks"] == "fail"
    assert cell["run_checks"][0]["status"] == "fail"


def test_two_steps_that_make_identical_bytes_are_not_mixed(ws):
    # 'note' is not in the key, so both upper steps share one store entry: the same text, not a mix
    rec = run(ws, MIXED.replace("{case: lower}", "{note: twin}").replace("as: lower", "as: twin")
              .replace("{text: lower}", "{text: twin}"))
    assert rec["cells"]["src"]["pair"]["status"] == "ran"
    assert "run_checks" not in rec["cells"]["src"]["pair"]
    assert rec["cells"]["src"]["upper"]["status"] == "cached"  # the second step reused the first one's output


@pytest.mark.parametrize("stages, msg", [
    ("  - download: {}\n  - upper: {}\n  - upper: {}\n", "two stages are named 'upper'"),
    ("  - download: {}\n  - upper: {}\n    as: count\n", "name of another stage type"),
    ("  - download: {}\n  - upper: {}\n  - count: {}\n    from: {text: nowhere}\n", "not an earlier stage"),
    ("  - download: {}\n  - upper: {}\n  - count: {}\n    from: {media: download}\n", "maps the inputs it needs"),
])
def test_wiring_errors(ws, stages, msg):
    with pytest.raises(RecipeError, match=msg):
        recipe(ws, stages)


def test_malformed_yaml_is_a_recipe_error(ws):
    with pytest.raises(RecipeError, match="not valid YAML"):
        recipe_from_text("name: x\nsource:\n  urls: [https://youtube.com/watch?v=abc, x]\n", ws)
    with pytest.raises(RecipeError, match="key: value"):
        recipe_from_text("- just a list\n", ws)


def test_stages_run_offline_unless_the_machine_says_otherwise(tmp_path):
    assert MachineConfig().stage_env == {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                                         "PYANNOTE_METRICS_ENABLED": "0"}
    f = tmp_path / "machine.yaml"
    f.write_text("stage_env:\n  HF_HUB_OFFLINE: 0\n", encoding="utf-8")
    env = load_config(f).stage_env
    assert env["HF_HUB_OFFLINE"] == "0" and env["PYANNOTE_METRICS_ENABLED"] == "0"
