# Helix

Helix is the final HackPrinceton 2026 application for commercial real estate feasibility and investment intelligence. The primary application is in [`project helix/`](project%20helix/). Earlier prototypes, shared infrastructure, and their tests remain in [`playground/`](playground/); research and reports remain in their respective directories.

## Run locally

Use Python 3 and run from the repository root:

```sh
cp .env.example .env
python3 "project helix/app.py" --port 8000 --no-browser
```

Open <http://127.0.0.1:8000/>. The server and core modules use Python's standard library. Populate `.env` with the credentials needed for the integrations you use; never commit it. CoStar-backed discovery requires the hackathon kit ZIP and its password. By default the server looks for `~/Downloads/costar-hackathon-kit.zip`; set `COSTAR_KIT_PATH` to use another location and `COSTAR_KIT_PASSWORD` (or `COSTAR_KIT_ZIP_PASSWORD`) for its password. Other optional service keys are listed in `.env.example`. External services and sponsor datasets are required for the corresponding live features.

For the demo replay mode, run `sh "project helix/run_demo.sh"` and open <http://127.0.0.1:8127/>. Replay depends on saved local investigation, report, and Stage 3 data; these generated files are ignored and are not included in the repository. Without matching saved data, the application uses the live pipeline.

## Application overview

- **Stage 0:** intake for location, budget, financing, and business preferences.
- **Stage 1:** property discovery and verification using the CoStar kit, with optional OpenRouter/Nemotron explanations.
- **Stage 2:** building investigation and evidence reconciliation, with integrations including Matterport, Vexcel, Tavily, and Census where configured.
- **Stage 3:** deterministic opportunity screening, compliance and financial analysis through the bundled engine.
- **Stage 4:** business selection, Finance Lab and investment memorandum. Finance features can use Capital One Nessie when configured.

The Python HTTP server is `project helix/app.py`; the browser interface is served from the same directory. Stage 2, Stage 3, and Finance Lab modules live in their respective subdirectories. `project helix/stage3_engine/README.md` describes the Stage 3 adapter and data limitations.

## Tests

```sh
python3 -m unittest discover -s "project helix" -p 'test_*.py'
python3 -m unittest discover -s "project helix/stage2" -p 'test_*.py'
python3 -m unittest discover -s "project helix/stage3_engine" -p 'test_*.py'
(cd "project helix" && python3 -m unittest finance_lab.test_engine)
python3 -m unittest discover -s playground -p 'test_*.py'
```

Some tests and live workflows require local sponsor data, caches, credentials, or optional dependencies.
