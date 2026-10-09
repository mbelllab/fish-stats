#!/bin/bash
# Transcribe on the GPU with the heat guard on (gpu_guard.sh pauses the work if the card
# gets too hot; it reads AMD's sensors). NO_GPU_GUARD=1 runs without it.
# Set VENV to your Python environment if it isn't ~/fish-stats-venv.
# HSA_OVERRIDE_GFX_VERSION is for an AMD RX 6700/6750 XT (gfx1031); change or remove it for
# another card.
export HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION-10.3.0}
export PATH="${VENV:-$HOME/fish-stats-venv}/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
mkdir -p "$HERE/../data/work/logs"
if [ "$NO_GPU_GUARD" != 1 ]; then
    (cd "$HERE" && bash gpu_guard.sh --ensure) || exit 1
fi
python "$HERE/transcribe.py" "$@" 2>&1 | tee -a "$HERE/../data/work/logs/transcribe.log"
exit "${PIPESTATUS[0]}"
