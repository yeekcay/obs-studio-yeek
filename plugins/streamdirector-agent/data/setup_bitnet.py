"""
Setup script for BitNet integration with StreamDirector.

This script helps set up the BitNet inference server that the BitNet agent mode
connects to. It clones the BitNet repo, downloads a model, and builds the project.

Usage:
    python setup_bitnet.py

After setup, start the inference server with:
    cd D:\Code\BitNet
    python run_inference.py -m models/bitnet_b1_58-3B/ggml-model-i2_s.gguf -p "You are a helpful assistant" -cnv

Or better, use the llama.cpp server mode (if available):
    build/bin/llama-server -m models/bitnet_b1_58-3B/ggml-model-i2_s.gguf --port 8080

The StreamDirector BitNet agent mode connects to http://127.0.0.1:8080 by default.
Set BITNET_SERVER_URL environment variable to change this.
"""

import os
import subprocess
import sys

BITNET_DIR = r"D:\Code\BitNet"
DEFAULT_MODEL = "1bitLLM/bitnet_b1_58-3B"
DEFAULT_QUANT = "i2_s"


def run(cmd, cwd=None, check=True):
    """Run a command and stream output."""
    print(f">>> {cmd}")
    result = subprocess.run(
        cmd, shell=True, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout)
    if check and result.returncode != 0:
        print(f"Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


def main():
    print("=" * 60)
    print("StreamDirector BitNet Setup")
    print("=" * 60)

    # Step 1: Clone BitNet
    if not os.path.exists(BITNET_DIR):
        print(f"\n[1/4] Cloning BitNet to {BITNET_DIR}...")
        run(f'git clone --recursive https://github.com/microsoft/BitNet.git "{BITNET_DIR}"')
    else:
        print(f"\n[1/4] BitNet already exists at {BITNET_DIR}")

    # Step 2: Install Python dependencies
    print("\n[2/4] Installing Python dependencies...")
    run(f'pip install -r "{os.path.join(BITNET_DIR, "requirements.txt")}"', check=False)

    # Step 3: Download model and build
    print(f"\n[3/4] Downloading model {DEFAULT_MODEL} and building...")
    print("This will download the model and compile bitnet.cpp (may take several minutes)...")
    run(
        f'python setup_env.py -hr {DEFAULT_MODEL} -q {DEFAULT_QUANT}',
        cwd=BITNET_DIR,
        check=False,
    )

    # Step 4: Verify
    print("\n[4/4] Setup complete!")
    print("\nTo start the inference server:")
    print(f'  cd "{BITNET_DIR}"')
    print(f'  python run_inference.py -m models/bitnet_b1_58-3B/ggml-model-i2_s.gguf -p "You are a helpful assistant" -cnv')
    print("\nOr if llama-server was built:")
    print(f'  "{BITNET_DIR}/build/bin/llama-server" -m models/bitnet_b1_58-3B/ggml-model-i2_s.gguf --port 8080')
    print("\nThen in StreamDirector, select 'BitNet (Fast)' mode and type instructions.")
    print("\nSet BITNET_SERVER_URL env var to change the server URL (default: http://127.0.0.1:8080)")


if __name__ == "__main__":
    main()
