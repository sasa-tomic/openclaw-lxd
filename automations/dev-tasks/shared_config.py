PROJECT_CONFIGS = {
    "decent-cloud": {
        "repo_path": "/projects/decent-cloud",
        "test_command": "cargo test",
        "agents_md": "/projects/decent-cloud/AGENTS.md",
        "pre_impl_read": [
            "/projects/decent-cloud/AGENTS.md",
            "/projects/Notes/Pickle/memory/decent-cloud-dev.md",
        ],
        "dev_process": "/home/openclaw/clawd/docs/DEV_PROCESS.md",
    },
    "voki": {
        "repo_path": "/projects/voice-ai-agent",
        "test_command": "pytest",
        "agents_md": "/projects/voice-ai-agent/AGENTS.md",
        "pre_impl_read": [],
        "dev_process": "/home/openclaw/clawd/docs/DEV_PROCESS.md",
    },
    "default": {
        "repo_path": None,
        "test_command": "echo 'No test command configured'",
        "agents_md": None,
        "pre_impl_read": [],
        "dev_process": "/home/openclaw/clawd/docs/DEV_PROCESS.md",
    },
}


def get_project_config(project: str) -> dict:
    return PROJECT_CONFIGS.get(project, PROJECT_CONFIGS["default"])
