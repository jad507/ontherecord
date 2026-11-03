#!/usr/bin/env bash
# The Python environment that serves NVIDIA Nemotron 3.5 Transcribe (the nemotron_transcribe stage): vLLM 0.32.0
# with NeMo Speech's vLLM plugin, installed the way the model card says
# (https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8, "Quantized Runtime and Environment"),
# but pinned: NeMo Speech at one commit, every package from its uv.lock (hashes checked), and the card's two extra
# wheels by SHA-256. It is its own environment because it needs PyTorch for CUDA 13, and On the Record's audio
# environment must stay on CUDA 12 for faster-whisper.
#
#   install/nemotron-vllm.sh [DIR]      # default ~/src/nemo-speech; then name DIR/.venv/bin/vllm in machine.yaml:
#                                       #   tools: {nemotron_vllm: ~/src/nemo-speech/.venv/bin/vllm}
#
# Several GB of packages. Nothing here touches the GPU. FlashInfer compiles kernels the first time the server
# starts, with nvcc from a CUDA 13 toolkit (the stage puts /usr/local/cuda/bin on PATH).
set -euo pipefail

NEMO_REPO=https://github.com/NVIDIA-NeMo/Speech.git
# main on Oct 31, 2026 ("Derive word-confidence boundaries from the decoded text itself (#16064)"). The model card
# asks for "the latest main"; its last change is Oct 29, and this is the first main commit after it.
NEMO_COMMIT=e5d79d5a49cbec49d79440b0070aac0c32a8dbb8
DIR=${1:-$HOME/src/nemo-speech}

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=uv-pinned.sh
. "$here/uv-pinned.sh"

if [ ! -d "$DIR/.git" ]; then
  git clone --filter=blob:none "$NEMO_REPO" "$DIR"
fi
git -C "$DIR" fetch --quiet origin "$NEMO_COMMIT" 2>/dev/null || git -C "$DIR" fetch --quiet origin
git -C "$DIR" -c advice.detachedHead=false checkout --quiet "$NEMO_COMMIT"
if [ "$(git -C "$DIR" rev-parse HEAD)" != "$NEMO_COMMIT" ]; then
  echo "NeMo Speech is not at the pinned commit $NEMO_COMMIT. Stopping."
  exit 1
fi

cd "$DIR"
[ -x .venv/bin/python ] || uv venv --python 3.13 .venv
UV_PROJECT_ENVIRONMENT=$PWD/.venv uv sync --extra asr --extra vllm --locked --no-install-project
uv pip install --python .venv/bin/python --no-deps -e .
# The card's two extras: PyAV decodes compressed audio (FLAC, M4A) without a system ffmpeg; vLLM needs OpenCV,
# which NeMo's settings leave out. Digests from PyPI on Nov 2, 2026.
extras=$(mktemp)
cat > "$extras" <<'EOF'
av==19.0.2 --hash=sha256:9c1da7aad0fa6c13b4a5c1e7985be0d13f8526a96284573acaf3d55ead6d3fe8
opencv-python-headless==5.0.0.93 --hash=sha256:09a872a157c1376ab922a69bbf22f9a95bcc7b658a9d8b436a60212b02b2eeb4 --hash=sha256:ed709fdf9aa0bd1f2ed8549e71d19449b03a675bb581eb292285f6861953be37
EOF
# --no-config: NeMo's pyproject overrides opencv-python-headless to "never install", and uv pip reads that here
uv pip install --no-config --python .venv/bin/python --no-deps --require-hashes -r "$extras"
.venv/bin/python -c "import importlib.util as u; assert u.find_spec('cv2') and u.find_spec('av'), 'cv2 or av missing'"
rm -f "$extras"
echo "$NEMO_COMMIT" > .venv/nemo-speech-commit.txt

.venv/bin/python -c "import vllm, nemo; print('vllm', vllm.__version__, '| nemo', nemo.__version__)"
echo
echo "Done. Add to machine.yaml:"
echo "  tools:"
echo "    nemotron_vllm: $DIR/.venv/bin/vllm"
