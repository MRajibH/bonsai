# bonsai

A small ChatGPT-style chat app that runs **Bonsai 27B (1-bit)** locally on CPU, and can also
chat with OpenAI, Google Gemini, Anthropic, NVIDIA and any OpenAI-compatible local server
(Ollama, LM Studio).

## Run

```bash
./start.sh
```

On first run the launcher downloads what is missing, then opens the chat at http://127.0.0.1:8090:

- the PrismML llama.cpp build (about 20 MB) into `~/models/llama-prism/`
- the model `Bonsai-27B-Q1_0.gguf` (3.8 GB) into `~/models/bonsai-27b/`

The model is not stored in this repository because it is larger than GitHub allows. It comes from
https://huggingface.co/prism-ml/Bonsai-27B-gguf

```bash
./start.sh stop     # stop the model and the web server
./start.sh status
```

## Requirements

- Linux x86-64 with AVX2, about 10 GB of free RAM (the loaded model uses roughly 8 GB)
- `python3` (standard library only), `curl`, `tar`

## What is in here

| File | Purpose |
| --- | --- |
| `start.sh` | One-click launcher: download, start, open the browser |
| `server.py` | Local server on 127.0.0.1: serves the page, forwards chat requests to Bonsai or a provider |
| `web/index.html` | The chat page (no build step, no dependencies) |
| `icon.svg` | App icon |

## Other providers

Open **Settings** in the sidebar, pick a provider tab, paste your API key and add models.
Keys are saved only on your machine in `config.json` (ignored by git) and are sent only to that provider.
