"""T-371 E2/E3: static checks of the Docker artefacts, run-args, server script, logrotate and
the server config template (no docker needed)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from llmhub.configuration.loader import load_config

ROOT = Path(__file__).resolve().parent.parent
DOCKER = ROOT / "deploy" / "docker"
ARGS = DOCKER / "run-args"
SCRIPT = ROOT / "deploy" / "server" / "llmhub-deploy.sh"
NAMES = ("llmhub-api", "llmhub-reaper", "llmhub-weights-auth", "llmhub-weights-nginx")


def _opts(path: Path) -> list[str]:
    """docker run options only (everything before the image placeholder)."""
    args = _lines(path)
    for i, a in enumerate(args):
        if a in ("@IMAGE@", "@NGINX_IMAGE@"):
            return args[:i]
    raise AssertionError(f"{path.name}: no image placeholder")


def _lines(path: Path) -> list[str]:
    out: list[str] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("@include "):
            out += _lines(ARGS / line.removeprefix("@include "))
            continue
        out.append(line)
    return out


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


# ---------------------------------------------------------------- Dockerfile


def test_dockerfile_pins_and_user() -> None:
    text = (DOCKER / "Dockerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    assert froms and all("@sha256:" in f for f in froms)
    assert re.search(r"^USER 64100$", text, re.MULTILINE)
    assert not re.search(r"^ENV\b.*(SECRET|KEY|TOKEN)", text, re.MULTILINE | re.IGNORECASE)
    for bad in ("secrets/", ".env", ".key", "HEALTHCHECK"):
        assert not re.search(rf"^(COPY|ADD|HEALTHCHECK).*{re.escape(bad)}", text, re.MULTILINE)
    pip = [line for line in text.splitlines() if "pip install" in line]
    assert pip and "--no-index" in pip[0] and "--require-hashes" in pip[0]
    assert all("--no-index" in line for line in pip)


def test_images_env_matches_dockerfile() -> None:
    env = dict(
        line.split("=", 1) for line in (DOCKER / "images.env").read_text().splitlines()
        if line and not line.startswith("#")
    )  # fmt: skip
    assert all("@sha256:" in v for v in env.values())
    assert f"FROM {env['PYTHON_IMAGE']}" in (DOCKER / "Dockerfile").read_text()
    nginx_test = (ROOT / "tests/test_w_nginx_conf.py").read_text()
    assert env["NGINX_IMAGE"].split("@")[1] in nginx_test


def test_dockerignore_excludes_secrets() -> None:
    text = (DOCKER / ".dockerignore").read_text()
    for pat in ("**/.env", "**/*.key", "**/secrets"):
        assert pat in text


# ---------------------------------------------------------------- run-args


@pytest.mark.parametrize("name", NAMES)
def test_run_args_hardening(name: str) -> None:
    args = _opts(ARGS / f"{name}.args")
    assert _pairs(args, "--name") == [name]
    assert _pairs(args, "--network") == ["host"]
    assert "--read-only" in args
    assert _pairs(args, "--cap-drop") == ["ALL"]
    assert _pairs(args, "--security-opt") == ["no-new-privileges:true"]
    (user,) = _pairs(args, "--user")
    assert user.split(":")[0] not in ("0", "root") and user.startswith("641")
    assert _pairs(args, "--restart") == ["unless-stopped"]
    assert _pairs(args, "--log-driver") == ["journald"]
    assert len(_pairs(args, "--health-cmd")) == 1
    assert _pairs(args, "--memory") and _pairs(args, "--pids-limit") == ["256"]
    for bad in ("-p", "--publish", "--privileged", "--pid", "--ipc", "--cap-add"):
        assert bad not in args, bad
    for e in _pairs(args, "-e"):
        key, _, value = e.partition("=")
        assert key.endswith("_FILE") and value.startswith("/run/"), e
    for v in _pairs(args, "-v"):
        src = v.split(":")[0]
        if src.startswith("/opt/models"):
            assert v.endswith(":ro"), v
        if src == "/etc/llama/api-key":
            assert name == "llmhub-api" and v.endswith(":ro")
        if src.startswith("/etc/llmhub/secrets/") or src == "/etc/llmhub/config.yaml":
            assert v.endswith(":ro"), v
        assert not src.startswith("/var/run/docker.sock")


def test_llama_key_only_in_api() -> None:
    api = _lines(ARGS / "llmhub-api.args")
    assert "/etc/llama/api-key:/run/llama/api-key:ro" in _pairs(api, "-v")
    assert "@LLAMA_GID@" in _pairs(api, "--group-add")
    for other in NAMES[1:]:
        assert not any("llama" in v for v in _pairs(_lines(ARGS / f"{other}.args"), "-v"))


def _secrets(name: str) -> set[str]:
    out = set()
    for v in _pairs(_lines(ARGS / f"{name}.args"), "-v"):
        src = v.split(":")[0]
        if src.startswith("/etc/llmhub/secrets/"):
            out.add(src.rsplit("/", 1)[1])
    return out


def test_secret_mount_matrix() -> None:
    assert _secrets("llmhub-api") == {"label", "instance", "weights", "providers"}
    assert _secrets("llmhub-reaper") == {"label"}
    assert _secrets("llmhub-weights-auth") == {"weights"}
    assert _secrets("llmhub-weights-nginx") == set()
    nginx = _pairs(_lines(ARGS / "llmhub-weights-nginx.args"), "-v")
    assert "/opt/models:/opt/models:ro" in nginx
    # the validator reads the controller DB and the accounting log read-only
    auth = _pairs(_lines(ARGS / "llmhub-weights-auth.args"), "-v")
    assert "/var/lib/llmhub:/var/lib/llmhub:ro" in auth and "/var/log/llmhub:/var/log/llmhub:ro" in auth


def test_api_command_has_no_docs_and_image_by_id() -> None:
    args = _lines(ARGS / "llmhub-api.args")
    i = args.index("@IMAGE@")
    assert args[i + 1 :] == ["python", "-m", "llmhub", "serve", "--config",
                             "/etc/llmhub/config.yaml", "--no-docs"]  # fmt: skip
    reaper = _lines(ARGS / "llmhub-reaper.args")
    assert "--heartbeat" in reaper and "--loop" in reaper


# ---------------------------------------------------------------- config template


def test_config_example() -> None:
    cfg = load_config(ROOT / "deploy/config.example.yaml")
    assert cfg.providers.enabled == ["mock"]
    assert cfg.api.host == "10.8.0.1" and cfg.api.port == 8095 and cfg.api.docs is False
    assert cfg.clients["agents"].cloud_allowed is False
    assert cfg.clients["e2e"].mock_only is True and cfg.clients["e2e"].max_active_jobs == 4
    for b in cfg.local_backends.values():
        assert str(b.api_key_file) == "/run/llama/api-key" and b.api_key_env is None
        assert b.probe in ("slots", "health", "models")
    # the server template targets the real units (the only file that may: not a test fixture)
    real = [f"http://10.8.0.1:{port}/v1" for port in range(8090, 8093)]
    assert sorted(b.url for b in cfg.local_backends.values()) == real
    assert cfg.weights.min_mem_available_mib == 8192


# ---------------------------------------------------------------- server script (E3)

CODE = "\n".join(
    line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
)


def test_script_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-x", str(SCRIPT)], check=True)


@pytest.mark.parametrize(
    "pattern",
    [
        r"pkill", r"killall", r"set -x", r"set -o xtrace",
        r"systemctl\s+(restart|stop|disable|mask|daemon-reexec|kill)\s+(docker|llama|containerd)",
        r"docker\s+(system|network\s+rm|volume\s+rm|container\s+prune|image\s+prune)",
        r"\bprune\b", r"ufw\S*\"?\s+(reset|disable|default)", r"daemon\.json",
        r"docker\.service\.d", r"(echo|printf)[^\n|>]*\$\(?(cat|head)[^\n]*(secrets|\.key)",
        r"cat\s+\S*(secrets/|\.key)\S*\s*$", r"--privileged", r"\bcurl\b.*-k\b",
    ],
)
def test_script_forbidden(pattern: str) -> None:
    assert not re.search(pattern, CODE, re.MULTILINE), pattern


def test_docker_lifecycle_only_through_wrappers() -> None:
    """Raw docker rm/kill/stop/restart/start only inside `dctl`, docker exec only in `dexec`,
    docker run -d only in `drun_named`; every other `docker run` is `--rm`."""
    funcs = re.split(r"\n(?=[a-z_0-9]+\(\) \{)", CODE)
    for body in funcs:
        name = body.split("(", 1)[0].strip()
        for m in re.finditer(r"\bdocker\s+(rm|kill|stop|restart|start|exec|run)\b(.*)", body):
            verb, rest = m.group(1), m.group(2)
            if verb in ("rm", "kill", "stop", "restart", "start"):
                assert name == "dctl", (name, m.group(0))
            elif verb == "exec":
                assert name == "dexec", (name, m.group(0))
            elif verb == "run":
                assert "--rm" in rest or (name == "drun_named" and rest.startswith(" -d")), (
                    name, m.group(0),
                )  # fmt: skip
    assert "docker kill" not in CODE


def test_dctl_allowlist_is_llmhub_only() -> None:
    m = re.search(r"is_ours\(\) \{\n\s+case \"\$1\" in ([^)]+)\)", CODE)
    assert m
    assert set(m.group(1).split("|")) == set(NAMES)


def test_every_step_has_check_apply_rollback() -> None:
    steps = re.search(r'^STEPS="([^"]+)"', CODE, re.MULTILINE)
    assert steps
    for s in steps.group(1).split():
        for kind in ("check", "apply", "rollback"):
            assert re.search(rf"^{kind}_{s}\(\) \{{", CODE, re.MULTILINE), f"{kind}_{s}"


def test_script_uses_fixed_ids_and_rules() -> None:
    for need in ("UID_API=64100", "UID_NGINX=64101", "UID_WAUTH=64102",
                 "comment 'llmhub api'", "comment 'llmhub weights'",
                 "/w/000000000000/qwen/x.gguf", "shred -u"):  # fmt: skip
        assert need in CODE, need


# ---------------------------------------------------------------- logrotate (F12), units


def test_logrotate_f12() -> None:
    text = (ROOT / "deploy/logrotate-llmhub-acct").read_text()
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "copytruncate" not in body
    assert "create 0640 llmhub-wnginx llmhub" in body
    assert "maxsize 50M" in body and "rotate 7" in body
    assert "docker inspect -f '{{.State.Running}}' llmhub-weights-nginx" in body
    assert "|| true" in body and "exit 0" in body
    assert re.findall(r"docker kill[^\n]*", body) == [
        "docker kill -s USR1 llmhub-weights-nginx >/dev/null 2>&1 || true"
    ]


def test_backup_units() -> None:
    svc = (ROOT / "deploy/systemd/llmhub-backup.service").read_text()
    assert "Type=oneshot" in svc
    assert "ExecStart=/usr/bin/docker exec llmhub-api python -m llmhub backup" in svc
    timer = (ROOT / "deploy/systemd/llmhub-backup.timer").read_text()
    assert "Persistent=true" in timer and "OnCalendar=" in timer


def test_build_script() -> None:
    text = (DOCKER / "build.sh").read_text()
    subprocess.run(["bash", "-n", str(DOCKER / "build.sh")], check=True)
    for need in ("--pull=false", "--require-hashes", "--only-binary :all:", "uv export --frozen",
                 "docker save", "image.id", "SHA256SUMS"):  # fmt: skip
        assert need in text, need
