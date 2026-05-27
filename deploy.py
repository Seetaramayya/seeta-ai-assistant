#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path


def load_env(path=".env"):
    if not Path(path).exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def run(cmd, check=True, capture=False):
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result.stdout.strip() if capture else None


def golem_json(args):
    result = subprocess.run(["golem"] + args + ["-F", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def get_component_info(env):
    data = golem_json(["-E", env, "component", "list"])
    for c in (data or []):
        if c.get("componentName") == "seeta-ai-assistant:app":
            return c.get("componentId"), c.get("componentRevision")
    return None, None


def fix_telegram_agent(env, server_url, token):
    if not server_url:
        print(f"Skipping TelegramAgent fix (no server URL for {env})")
        return

    component_id, target_revision = get_component_info(env)
    if not component_id:
        print("Warning: could not find seeta-ai-assistant:app component ID, skipping TelegramAgent fix", file=sys.stderr)
        return

    data = golem_json(["-E", env, "agent", "list", "TelegramAgent"])
    agents = (data or {}).get("agents", [])
    match = [a for a in agents if "TelegramAgent" in a.get("agentName", "")]
    current_revision = match[0]["componentRevision"] if match else -1

    if current_revision == target_revision:
        print(f'TelegramAgent("main") already at revision {target_revision}')
        return

    print(f'Fixing TelegramAgent("main"): revision {current_revision} → {target_revision}')

    subprocess.run(["golem", "-E", env, "agent", "delete", 'TelegramAgent("main")', "--yes"],
                   capture_output=True)

    headers = ["-H", f"Authorization: Bearer {token}"] if token else []
    subprocess.run([
        "curl", "-sf", f"{server_url}/v1/components/{component_id}/workers",
        "-X", "POST", *headers,
        "-H", "Content-Type: application/json",
        "-d", '{"name": "TelegramAgent(\\"main\\")", "args": [], "env": {}}'
    ], capture_output=True, check=True)

    if target_revision > 0:
        subprocess.run(["golem", "-E", env, "agent", "update", 'TelegramAgent("main")',
                        "automatic", str(target_revision), "--await", "--yes"], check=True)

    print(f'TelegramAgent("main") updated to revision {target_revision}')

    webhook_url = os.environ.get("WEBHOOK_BASE_URL", "")
    if webhook_url:
        print("Registering Telegram webhook...")
        result = subprocess.run(
            ["curl", "-sf", f"{webhook_url}/telegram/main/start", "-X", "POST",
             "-H", "Content-Type: application/json"],
            capture_output=True, text=True
        )
        print(f"Webhook: {result.stdout.strip() or 'Failed'}")


def fix_stale_user_agents(env, server_url, token):
    if not server_url:
        return
    component_id, target_revision = get_component_info(env)
    if target_revision is None or not component_id:
        return

    data = golem_json(["-E", env, "agent", "list", "UserAgent"])
    agents = (data or {}).get("agents", [])
    stale = [a for a in agents if a.get("componentRevision") != target_revision]

    if not stale:
        return

    for a in stale:
        name = a["agentName"]
        # Extract user ID from e.g. UserAgent("8487659930") -> 8487659930
        user_id = name.removeprefix('UserAgent("').removesuffix('")')
        print(f"  Fixing stale agent {name} (revision {a.get('componentRevision')} → {target_revision})")
        subprocess.run(["golem", "-E", env, "agent", "delete", name, "--yes"], capture_output=True)
        # Recreate with empty oplog so update succeeds
        headers = ["-H", f"Authorization: Bearer {token}"] if token else []
        subprocess.run([
            "curl", "-sf", f"{server_url}/v1/components/{component_id}/workers",
            "-X", "POST", *headers,
            "-H", "Content-Type: application/json",
            "-d", f'{{"name": "UserAgent(\\"{user_id}\\")", "args": [], "env": {{}}}}'
        ], capture_output=True, check=True)
        if target_revision > 0:
            subprocess.run(["golem", "-E", env, "agent", "update", name,
                            "automatic", str(target_revision), "--await", "--yes"], check=True)
        print(f"  {name} updated to revision {target_revision}")


def main():
    load_env()

    target = sys.argv[1] if len(sys.argv) > 1 else "self-hosted"
    mode = sys.argv[2] if len(sys.argv) > 2 else ""

    targets = {
        "local":        ("local",       "http://localhost:9881",    "",                                      "http://localhost:9881"),
        "golem-cloud":  ("cloud",       "",                         "",                                      "https://seeta-ai-assistant.apps.golem.cloud"),
        "cloud":        ("cloud",       "",                         "",                                      "https://seeta-ai-assistant.apps.golem.cloud"),
        "self-hosted":  ("self-hosted", "https://golem.vadali.in", os.environ.get("GOLEM_STATIC_TOKEN", ""), "https://golem-api.vadali.in"),
        "self-hosting": ("self-hosted", "https://golem.vadali.in", os.environ.get("GOLEM_STATIC_TOKEN", ""), "https://golem-api.vadali.in"),
    }

    if target not in targets:
        print(f"Usage: {sys.argv[0]} [local|golem-cloud|self-hosted] [--redeploy|--reset]", file=sys.stderr)
        print("  --redeploy  delete and recreate all agents (loses state)", file=sys.stderr)
        print("  --reset     delete agents + environment, fully fresh start", file=sys.stderr)
        sys.exit(1)

    env, server_url, token, webhook_base_url = targets[target]
    os.environ["WEBHOOK_BASE_URL"] = webhook_base_url

    deploy_flags_map = {
        "--redeploy": ["--yes", "--redeploy-agents"],
        "--reset":    ["--yes", "--reset"],
        "":           ["--yes", "--update-agents", "auto"],
    }

    if mode not in deploy_flags_map:
        print(f"Unknown mode: {mode}. Use --redeploy or --reset", file=sys.stderr)
        sys.exit(1)

    print(f"Deploying to: {env}" + (f" (mode: {mode})" if mode else ""))
    run(["golem", "build"])
    run(["golem", "-E", env, "deploy"] + deploy_flags_map[mode])

    fix_telegram_agent(env, server_url, token)
    fix_stale_user_agents(env, server_url, token)


if __name__ == "__main__":
    main()
