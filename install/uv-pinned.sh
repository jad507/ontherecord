# Sourced by the Linux and macOS installers: puts uv 0.11.25 in ~/.local/bin unless a uv is already installed.
# The archive comes from uv's GitHub release and is kept only if its SHA-256 is the one written here (GitHub's
# digest for the asset, which uv's own installer script also carries), so a changed download stops the install.
UV_VERSION=0.11.25
if ! command -v uv >/dev/null 2>&1; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)               UV_ASSET=uv-x86_64-unknown-linux-musl.tar.gz
                                UV_SHA256=7195cfcc1785d0c559d4682ad9109f26664b8db4d828d42d66078ef454061891 ;;
    Linux-aarch64|Linux-arm64)  UV_ASSET=uv-aarch64-unknown-linux-musl.tar.gz
                                UV_SHA256=3d9c9a95f1868ff20ac880712f3a337d710ea4b65f135184b7a35635d1acbf84 ;;
    Darwin-arm64)               UV_ASSET=uv-aarch64-apple-darwin.tar.gz
                                UV_SHA256=5fc334bb25d19806262efd1f6e7d380155c7e817d89bf426df4ba7ae873c9471 ;;
    Darwin-x86_64)              UV_ASSET=uv-x86_64-apple-darwin.tar.gz
                                UV_SHA256=65ff85b33212f75d34d7c0f0724aba9a742c74f62559f67dc0d6c543dc2fc52f ;;
    *) echo "No pinned uv for $(uname -s) $(uname -m). Install uv (https://docs.astral.sh/uv/), then run this again."
       exit 1 ;;
  esac
  echo "Installing uv $UV_VERSION, the Python installer (https://docs.astral.sh/uv/)..."
  UV_TMP=$(mktemp -d)
  curl -LsSf "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ASSET" -o "$UV_TMP/$UV_ASSET"
  if command -v sha256sum >/dev/null 2>&1; then UV_GOT=$(sha256sum "$UV_TMP/$UV_ASSET" | cut -d' ' -f1)
  else UV_GOT=$(shasum -a 256 "$UV_TMP/$UV_ASSET" | cut -d' ' -f1); fi
  if [ "$UV_GOT" != "$UV_SHA256" ]; then
    echo "uv's download does not match its pinned SHA-256, so it was not installed. Stopping."
    rm -rf "$UV_TMP"
    exit 1
  fi
  tar -xzf "$UV_TMP/$UV_ASSET" -C "$UV_TMP"
  mkdir -p "$HOME/.local/bin"
  cp "$UV_TMP"/uv-*/uv "$UV_TMP"/uv-*/uvx "$HOME/.local/bin/"
  rm -rf "$UV_TMP"
  export PATH="$HOME/.local/bin:$PATH"
fi
