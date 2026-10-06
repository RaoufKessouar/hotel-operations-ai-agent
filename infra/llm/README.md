# Optional local Qwen server

The default configuration uses DeepSeek through an API. This directory provides an optional OpenAI-compatible server for local model comparison.

## Start on a Linux GPU host

```bash
export HOTEL_AGENT_PYTHON=/path/to/python
export MODEL_DEVICE=cuda:0
export MODEL_CACHE_DIR="$HOME/.cache/huggingface"
bash infra/llm/start_qwen_server.sh
```

The service listens on `127.0.0.1:8000` and serves `Qwen/Qwen2.5-14B-Instruct` by default.

To stop it:

```bash
bash infra/llm/stop_qwen_server.sh
```

If the GPU host is remote, expose the loopback-only service through your own secured tunnel. No host name, user account or private path is embedded in this repository.

## Use from the Windows launcher

Once the endpoint is available locally on port 8000:

```powershell
.\start_agentpms.ps1 -LocalQwen
```

The local server is a lightweight comparison implementation. The main DeepSeek path uses native function calling, while this endpoint relies on structured model output that is still validated by the application.

