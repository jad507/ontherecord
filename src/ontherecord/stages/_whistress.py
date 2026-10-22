"""WhiStress inference (Yosha, Shechtman and Adi, Interspeech 2025), vendored from https://github.com/slp-rl/WhiStress
at commit bb8921f5, the inference path only, with one fix: transformers 5 decoder layers return the hidden states
tensor itself, not a tuple (`_layer_hidden`). Upstream's numbers reproduce with it: StressTest F1 0.884 vs the
published 0.883.

Differences from upstream that cannot change an output: the module is a plain nn.Module rather than a
PreTrainedModel (the backbone comes from from_pretrained and the head from its saved state dicts either way), the
head's weights load with torch.load(weights_only=True) (they are plain state dicts), and the device is the model's
own rather than "cuda if available".

MIT License, Copyright (c) 2025 SLP-RL

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

BACKBONE = "openai/whisper-small.en"
WEIGHTS_REPO = "slprl/WhiStress"
WEIGHT_FILES = ("classifier.pt", "additional_decoder_block.pt", "metadata.json")
EOT = 50256  # whisper-small.en's end-of-text token, whose head output upstream masks


def _layer_hidden(outputs):
    return outputs if isinstance(outputs, torch.Tensor) else outputs[0]


class FCNN(nn.Module):
    def __init__(self, input_dim, output_dim):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, 2 * input_dim)
        self.fc2 = nn.Linear(2 * input_dim, output_dim)

    def forward(self, x):
        return self.fc2(F.relu(self.fc1(x)))


class WhiStress(nn.Module):
    def __init__(self, weights_dir: Path, device: str, backbone: str = BACKBONE, revision: str | None = None):
        super().__init__()
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        from transformers.models.whisper.modeling_whisper import WhisperDecoderLayer

        self.whisper_model = WhisperForConditionalGeneration.from_pretrained(backbone, revision=revision).eval()
        self.processor = WhisperProcessor.from_pretrained(backbone, revision=revision)
        self.processor.tokenizer.model_input_names = ["input_ids", "attention_mask", "labels_head"]
        cfg = self.whisper_model.config
        self.additional_decoder_block = WhisperDecoderLayer(cfg)
        self.classifier = FCNN(cfg.d_model, 2)
        self.classifier.load_state_dict(torch.load(weights_dir / "classifier.pt", weights_only=True,
                                                   map_location=device))
        self.additional_decoder_block.load_state_dict(
            torch.load(weights_dir / "additional_decoder_block.pt", weights_only=True, map_location=device))
        self.layer_for_head = json.loads((weights_dir / "metadata.json").read_text())["layer_for_head"]
        self.device_name = device
        self.to(device)
        self.eval()

    def _head(self, input_features, decoder_input_ids):
        out = self.whisper_model(input_features=input_features, decoder_input_ids=decoder_input_ids,
                                 output_hidden_states=True)
        block = self.additional_decoder_block(hidden_states=out.decoder_hidden_states[self.layer_for_head],
                                              encoder_hidden_states=out.encoder_hidden_states[self.layer_for_head])
        return self.classifier(_layer_hidden(block))

    @torch.inference_mode()
    def heard(self, features):
        """Whisper transcribes; the head labels its tokens. Returns (tokens, head logits)."""
        gen = self.whisper_model.generate(input_features=features, max_length=200, return_dict_in_generate=True)
        return gen.sequences, self._head(features, gen.sequences)

    @torch.inference_mode()
    def given(self, features, transcription: str):
        """The head labels the tokens of a transcript it is given (upstream: max 30 tokens, padded)."""
        ids = self.processor.tokenizer(transcription, return_tensors="pt", padding="max_length", truncation=True,
                                       max_length=30)["input_ids"].to(self.device_name)
        return ids, self._head(features, ids)


def prepare_audio(y: np.ndarray, sr: int, target_sr: int = 16000) -> np.ndarray:
    """Upstream's: resample to 16 kHz with librosa, then peak-normalize."""
    import librosa
    y = librosa.resample(np.array(y, dtype=float), orig_sr=sr, target_sr=target_sr)
    return y / max(abs(y))


def features(model: WhiStress, y16: np.ndarray):
    return model.processor.feature_extractor(y16, sampling_rate=16000, return_tensors="pt")["input_features"].to(
        model.device_name)


def word_pairs(model: WhiStress, tokens, logits) -> list[tuple[str, int, float]]:
    """[(word, label, p_stressed)] for one clip: the head's outputs shifted right by one onto the tokens, special
    tokens dropped, sub-tokens merged into words (a word is stressed if any of its tokens is; its probability is
    the highest of its tokens')."""
    probs = F.softmax(logits, dim=-1)
    labels, p1 = probs.argmax(dim=-1), probs[..., 1].float()

    def shift(t):
        return torch.cat((t[:, -1:], t[:, :-1]), dim=1)[0].tolist()

    tok = model.processor.tokenizer
    special = set(tok.all_special_ids)
    rows = [(tok.decode([t], skip_special_tokens=False), lab, p) for t, lab, p in
            zip(tokens[0].tolist(), shift(labels), shift(p1)) if t not in special]
    merged: list[list] = []
    for text, lab, p in rows:
        if text.startswith(" ") or not merged:
            merged.append([text, lab, p])
        else:
            merged[-1][0] += text
            merged[-1][1] = max(merged[-1][1], lab)
            merged[-1][2] = max(merged[-1][2], p)
    return [(w.strip(), int(lab), float(p)) for w, lab, p in merged]
