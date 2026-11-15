"""Can a text model read stress it cannot hear: StressTest's meaning question (SSR), asked of a transcript with the
stressed words marked. Ported from AITranscribe/cascade_stresstest.py (CAPITALS prompt) and paper_prompt_check.py
(the paper's wording); answer parsing from reproduce_stresstest_gemma.py."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ..llm import close_model, leaves_machine_check, open_model, remote_host
from ..usage import llm_metrics
from .base import Check, Context, Item, Result, Setting, Stage

CAPITALS = ('The speaker said: "{sentence}"\n{note}'
            "Out of the following answers, according to the speaker's stressed words, "
            "what is most likely the underlying intention of the speaker?\n"
            "1. {answer_1}\n2. {answer_2}\nAnswer:")
NOTE = "Words in CAPITAL LETTERS were stressed by the speaker.\n"
PAPER = ('Question: Given that a speaker said: "{sentence}", and stressed the words: {words}. '
         "Out of the following answers, what is most likely the underlying intention of the speaker?\n"
         "Possible answers:\n1. {answer_1}\n2. {answer_2}\nAnswer:")
# The StressTest authors' judge for answers no rule can read, verbatim (its spelling included).
JUDGE = '''You are a Speech-LM evaluator that helps evaluating models that have trouble in outputting a correct schema for an answer.
You are very good at outputting the correct schema according to the instructions.

INSTRUCTIONS:
Given a prompt with a question and possible answers that the Speech-LM received, and the output the model emmited, you are required to output the Speech-LM answer in a fixed format.
* The output should be aligned with the Speech-LM output, and should not include any additional information or context.
* The output should be a JSON object with a single key "answer" and a value that is the number of the correct answer according to the output of the Speech-LM.
* The answer should be an integer, either 1 or 2.

INPUT TO Speech-LM:
{input_prompt}
OUTPUT FROM Speech-LM:
{output}
YOUR EXPECTED JSON OUTPUT:'''
SAMPLING = {"google": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},  # Gemma's recommended settings
            "greedy": {"temperature": 0.0}}


def capitals(transcription: str, stress: list[int]) -> str:
    """Lower-cased, with only the stressed words in CAPITALS (so a stressed "I" reads "I" and another "i")."""
    words = transcription.lower().split()
    return " ".join(w.upper() if i < len(stress) and stress[i] else w for i, w in enumerate(words))


def quoted(transcription: str, stress: list[int]) -> str:
    words = transcription.split()
    picked = [re.sub(r"[^\w'-]", "", w) for i, w in enumerate(words) if i < len(stress) and stress[i]]
    return ", ".join(f'"{w}"' for w in picked if w) or "none"


def parse(text: str, answers: list[str]) -> tuple[int | None, str]:
    text = text.strip()
    m = re.search(r"\*\*\s*([12])[.)]", text)  # "The correct answer is **1. ...**"
    if m:
        return int(m[1]), "rule_bold_option"
    m = re.match(r"^\W*(?:answer\W*)?(?:option\W*)?([12])\b", text, re.I)
    if m:
        return int(m[1]), "rule_leading_digit"
    digits = set(re.findall(r"(?<![\w.])([12])(?![\w])", text))
    if len(digits) == 1:
        return int(digits.pop()), "rule_single_digit"
    hits = [i + 1 for i, a in enumerate(answers) if a.strip().rstrip(".").lower() in text.lower()]
    if len(hits) == 1:
        return hits[0], "rule_answer_text"
    return None, ""


def wald(k: int, n: int) -> list[float]:
    p = k / n
    h = 1.96 * math.sqrt(p * (1 - p) / n)
    return [round(max(0.0, p - h), 3), round(min(1.0, p + h), 3)]


def marks_for(choice: str, item: dict, m: dict | None) -> list[int]:
    if choice == "none":
        return []
    if choice == "labels":
        return item["stress_binary"]
    if choice == "whistress_top":
        top = m["heard"]["top"]
        return [] if top is None else [int(i == top) for i in range(len(item["stress_binary"]))]
    return m[choice.removeprefix("whistress_")]["binary"]


class StressRead(Stage):
    """Ask a text model which of two meanings the speaker intended, showing it only the transcript with the
    stressed words marked: by WhiStress, by the set's own labels, or not at all. This is the second half of the
    "a detector marks the transcript, a text model reads it" pipeline; 'none' measures what the words alone give
    away (each sentence appears with both meanings, so about 50%)."""
    name = "stress_read"
    label = "Read the stress marks (LLM)"
    version = "1"
    gpu = True
    needs = {"stress_set": "other", "stress_marks": "other"}
    makes = {"stress_answers": "other"}
    headline = ("accuracy", "of meanings right")
    settings = {
        "marks": Setting("whistress_given", "Which words are marked: WhiStress given the true transcript, WhiStress "
                         "from the audio alone, only WhiStress's single most stressed word, the set's own labels, "
                         "or none.", choices=["whistress_given", "whistress_heard", "whistress_top", "labels",
                                              "none"]),
        "prompt": Setting("capitals", "'capitals': the transcript with stressed words in CAPITALS (the ICDS "
                          "poster's); 'paper': the StressTest paper's 'and stressed the words: ...'.",
                          choices=["capitals", "paper"]),
        "endpoint": Setting("managed", "'managed' starts llama-server with `model` and stops it afterwards; or "
                            "the URL of an OpenAI-compatible server already running."),
        "model": Setting("gemma-4-26B-A4B-it-MXFP4_MOE.gguf", "GGUF file for a managed server (relative to the "
                         "machine config's models_root)."),
        "context": Setting(8192, "Context window in tokens."),
        "offload": Setting("-ngl 999 --n-cpu-moe 5; -ngl 999 --n-cpu-moe 8; -ngl 999 --n-cpu-moe 14",
                           "llama-server offload arguments, tried in order until the model loads (';'-separated).",
                           in_key=False),
        "sampling": Setting("google", "'google': temperature 1.0, top_p 0.95, top_k 64 (how the poster's readers "
                            "ran); 'greedy': temperature 0.", choices=list(SAMPLING)),
        "thinking": Setting(False, "Let the model reason before answering (the paper's GPT-4o did not)."),
        "seed": Setting(42, "Server seed; each question is asked with its own item's seed."),
        "max_tokens": Setting(512, "Answer length cap."),
    }

    def setup(self, settings: dict, ctx: Context):
        return open_model(settings, ctx, settings["context"])

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        from .stress_set import read_set
        llm, sampling = state["llm"], SAMPLING[settings["sampling"]]
        manifest, _ = read_set(inputs["stress_set"])
        marks = {}
        for line in inputs["stress_marks"].read_text(encoding="utf-8").splitlines():
            m = json.loads(line)
            marks[m["id"]] = m
        rows, calls = [], []
        for n, c in enumerate(manifest["items"], 1):
            stress = marks_for(settings["marks"], c, marks.get(c["id"]))
            a1, a2 = c["answers"]
            if settings["prompt"] == "capitals":
                prompt = CAPITALS.format(sentence=capitals(c["transcription"], stress),
                                         note="" if settings["marks"] == "none" else NOTE, answer_1=a1, answer_2=a2)
            else:
                prompt = PAPER.format(sentence=c["transcription"], words=quoted(c["transcription"], stress),
                                      answer_1=a1, answer_2=a2)
            r = llm.chat([{"role": "user", "content": prompt}], max_tokens=settings["max_tokens"], seed=c["seed"],
                         **sampling)
            calls.append(r)
            text = r["raw_text"]  # an answer cut off at max_tokens still counts if it named an option
            pred, how = parse(text, c["answers"])
            if pred is None:
                j = llm.chat([{"role": "user", "content": JUDGE.format(input_prompt=prompt, output=text)}],
                             max_tokens=256, seed=c["seed"], **sampling)
                calls.append(j)
                hit = re.search(r"\{.*\}", j["raw_text"], re.S)
                try:
                    ans = json.loads(hit.group(0))["answer"] if hit else None
                except (json.JSONDecodeError, KeyError, TypeError):
                    ans = None
                pred, how = (int(ans), "judge") if ans in (1, 2, "1", "2") else (None, "unparsed")
            rows.append({"id": c["id"], "marked": stress, "prompt": prompt, "answer": text,
                         "finish_reason": r["finish_reason"], "pred": pred, "label": c["label"] + 1,
                         "correct": pred == c["label"] + 1, "parse": how, "blind": c.get("blind", False)})
            if n % 50 == 0:
                ctx.log(f"  {n}/{len(manifest['items'])}: {sum(x['correct'] for x in rows)} right so far")
        (out / "answers.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                                           encoding="utf-8")
        k, total = sum(x["correct"] for x in rows), len(rows)
        how: dict[str, int] = {}
        for x in rows:
            how[x["parse"]] = how.get(x["parse"], 0) + 1
        metrics = {"items": total, "correct": k, "accuracy": round(k / total, 3), "wald95": wald(k, total),
                   "parse": how, **llm_metrics(calls)}
        blind = [x for x in rows if x["blind"]]
        if blind:
            kb = sum(x["correct"] for x in blind)
            metrics.update(blind_items=len(blind), blind_accuracy=round(kb / len(blind), 3),
                           blind_wald95=wald(kb, len(blind)))
        lo, hi = metrics["wald95"]
        checks = [Check("accuracy", "ok", metrics["accuracy"], f"{k / total:.0%} right ({lo:.0%}-{hi:.0%}); "
                        + ("above" if lo > 0.5 else "below" if hi < 0.5 else "not distinguishable from")
                        + " chance (50%)")]
        if how.get("unparsed"):
            checks.append(Check("unparsed", "warn", how["unparsed"], f"{how['unparsed']} answers named neither "
                                "option; counted as wrong"))
        checks += leaves_machine_check(remote_host(settings["endpoint"]))
        return Result(files={"stress_answers": "answers.jsonl"}, metrics=metrics, checks=checks, device="server",
                      tools=state["tools"])

    def teardown(self, state) -> None:
        close_model(state)
