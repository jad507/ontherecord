# On the Record in Docker

For Linux machines and servers. On a laptop, the installers in `install/` are the better choice: on macOS a
container cannot use the Apple GPU, on Windows it needs Docker Desktop and WSL2, and on both a camera card
plugged into the computer is not visible inside the container.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build   # with an NVIDIA GPU
docker compose up -d --build                                                    # CPU only
```

Open http://127.0.0.1:8765/ on the same machine (from another one: `ssh -L 8765:127.0.0.1:8765 HOST`). Everything
that persists is in `./data`: the workspace, the archive, the models, the Hugging Face cache and `secrets.env`.

Models and the Hugging Face token:

```bash
docker compose exec ontherecord ontherecord models
docker compose exec ontherecord ontherecord models get whisper-large-v3 gemma-4-E4B gemma-4-26B
docker compose exec ontherecord ontherecord models get pyannote-community-1 --token hf_... --save-token
```

On a cluster that runs Apptainer rather than Docker, the same image works: `apptainer build ontherecord.sif
docker-daemon://ontherecord:0.1.0`, then run it with `--nv` and `/data` bound to a project directory.

The container runs as your user (`user:` in docker-compose.yml, from `UID` and `GID`), so the files it writes in
`./data` are yours. Tested on Linux with an NVIDIA GPU (driver 615, NVIDIA Container Toolkit 1.20): the GUI, the
queue, and a run from audio through speakers and Whisper to alignment, all on the GPU.

The image records itself in every receipt like any other install: Python packages, the llama.cpp build, the GPU.
