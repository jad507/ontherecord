"""Word choices: for every word Whisper was unsure of, the few words it would have said instead, each with a time.

What the transcript checker (the Check page) offers on a key press. It reads a transcript Whisper already made
(align's, so each line keeps its speaker) and the audio, and does not transcribe again: the text being checked is
exactly the text that was made. For each stretch of lines up to 30 seconds long:

1. Forced alignment of the transcript's own tokens (faster-whisper's word timestamps, run on the given text):
   each word's start and end, and the probability Whisper gives each of its tokens.
2. For each word whose least likely letter-bearing token is below `doubt_below`, Whisper's next-token
   distribution at that token, given the audio and every word before it: the most likely other tokens at or above
   `min_p`.
3. Each such token completed greedily for a few words, which gives the whole word and says how many of the
   transcript's words it replaces: one ("their" for "there"), two ("gonna" for "going to"), none (a word Whisper
   may have invented: the choice is to delete it), or two words for one ("ice cream" for "I scream").

The probabilities are Whisper's own, conditional on the transcript's words before that point, so a choice's p is
"how likely this token was here instead". They are not calibrated against people; reviews can measure that.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage
from .transcribe import Transcribe

SR = 16000
WINDOW_S = 30.0
_SPANS = ((0, 1), (1, 1), (1, 2), (2, 1), (2, 2), (1, 3), (3, 1))  # (choice words, transcript words) replaced


def norm(text: str) -> str:
    """A word as compared: lower case, letters, digits and apostrophes only."""
    return " ".join(re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")))


def has_letters(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))


def windows(segments: list[dict], pad: float, limit: float = WINDOW_S) -> list[list[int]]:
    """Consecutive segment indices grouped so each group, padded, fits one Whisper window. A segment longer than
    the window gets one of its own (its words past the window keep the window's end as their time)."""
    out: list[list[int]] = []
    for i, s in enumerate(segments):
        if out and s["end"] + pad - (segments[out[-1][0]]["start"] - pad) <= limit:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def merge_words(words: list[dict]) -> list[dict]:
    """Whole words as the text spells them, so the words are exactly the transcript split at spaces: a piece that
    does not start with a space (punctuation, or the rest of "co-worker") joins the word before it, and a word of
    punctuation alone (an opening quote) joins the word after. Each dict has text, tokens (indices into the window's
    tokens), start, end."""
    out: list[dict] = []
    for w in words:
        if out and not w["text"][:1].isspace():
            out[-1]["text"] += w["text"]
            out[-1]["tokens"] += w["tokens"]
            out[-1]["end"] = max(out[-1]["end"], w["end"])
        elif out and not has_letters(out[-1]["text"]):
            prev = out.pop()
            out.append({**w, "text": prev["text"] + w["text"].lstrip(), "tokens": prev["tokens"] + w["tokens"],
                        "start": min(prev["start"], w["start"])})
        else:
            out.append(dict(w))
    return out


def span(choice_words: list[str], following: list[str], ended: bool) -> tuple[int, int]:
    """How many of the choice's words replace how many of the transcript's, found by where the two agree again:
    the first (a, b) in _SPANS with choice word a equal to transcript word b (both counted from the doubtful
    word). `following` is the transcript from the doubtful word on; `ended` means the completion stopped (end of
    text) after the choice's words. With no agreement in reach, one word for one."""
    c = [norm(w) for w in choice_words]
    t = [norm(w) for w in following]
    for a, b in _SPANS:
        if a < len(c) and b < len(t) and c[a] and c[a] == t[b]:
            return a, b
        if ended and a == len(c) and b == len(t):
            return a, b
    return 1, 1


class WordChoices(Stage):
    """Times and confidences for every word of a transcript, and for the doubtful ones, the words Whisper would
    have said instead (the Check page's choices). Runs on the transcript's own text; does not transcribe again.
    Use the same Whisper model as the transcript so the probabilities are the ones that made it."""
    name = "word_choices"
    label = "Word choices (for checking by hand)"
    version = "2"
    group = "audio"
    gpu = True
    needs = ("audio", "aligned")
    makes = ("words",)
    packages = ("faster-whisper", "ctranslate2")
    settings = {
        "model": Transcribe.settings["model"],
        "compute_type": Transcribe.settings["compute_type"],
        "device": Transcribe.settings["device"],
        "language": Setting("en", "Language code of the transcript."),
        "doubt_below": Setting(0.8, "A word is doubtful when Whisper gave one of its tokens less than this "
                                    "probability; only doubtful words get choices."),
        "min_p": Setting(0.03, "A choice must have at least this probability where the doubtful token was."),
        "choices": Setting(4, "At most this many choices per doubtful word."),
        "pad_s": Setting(0.2, "Seconds of audio kept either side of each stretch of lines."),
        "follow_tokens": Setting(8, "Tokens each choice is completed with, to find how many words it replaces."),
        "batch": Setting(0, "Prompts decoded together; 0 picks from the card's free memory (each prompt holds "
                            "Whisper's cross-attention for the audio, about 245 MB for large-v3), at most 16.",
                         in_key=False),
    }

    def setup(self, settings: dict, ctx: Context):
        state = Transcribe.setup(self, settings, ctx)
        state["batch"] = int(settings["batch"]) or _auto_batch(state["device"])
        ctx.log(f"decoding {state['batch']} prompts at a time")
        from faster_whisper.tokenizer import Tokenizer
        m = state["model"]
        state["tok"] = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe",
                                 language=settings["language"] or "en")
        return state

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        from faster_whisper.audio import decode_audio
        aligned = json.loads(inputs["aligned"].read_text(encoding="utf-8"))
        segs = [s for s in aligned["segments"] if s.get("text", "").strip()]
        audio = decode_audio(str(inputs["audio"]), sampling_rate=SR)
        t0 = time.time()
        timing = {"encode_align_s": 0.0, "line_encode_s": 0.0, "distributions_s": 0.0, "completions_s": 0.0}
        result_segs, doubtful, offered = [], 0, 0
        groups = windows(segs, settings["pad_s"])
        for n, g in enumerate(groups):
            words_by_seg, d, o = self._window(audio, [segs[i] for i in g], settings, state, timing)
            doubtful += d
            offered += o
            for i, ws in zip(g, words_by_seg):
                s = segs[i]
                result_segs.append({"start": s["start"], "end": s["end"], "speaker": s.get("speaker", ""),
                                    "text": s["text"].strip(), "words": ws})
            if n and n % 25 == 0:
                ctx.log(f"{item.id}: {n}/{len(groups)} stretches, {offered} words with choices")
        (out / "words.json").write_text(json.dumps({"model": settings["model"], "segments": result_segs},
                                                   indent=1, ensure_ascii=False), encoding="utf-8")
        total = sum(len(s["words"]) for s in result_segs)
        timed = sum(1 for s in result_segs for w in s["words"] if w["e"] > w["s"])
        share = timed / total if total else 0.0
        return Result(
            files={"words": "words.json"}, device=state["device"],
            checks=[Check("timed_words", "warn" if share < 0.9 else "ok", round(share, 3),
                          f"{timed:,} of {total:,} words have a time span")],
            metrics={"words": total, "doubtful": doubtful, "with_choices": offered, "stretches": len(groups),
                     "duration_s": round(len(audio) / SR, 1), "seconds": round(time.time() - t0, 1),
                     **{k: round(v, 1) for k, v in timing.items()}, "compute_type_used": state["compute"]},
            tools={"model": settings["model"], "model_revision": state["revision"]})

    # --------------------------------------------------------------------------------------------- one window
    def _window(self, audio, segs: list[dict], st: dict, state: dict, timing: dict):
        import numpy as np
        from faster_whisper.transcribe import get_ctranslate2_storage
        m, tok = state["model"], state["tok"]
        pad = st["pad_s"]
        w0 = max(0.0, segs[0]["start"] - pad)
        a, b = int(w0 * SR), int(min(len(audio) / SR, segs[-1]["end"] + pad) * SR)
        b = min(b, a + int(WINDOW_S * SR))
        t = time.time()
        feats = m.feature_extractor(audio[a:b])
        frames = min(feats.shape[-1], 3000)
        feats = np.pad(feats, ((0, 0), (0, max(0, 3000 - feats.shape[-1]))))[:, :3000]
        enc = m.model.encode(get_ctranslate2_storage(np.expand_dims(feats, 0)), to_cpu=True)
        tokens, seg_of = [], []
        for k, s in enumerate(segs):
            ids = tok.encode(" " + s["text"].strip())
            tokens += ids
            seg_of += [k] * len(ids)
        if not tokens:
            return [[] for _ in segs], 0, 0
        res = m.model.align(enc, tok.sot_sequence, [tokens], frames, median_filter_width=7)[0]
        probs = list(res.text_token_probs)
        text_idx = np.array([p[0] for p in res.alignments])
        time_idx = np.array([p[1] for p in res.alignments])
        jumps = np.pad(np.diff(text_idx), (1, 0), constant_values=1).astype(bool)
        jump_t = time_idx[jumps] / m.tokens_per_second
        words_txt, word_toks = tok.split_to_word_tokens(tokens + [tok.eot])
        bounds = np.pad(np.cumsum([len(x) for x in word_toks[:-1]]), (1, 0))
        raw = []
        for j in range(len(word_toks) - 1):
            lo, hi = int(bounds[j]), int(bounds[j + 1])
            ts = jump_t[lo] if lo < len(jump_t) else jump_t[-1]
            te = jump_t[hi] if hi < len(jump_t) else jump_t[-1]
            raw.append({"text": words_txt[j], "tokens": list(range(lo, hi)), "start": w0 + float(ts),
                        "end": w0 + float(te)})
        words = merge_words(raw)
        timing["encode_align_s"] += time.time() - t

        # doubtful words: the least likely letter-bearing token of each word
        doubts = []
        for j, w in enumerate(words):
            cand = [i for i in w["tokens"] if has_letters(tok.decode([tokens[i]]))] or w["tokens"]
            worst = min(cand, key=lambda i: probs[i] if i < len(probs) else 1.0)
            w["p"] = float(probs[worst]) if worst < len(probs) else 1.0
            w["worst"] = worst
            if w["p"] < st["doubt_below"]:
                doubts.append(j)
        start = tok.sot_sequence + [tok.no_timestamps]
        alts: dict[int, list[dict]] = {}
        seg_bounds = [seg_of.index(k) for k in range(len(segs))] + [len(tokens)]
        for k, s in enumerate(segs):
            ds = [j for j in doubts if seg_of[words[j]["tokens"][0]] == k]
            if not ds:
                continue
            # the line on its own: its audio, and its words as the prefix, so prompts stay short (CTranslate2
            # decodes a forced prefix one token at a time)
            lo, hi = seg_bounds[k], seg_bounds[k + 1]
            t = time.time()
            enc_s = self._encode(m, audio, s["start"] - pad, s["end"] + pad, get_ctranslate2_storage)
            timing["line_encode_s"] += time.time() - t
            t = time.time()
            dists = self._distributions(m, enc_s, start + tokens[lo:hi], hi - lo)
            timing["distributions_s"] += time.time() - t
            t = time.time()
            todo = []  # (doubtful word j, candidate token, p)
            for j in ds:
                worst = words[j]["worst"]
                own = tokens[worst]
                keep = [(tid, p) for tid, p in dists[worst - lo] if p >= st["min_p"] and tid != own
                        and tid < tok.eot and has_letters(tok.decode([tid]))]
                todo += [(j, tid, p) for tid, p in keep[:st["choices"]]]
            for c in range(0, len(todo), state["batch"]):
                part = todo[c:c + state["batch"]]
                ps = [start + tokens[lo:words[j]["worst"]] + [tid] for j, tid, _ in part]
                done = self._complete(m, enc_s, ps, st["follow_tokens"], get_ctranslate2_storage, len(start))
                for (j, tid, p), (new, ended) in zip(part, done):
                    w = words[j]
                    head = [tokens[i] for i in w["tokens"] if i < w["worst"]]
                    cw = tok.decode(head + [tid] + new).split()
                    if not cw:
                        continue
                    following = [x["text"] for x in words[j:] if seg_of[x["tokens"][0]] == k]
                    na, nb = span(cw, following, ended)
                    choice = " ".join(cw[:na])
                    if norm(choice) == norm(" ".join(following[:nb])):
                        continue
                    lst = alts.setdefault(j, [])
                    if not any(norm(x["t"]) == norm(choice) and x["n"] == nb for x in lst):
                        lst.append({"t": choice, "p": round(p, 4), "n": nb})
            timing["completions_s"] += time.time() - t

        by_seg: list[list[dict]] = [[] for _ in segs]
        offered = 0
        for j, w in enumerate(words):
            d = {"w": w["text"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3), "p": round(w["p"], 4)}
            if alts.get(j):
                d["alts"] = sorted(alts[j], key=lambda x: -x["p"])[:st["choices"]]
                offered += 1
            by_seg[seg_of[w["tokens"][0]]].append(d)
        return by_seg, len(doubts), offered

    @staticmethod
    def _encode(m, audio, t0: float, t1: float, storage):
        """Whisper's encoder output for audio[t0:t1] (seconds), on the processor as numpy, so it can be repeated
        for a batch of prompts."""
        import numpy as np
        a, b = int(max(0.0, t0) * SR), int(min(len(audio) / SR, t1) * SR)
        feats = m.feature_extractor(audio[a:min(b, a + int(WINDOW_S * SR))])
        feats = np.pad(feats, ((0, 0), (0, max(0, 3000 - feats.shape[-1]))))[:, :3000]
        return np.asarray(m.model.encode(storage(np.expand_dims(feats, 0)), to_cpu=True))

    @staticmethod
    def _distributions(m, enc_np, prompt: list[int], n: int, k: int = 12):
        """Whisper's distribution over each of the last n tokens of the prompt, given the audio and the tokens
        before it (one forced pass): [[(token id, probability)] * k] * n."""
        import numpy as np
        from faster_whisper.transcribe import get_ctranslate2_storage
        res = m.model.generate(get_ctranslate2_storage(enc_np), [prompt], beam_size=1,
                               max_length=_max_length(len(prompt) + 1), return_logits_vocab=True,
                               suppress_blank=False)[0]
        steps = res.logits[0]
        steps = [_array(v) for v in steps] if isinstance(steps, (list, tuple)) else list(_array(steps))
        out = []
        for lv in steps[:n]:
            lv = lv.reshape(-1) - lv.max()
            pr = np.exp(lv) / np.exp(lv).sum()
            top = np.argsort(-pr)[:k]
            out.append([(int(i), float(pr[i])) for i in top])
        return out

    @staticmethod
    def _complete(m, enc_np, prompts, n: int, storage, skip: int):
        """Greedy continuations of n tokens: (new token ids, whether the text ended first) per prompt. `skip` is
        the length of the start sequence; the result repeats the prompt's tokens after it before the new ones."""
        import numpy as np
        feats = storage(np.repeat(enc_np, len(prompts), axis=0))
        res = m.model.generate(feats, prompts, beam_size=1, max_length=_max_length(max(len(p) for p in prompts) + n),
                               suppress_blank=False)
        out = []
        for p, r in zip(prompts, res):
            seq, given = list(r.sequences_ids[0]), p[skip:]
            new = seq[len(given):] if seq[:len(given)] == given else seq
            out.append((new[:n], len(new) < n))
        return out

    def teardown(self, state) -> None:
        state.clear()



def _auto_batch(device: str) -> int:
    """Prompts to decode at once: what the card's free memory holds after a gigabyte's margin, 1 to 16."""
    if device != "cuda":
        return 4
    try:
        import torch
        free, _ = torch.cuda.mem_get_info()
    except Exception:
        return 4
    return max(1, min(16, int((free - 2**30) // (260 * 2**20))))


def _max_length(tokens: int) -> int:
    """generate()'s max_length for a result of this many tokens, prompt included. CTranslate2 4.8.3's Whisper stops
    at about half the max_length it is given (measured: 28, 40 and 50 gave 14, 20 and 25 tokens of a 27-token
    prompt), so give it twice the length and cut the result."""
    return min(448, 2 * tokens + 4)


def _array(view):
    """A CTranslate2 StorageView (on any device) as a float32 numpy array."""
    import ctranslate2
    import numpy as np
    if getattr(view, "device", "cpu") != "cpu":
        view = view.to_device(ctranslate2.Device.cpu)
    return np.asarray(view).astype(np.float32)
