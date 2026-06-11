# 🆚 LLM Duel

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-1.30+-FF4B4B?style=flat-square&logo=streamlit&logoColor=white)
![python-dotenv](https://img.shields.io/badge/python--dotenv-1.0+-ECD53F?style=flat-square&logo=dotenv&logoColor=black)
![requests](https://img.shields.io/badge/requests-2.31+-2B5B84?style=flat-square)
![LLMs](https://img.shields.io/badge/LLMs-OpenAI%20%7C%20Anthropic%20%7C%20Gemini%20%7C%20Ollama-FF8C42?style=flat-square)

<img width="600" height="370" alt="-c4FdaGxKTw5sZ5fPFmVfFPmVU63dJi_63z-R2jzTcp4D-Rs-ZqMzc531AgqNjmI0YwOBm3cXrHFY4VD0VzOsOKT3cYejHk6EY38mCGsmzpSLOu3UOnmw50Sxn-qAWwAsGXi6LrLKHyEVAeYrdhFOA" src="https://github.com/user-attachments/assets/6587bd35-c19e-48e6-890d-280e65c1aded" />

**[LLM_Duel_Manual.pdf](LLM_Duel_Manual.pdf)**

Chat with two (or more) LLMs **side by side**, with the same conversation, and
compare their answers as they **stream in live**. Each model is driven by a
small, editable block of Python — so you can wire up ChatGPT, Claude, Gemini, a
local model, or your own, without the app needing to know how any of them work.

## The contract

Every panel's code follows the same fixed interface:

| | name | what it is |
|---|---|---|
| **in** | `prompt` | the latest user message (a string) |
| **in** | `messages` | the full conversation so far — `list[{"role", "content"}]` |
| **out** | `response` | a **string** (shown at once), **or** a **generator** that yields text chunks (streamed live) |

That's the whole interface. SDKs, HTTP, auth — all up to your code.

```python
# Example panel: OpenAI, streaming, multi-turn
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=messages,          # full history, so it remembers the conversation
    stream=True,
)

def gen():
    for event in stream:
        if event.choices[0].delta.content:
            yield event.choices[0].delta.content

response = gen()                 # a generator -> streamed token by token
```

## Quick start

```powershell
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501. It works immediately with the **Echo** template
(no keys). In each panel's **⚙️ Code** expander, pick a template — it applies
the moment you select it and titles the panel after it (the title stays
editable) — then edit the code and chat via the box at the bottom.

## API keys

Enter your keys in the sidebar under **🔑 API keys** (and the RAG store id under
**📁 File Search**). They're kept for the session only and injected into the
environment your panel code reads via `os.environ` — so no `.env` file is needed.

## Installing model SDKs

Open the **🔧 Terminal** section in the sidebar and run, e.g.:

```
pip install openai anthropic google-generativeai
```

It installs into the same interpreter the app runs in, so your panel code can
import it right away.

## How streaming + concurrency works

Each panel's code runs in its **own worker thread** that pushes text chunks into
a queue; the main thread drains every queue and repaints the transcripts. So the
panels stream **at the same time** — you wait for the slowest model, not the sum
of all of them. (Verified: two half-second responses finish in ~0.5s, not ~1.0s.)

## Features

- **Side-by-side chat** with shared input and per-panel transcripts.
- **Live streaming**, genuinely concurrent across panels.
- **Multi-turn** — each panel keeps its own history, so models remember the chat.
- **Templates** per panel: OpenAI, Anthropic, Gemini, local (Ollama/HTTP), Echo.
- **Save / load** your setup to `llm_duel_config.json` (auto-loaded on start).
- **2–4 panels** (sidebar). **Clear all chats** to start fresh.

## Manual

A full illustrated user manual (with real screenshots of the app) is included in
two formats:

- **[LLM_Duel_Manual.pdf](LLM_Duel_Manual.pdf)** — portable, print-ready.
- **[LLM_Duel_Manual.docx](LLM_Duel_Manual.docx)** — editable in Word.

Both are generated from [manual.html](manual.html) and the screenshots in
[assets/](assets/) by one script:

```powershell
pip install playwright python-docx ; python -m playwright install chromium
python tools/make_manual.py                 # capture fresh screenshots, then build PDF + DOCX
python tools/make_manual.py --skip-capture  # rebuild from existing screenshots only
```

`make_manual.py` launches the app on a temporary port, drives it with a headless
browser to capture the screenshots, then renders both documents — so the manual
stays in sync whenever the UI changes.

## Note on security

Panels run with `exec()`, and the Terminal runs shell commands — full power, by
design, so you can use any SDK. Only paste code you trust. This is built as a
local, single-user tool.
