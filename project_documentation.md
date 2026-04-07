# STEMbotix-AI Project Documentation

This document provides an extreme-detail overview of the **STEMbotix-AI** project architecture, covering the overall system flow, the frontend implementation, the backend implementation, and all the specialized AI algorithms handling code generation and simulation.

> [!NOTE]
> The STEMbotix-AI application is a comprehensive, browser-based educational platform designed to teach coding, robotics, and hardware electronics (ESP32/Arduino) using a mix of traditional IDE tools, visual block programming (Blockzie), interactive hardware simulators, and AI-driven tutors.

---

## 1. High-Level Architecture

The project follows a decoupled client-server architecture:

1. **Frontend (`STEMai-frontend`)**: A lightweight web layer built using Python Flask. It serves HTML/JS templates and static assets. Crucially, it doubles as an API reverse-proxy, forwarding any API request originating from the browser to the backend service.
2. **Backend (`STEMai-backend`)**: A highly robust, asynchronous API layer built using Python **FastAPI**. It manages all the heavy-lifting: user authentication, database interactions (SQLite), LLM integrations (via OpenRouter), code execution, and hardware code generation.

### Local Development Flow
When running locally, two separate servers are maintained:
- Frontend runs on `http://127.0.0.1:5000` (Flask `app.py`)
- Backend runs on `http://0.0.0.0:8123` (FastAPI `run.py`)

All browser requests are sent to the frontend. If the request matches `/api/*` or `/auth/*`, the Flask frontend transparently proxies the request to `http://localhost:8123` to be handled by FastAPI. 

---

## 2. Frontend Subsystem (`STEMai-frontend`)

The frontend relies heavily on server-side rendered HTML combined with vast, localized JavaScript modules to provide rich interactions without constant page reloads.

### Core Files & Directories:
- **`app.py`**: The entry point for the frontend. It defines all the page routes (e.g., `/`, `/ide`, `/dashboard`, `/simulator`, `/programming-lab`, `/blockzie`) and contains the reverse proxy logic `proxy_api()` using the `requests` library to bridge to the backend. In production (Vercel), `vercel.json` would handle this proxying natively.
- **`templates/`**: Contains the raw HTML files.
  - `programming_lab.html`: An advanced coding IDE interface with syntax highlighting (CodeMirror), embedded terminal output, and a dedicated AI assistant panel.
  - `simulator.html` / `Esp32 simulator.html`: Handles the interactive drag-and-drop hardware circuit simulations.
  - `index.html`: The main platform entry point, incorporating the `nlp_blockzie_intent.js` AI layer for teachers.
- **`static/`**: Contains static assets, including CSS and complex client-side JS logic.
  - **`app.js`**: Handles core Chat, UI interactions, and routing the visual block generation.
  - **`nlp_blockzie_intent.js`**: A specialized, client-side, zero-latency NLP intent parser to detect when teachers are attempting to generate Blockzie code using natural phraseology.

---

## 3. Backend Subsystem (`STEMai-backend`)

The backend is built with FastAPI to leverage fast, asynchronous processing—essential when waiting for LLM APIs or doing simulated code execution.

### Application Entry
- **`run.py`**: The Uvicorn server launcher. It incorporates specific logic (`ProactorServer`) for Windows to safely support asynchronous subprocesses (critical for the backend code execution tools). It binds the service to port `8123`.
- **`app/app.py`**: Constructs the FastAPI application, mounts CORS middleware, connects all feature routers, and manages the startup lifecycle (opening database tables for `User` models and initializing `init_db()`).

### Core Backend Modules (`app/core` & `app/models` & `app/services`)
- **`core/database.py`**: Configures SQLAlchemy ORM with local SQLite databases (`stembotix.db` and `teacher.db`). 
- **`models/user.py`**: Defines data structures for the user object, roles (`student`, `teacher`), and credential handling.
- **`core/auth.py`**: Manages secure JWT tokens, ensuring secure access to API endpoints.

---

## 4. Feature Implementation Details

The backend is split into several domain-specific Routers (`app/api/routes/`), each addressing a specific slice of STEMbotix functionality.

### A. The Programming Lab (`programming_lab.py`)
Provides an interactive environment for Python, JavaScript, C, and C++ coding.
- **Code Execution (`/api/lab/run`)**: Receives code, writes it to a temporary file, and uses Python's `subprocess.run` to synchronously compile/execute the code using local system binaries (e.g., `python`, `node`, `g++`), capturing standard output and standard error to display in the user's terminal.
- **Agentic Generation (`/api/lab/agentic`)**: Allows the user to prompt the AI to write a specific program. The backend interfaces with the LLM via `call_ai()`, robustly extracts the code block using regex (````python ... ````), and automatically runs the AI-generated code to verify its output before returning it to the user.

### B. Blockzie Visual Programming (`blockzie.py` & `stemx_text_to_xml.py`)
"Blockzie" is the platform's visual block programming interface (similar to Scratch/Blockly).
- **Text-to-Block Translation (`stemx_text_to_xml.py`)**: A massive regex-based parsing engine. It takes natural language or pseudo-code (e.g., `"set pin 2 mode output -> set digital pin 2 out HIGH"`) and statically translates it into robust XML element trees (`<xml><block type="...">...`) that the Blockzie frontend can render.
- **AI Block Generation (`stemx_text_to_xml_ai_make.py`)**: For complex prompts, the system uses an LLM to generate the intermediate pseudo-code sequence, which is then fed into the XML translation engine, essentially allowing children and teachers to say "Make an LED blink" and have the resulting block-code visually populate on screen.

### C. Circuit Simulator AI Planner (`simulator.py` & `ai_planner.py`)
Handles dynamic hardware component generation.
- **`ai_planner.py`**: A specialized AI orchestrator mapping out virtual breadboard layouts. When a user asks an AI to "build a temperature sensor circuit," this planner constructs an LLM prompt containing explicitly allowed hardware components (e.g., `esp32`, `led_red`, `dht11`) and safe digital pins.
- The LLM then returns structured JSON dictating X/Y coordinates for the components and mapping edge connections (e.g., `from pin 4 -> to DHT data`), which the frontend simulator uses to autonomously draw the circuit.

### D. Firmware & IoT Services (`firmware.py`, `iot.py`)
- **Firmware Compilation**: Designed to generate C++ code (Arduino/ESP32) via the OpenRouter LLM specifically mapped to the user's registered board architectures (FQBNs) and desired pin layouts.
- **IoT Telemetry**: Endpoints for physical STEMbotix hardware to ping home, relaying telemetry payloads (e.g., Temperature, Humidity) back to the application dashboards.

---

## 5. AI Integrations (OpenRouter)

To keep the platform flexible and intelligent, STEMbotix utilizes **OpenRouter** as an aggregate wrapper to contact various large language models cleanly.

- **Models Used**: The system falls back on `openai/gpt-4o-mini` for heavy, fast logic (like coding and XML structures) while providing fallbacks to Google's Exp models for free tier logic (`gemini-2.5-pro-exp`) or Anthropic for Premium users (`claude-3.5-sonnet`).
- **Prompt Engineering**: The backend enforces extremely rigid parsing rules on the AI logic (using explicit system instructions telling it to `Return ONLY the raw source code` or JSON formatting rules), allowing the returned text to be blindly parsed into functional states inside the educational environments.

> [!TIP]
> **Extensibility**: By abstracting AI requests into `call_ai()` and heavily relying on regex-fallback logic, the platform can swap its intelligence engines entirely behind the scenes via the `.env` file without breaking the fragile XML/Hardware parsers.
