# Automations — Agent Instructions

## Python Environment

**ALWAYS use `uv` for Python commands. NEVER use `python`, `python3`, or `pip` directly.**

### Running Python Scripts
```bash
# ✅ CORRECT
uv run python script.py

# ❌ WRONG
python script.py
python3 script.py
```

### Installing Packages
```bash
# ✅ CORRECT
uv add package-name

# ❌ WRONG
pip install package-name
```

### Running Tests
```bash
# ✅ CORRECT
uv run pytest tests/

# ❌ WRONG
pytest tests/
python -m pytest tests/
```

### Checking Package Versions
```bash
# ✅ CORRECT
uv pip list

# ❌ WRONG
pip list
```

### Why `uv`?
- Consistent dependency management across all automations
- Faster than pip
- Ensures correct virtual environment is used
- Project uses `uv` for all dependency management

### Environment Variables
The project loads environment variables from `~/.openclaw/.env` automatically via `python-dotenv` in `lib/llm_utils.py`. Key variables:
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
