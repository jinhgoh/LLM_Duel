"""
LLM Duel — chat with two (or more) LLMs side by side and compare them live.

Core idea (yours):
  * Each panel holds a small, editable block of Python code.
  * The code is handed the conversation and must produce the model's reply.
  * Because that input/output contract is fixed, the app can drive any model
    — OpenAI, Anthropic, Gemini, a local model, your own — without knowing
    anything about how each one works internally.

The contract each panel's code follows:
  IN   prompt    -> str, the latest user message
       messages  -> list[{"role": "user"|"assistant", "content": str}],
                    the full conversation history for THIS panel
  OUT  response  -> either a plain string (shown at once),
                    or a generator that yields text chunks (streamed live)

Extras on top: per-panel templates, a Terminal to `pip install`, save/load
your setup, and 1-4 panels. Streaming is genuinely concurrent (each panel
runs in its own thread feeding a queue; the main thread paints them all).

Run with:  streamlit run app.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

import streamlit as st

# Optional: still load a local .env if one happens to exist. The primary way to
# provide keys is the sidebar (🔑 API keys), which injects them at run time.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Templates — starting points for the per-panel code box.                     #
# Each reads `prompt` / `messages` and sets `response` (string or generator). #
# --------------------------------------------------------------------------- #
ECHO = '''# No API key needed — streams an echo so you can see the UI working.
# You get `prompt` (latest message) and `messages` (full history).
# Set `response` to a string, OR a generator that yields text chunks.
import time

def gen():
    for word in ("You said: " + prompt).split():
        yield word + " "
        time.sleep(0.02)

response = gen()
'''

OPENAI_TPL = '''# pip install openai   |   set OPENAI_API_KEY in the sidebar (🔑 API keys)
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
stream = client.chat.completions.create(
    model="gpt-4o",                 # any model you have access to
    messages=messages,              # full conversation history
    stream=True,
)

def gen():
    for event in stream:
        delta = event.choices[0].delta.content
        if delta:
            yield delta

response = gen()
'''

ANTHROPIC_TPL = '''# pip install anthropic   |   set ANTHROPIC_API_KEY in the sidebar (🔑 API keys)
import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def gen():
    with client.messages.stream(
        model="claude-sonnet-4-6",  # e.g. claude-opus-4-8, claude-haiku-4-5
        max_tokens=1024,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text

response = gen()
'''

GEMINI_TPL = '''# pip install google-genai   |   set GOOGLE_API_KEY in the sidebar (🔑 API keys)
import os
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))

# Convert the shared history to the SDK's format (assistant -> "model").
history = [
    types.Content(
        role="model" if m["role"] == "assistant" else "user",
        parts=[types.Part(text=m["content"])],
    )
    for m in messages[:-1]          # everything before the latest user turn
]
chat = client.chats.create(model="gemini-2.5-flash", history=history)  # or -2.5-pro, -3-flash-preview

def gen():
    for chunk in chat.send_message_stream(prompt):
        if chunk.text:
            yield chunk.text

response = gen()
'''

GEMINI_FILESEARCH_TPL = '''# New Google SDK + File Search (RAG over your own document store).
# pip install google-genai
# Set GOOGLE_API_KEY (🔑 API keys) and FILE_SEARCH_STORE_NAME (📁 File Search) in the sidebar.
import os
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
store = os.environ.get("FILE_SEARCH_STORE_NAME")

# Send the whole conversation (assistant -> "model") so it remembers prior turns.
contents = [
    types.Content(
        role="model" if m["role"] == "assistant" else "user",
        parts=[types.Part(text=m["content"])],
    )
    for m in messages
]

response_raw = client.models.generate_content(
    model="gemini-2.5-flash",       # also: gemini-2.5-pro, gemini-3-flash-preview
    # 2.5-flash / 2.5-pro have free-tier quota; gemini-2.0-flash no longer does.
    contents=contents,              # full history -> multi-turn memory
    config=types.GenerateContentConfig(
        tools=[
            types.Tool(
                file_search=types.FileSearch(
                    file_search_store_names=[store]
                )
            )
        ]
    ),
)
response = response_raw.text
'''

OLLAMA_TPL = '''# Local model via Ollama (https://ollama.com) — no API key needed.
# First, in a terminal:  ollama run llama3
import json
import requests   # pip install requests

def gen():
    with requests.post(
        "http://localhost:11434/api/chat",
        json={"model": "llama3", "messages": messages, "stream": True},
        stream=True,
    ) as r:
        for line in r.iter_lines():
            if line:
                piece = json.loads(line).get("message", {}).get("content", "")
                if piece:
                    yield piece

response = gen()
'''

PRESETS = {
    "Echo (offline, no key)": ECHO,
    "OpenAI / ChatGPT": OPENAI_TPL,
    "Anthropic / Claude": ANTHROPIC_TPL,
    "Google / Gemini": GEMINI_TPL,
    "Google / Gemini (File Search)": GEMINI_FILESEARCH_TPL,
    "Local (Ollama / custom HTTP)": OLLAMA_TPL,
}

DEFAULT_CODE = ECHO
CONFIG_PATH = "llm_duel_config.json"
MAX_PANELS = 4
STREAM_TIMEOUT = 300  # seconds; safety cap so a hung model can't freeze the run

# Secret fields shown in the sidebar; injected into os.environ before each run
# so panel code keeps working with os.environ.get(...).
SECRET_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "FILE_SEARCH_STORE_NAME",
]


# --------------------------------------------------------------------------- #
# State helpers                                                               #
# --------------------------------------------------------------------------- #
def history(i: int) -> list:
    key = f"history_{i}"
    if key not in st.session_state:
        st.session_state[key] = []
    return st.session_state[key]


def clear_chats() -> None:
    for i in range(MAX_PANELS):
        st.session_state[f"history_{i}"] = []


# --------------------------------------------------------------------------- #
# Execution — one worker thread per panel, streaming chunks into a queue.     #
# Worker threads must NOT touch st.*; they only run plain Python.             #
# --------------------------------------------------------------------------- #
def _producer(code: str, prompt: str, messages: list, q: "queue.Queue") -> None:
    namespace = {
        "prompt": prompt,
        "messages": messages,
        "response": None,
        "__name__": "__llm_panel__",
    }
    try:
        exec(code, namespace)  # noqa: S102 — intentional: you author this code
        resp = namespace.get("response")
        if resp is None:
            q.put(("chunk", "(code ran but never set a `response` variable)"))
        elif isinstance(resp, str):
            q.put(("chunk", resp))
        else:
            try:
                for piece in resp:          # a generator / iterable of chunks
                    if piece:
                        q.put(("chunk", str(piece)))
            except TypeError:
                q.put(("chunk", str(resp)))  # not iterable — just stringify it
    except Exception:
        q.put(("error", traceback.format_exc()))
    finally:
        q.put(("done", None))


def format_response(text: str) -> str:
    """If the whole reply is a JSON object/array, pretty-print it in a ```json
    block. Otherwise return it unchanged. A code fence around the JSON (e.g.
    ```json ... ```) is tolerated. Scalars ("42", "true") are left as-is."""
    stripped = text.strip()
    candidate = stripped
    if candidate.startswith("```"):
        # drop opening fence (```json / ```), keep the body, drop trailing fence
        body = candidate.split("\n", 1)[1] if "\n" in candidate else ""
        candidate = body.rsplit("```", 1)[0].strip() if body.endswith("```") else body.strip()
    if not candidate or candidate[0] not in "{[":
        return text
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return f"```json\n{pretty}\n```"


def apply_secrets() -> None:
    """Inject sidebar-entered keys into the environment the panel code reads."""
    for key in SECRET_KEYS:
        value = st.session_state.get(f"secret_{key}", "")
        if value:
            os.environ[key] = value


def run_stream(placeholders: dict, prompt: str) -> None:
    """Stream all panels concurrently into their placeholders, then persist."""
    apply_secrets()
    queues, threads = {}, {}
    start = time.perf_counter()
    for i in placeholders:
        queues[i] = queue.Queue()
        threads[i] = threading.Thread(
            target=_producer,
            args=(st.session_state[f"code_{i}"], prompt, history(i), queues[i]),
            daemon=True,
        )
        threads[i].start()

    texts = {i: "" for i in placeholders}
    errors = {i: None for i in placeholders}
    elapsed = {i: 0.0 for i in placeholders}
    active = set(placeholders)

    while active:
        for i in list(active):
            try:
                while True:
                    kind, payload = queues[i].get_nowait()
                    if kind == "chunk":
                        texts[i] += payload
                    elif kind == "error":
                        errors[i] = payload
                    elif kind == "done":
                        elapsed[i] = time.perf_counter() - start
                        active.discard(i)
                        break
            except queue.Empty:
                pass
            cursor = " ▌" if i in active else ""
            placeholders[i].markdown(texts[i] + cursor)

        if time.perf_counter() - start > STREAM_TIMEOUT:
            for i in list(active):
                errors[i] = errors[i] or f"Timed out after {STREAM_TIMEOUT}s."
                elapsed[i] = time.perf_counter() - start
                active.discard(i)
        time.sleep(0.04)

    for i in placeholders:
        if errors[i]:
            content = f"⚠️ **Error**\n```\n{errors[i]}\n```"
        else:
            content = format_response(texts[i]) if texts[i] else "(empty response)"
        placeholders[i].markdown(content)
        history(i).append(
            {"role": "assistant", "content": content, "elapsed": round(elapsed[i], 2)}
        )


def run_shell(command: str) -> dict:
    """Run a shell command. `pip ...` is rewritten to target THIS interpreter."""
    stripped = command.strip()
    if stripped.startswith("pip "):
        command = f'"{sys.executable}" -m {stripped}'
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=600
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        returncode = proc.returncode
    except Exception as exc:
        out = f"{type(exc).__name__}: {exc}"
        returncode = -1
    return {"out": out, "code": returncode, "elapsed": time.perf_counter() - start}


# --------------------------------------------------------------------------- #
# Config save / load                                                          #
# --------------------------------------------------------------------------- #
def current_config() -> dict:
    n = st.session_state.get("num_panels", 2)
    return {
        "panels": [
            {
                "name": st.session_state.get(f"name_{i}", f"Model {i + 1}"),
                "code": st.session_state.get(f"code_{i}", DEFAULT_CODE),
            }
            for i in range(n)
        ]
    }


def apply_config(cfg: dict) -> None:
    panels = cfg.get("panels", [])
    st.session_state["num_panels"] = max(1, min(MAX_PANELS, len(panels) or 1))
    for i, panel in enumerate(panels[:MAX_PANELS]):
        st.session_state[f"name_{i}"] = panel.get("name", f"Model {i + 1}")
        st.session_state[f"code_{i}"] = panel.get("code", DEFAULT_CODE)
        st.session_state[f"history_{i}"] = []  # reset chats when loading a setup


def save_config() -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(current_config(), handle, indent=2, ensure_ascii=False)
    st.session_state["_toast"] = f"Saved to {CONFIG_PATH}"


def load_config() -> None:
    if not os.path.exists(CONFIG_PATH):
        st.session_state["_toast"] = "No saved config found."
        return
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        apply_config(json.load(handle))
    st.session_state["_toast"] = "Loaded."


def apply_template(i: int) -> None:
    """Selecting a template applies it immediately and titles the panel after it."""
    preset = st.session_state.get(f"preset_{i}")
    if preset in PRESETS:
        st.session_state[f"code_{i}"] = PRESETS[preset]
        st.session_state[f"name_{i}"] = preset


def init_state() -> None:
    if st.session_state.get("_init"):
        return
    st.session_state["num_panels"] = 2
    for i in range(MAX_PANELS):
        st.session_state[f"name_{i}"] = f"Model {i + 1}"
        st.session_state[f"code_{i}"] = DEFAULT_CODE
        st.session_state[f"history_{i}"] = []
        st.session_state[f"send_{i}"] = True   # panel receives prompts by default
    for key in SECRET_KEYS:             # pre-fill from any existing environment value
        st.session_state[f"secret_{key}"] = os.environ.get(key, "")
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as handle:
                apply_config(json.load(handle))
        except Exception:
            pass
    st.session_state["_init"] = True


# --------------------------------------------------------------------------- #
# UI                                                                          #
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="LLM Duel", layout="wide")
init_state()
st.session_state.setdefault("show_code", True)
# Deferred flip: set after the first message, applied before the toggle renders.
if st.session_state.pop("_collapse_code_next", False):
    st.session_state["show_code"] = False

st.markdown(
    "<style>textarea{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"
    "monospace !important;font-size:0.85rem !important;}</style>",
    unsafe_allow_html=True,
)

if st.session_state.get("_toast"):
    st.toast(st.session_state.pop("_toast"))

with st.sidebar:
    st.header("⚙️ Settings")
    st.number_input("Panels", min_value=1, max_value=MAX_PANELS, step=1, key="num_panels")
    st.toggle("⚙️ Show all code editors", key="show_code")
    st.button("🗑 Clear all chats", on_click=clear_chats, use_container_width=True)

    st.divider()
    with st.expander("🔑 API keys", expanded=False):
        st.text_input("OPENAI_API_KEY", key="secret_OPENAI_API_KEY", type="password")
        st.text_input("ANTHROPIC_API_KEY", key="secret_ANTHROPIC_API_KEY", type="password")
        st.text_input("GOOGLE_API_KEY", key="secret_GOOGLE_API_KEY", type="password")
        st.caption("Kept for this session only — injected into the environment your code reads.")
    with st.expander("📁 File Search", expanded=False):
        st.text_input(
            "FILE_SEARCH_STORE_NAME", key="secret_FILE_SEARCH_STORE_NAME",
            placeholder="fileSearchStores/...",
        )
        st.caption("Used by the Google / Gemini (File Search) template.")
    with st.expander("🔧 Terminal — install packages / run commands", expanded=False):
        st.caption(
            f"Runs in the app's interpreter ({sys.executable}). "
            "`pip install ...` installs where this app can import it."
        )
        cmd = st.text_input(
            "Command", placeholder="pip install openai anthropic", key="_cmd"
        )
        if st.button("Run command") and cmd.strip():
            st.session_state["_shell"] = run_shell(cmd)
        shell = st.session_state.get("_shell")
        if shell:
            st.caption(f"exit {shell['code']}  ·  {shell['elapsed']:.1f}s")
            st.code(shell["out"] or "(no output)", language="text")

    st.divider()
    st.subheader("Setup")
    st.button("💾 Save", on_click=save_config, use_container_width=True)
    st.button("📂 Load", on_click=load_config, use_container_width=True)
    st.download_button(
        "⬇️ Download JSON",
        data=json.dumps(current_config(), indent=2, ensure_ascii=False),
        file_name=CONFIG_PATH,
        mime="application/json",
        use_container_width=True,
    )

    st.divider()
    st.caption(
        "Each panel runs your Python with `prompt` (latest message) and "
        "`messages` (full history) in scope. Set `response` to a string, or a "
        "generator that yields chunks to stream. Enter keys in 🔑 API keys above."
    )

n = st.session_state["num_panels"]

st.title("🆚 LLM Duel")
st.caption("Same conversation, side-by-side answers — streamed live. Edit each panel to drive any model.")

# Reading chat_input early lets us update history before rendering transcripts.
# (The widget itself still pins to the bottom of the page.)
# Which panels are checked to receive prompts (reads state from the checkboxes,
# which persist across reruns even though they're rendered further down).
targets = [i for i in range(n) if st.session_state.get(f"send_{i}", True)]

user_msg = st.chat_input("Message the selected panels…")
if user_msg and not targets:
    st.session_state["_toast"] = "No panels selected — check 📨 on at least one panel."
    user_msg = None
pending = bool(user_msg)
if pending:
    first_message = all(not history(i) for i in targets)
    for i in targets:
        history(i).append({"role": "user", "content": user_msg})
    if first_message:
        # The sidebar toggle is already rendered this run; flip it on the next
        # rerun (which run_stream triggers) so the editors tuck away.
        st.session_state["_collapse_code_next"] = True

# ----- Panels ---------------------------------------------------------------
# The chat_input is pinned to the bottom of the page; when the code editors are
# collapsed the columns are short, leaving a gap above it. Grow the chat area to
# fill that space when editors are hidden.
chat_height = 460 if st.session_state["show_code"] else 640
columns = st.columns(n)
placeholders = {}
for i, column in enumerate(columns):
    with column:
        st.text_input("Display name", key=f"name_{i}", label_visibility="collapsed")
        st.checkbox("📨 Receive prompts", key=f"send_{i}")
        with st.expander("⚙️ Code", expanded=st.session_state["show_code"]):
            st.selectbox(
                "Template", list(PRESETS), key=f"preset_{i}",
                on_change=apply_template, args=(i,), label_visibility="collapsed",
            )
            st.text_area("Code", key=f"code_{i}", height=260, label_visibility="collapsed")

        with st.container(height=chat_height, border=True):
            for msg in history(i):
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
                    if msg.get("elapsed"):
                        st.caption(f"⏱ {msg['elapsed']:.2f}s")
            if pending and i in targets:
                with st.chat_message("assistant"):
                    placeholders[i] = st.empty()

# ----- Stream the new turn, then rerun to render it cleanly from history ----
if pending:
    run_stream(placeholders, user_msg)
    st.rerun()
